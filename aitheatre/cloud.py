"""Облачная модель через шлюз в формате OpenAI.

Зачем отдельный модуль. Ollama и облачные шлюзы говорят на разных диалектах
одного и того же разговора: список сообщений с ролями есть у обоих, но у Ollama
числа вложены в «options» и есть свои поля (think, min_p, num_ctx), а у шлюза
они лежат прямо в запросе, а ключ передаётся заголовком. Здесь — перевод в обе
стороны, и больше ничего: сам спектакль, промпты и история живут как прежде и
про облако не знают (см. ollama_api.ask_model_with_tools — он выбирает,
куда отправить ход).

Формат, который понимает модуль (он же — то, что показано в примерах шлюзов):

    POST {base}/chat/completions
    Authorization: Bearer <ключ>
    {"model": "qwen/qwen3.7-flash", "messages": [...], "stream": false}

Тело запроса нарочно короткое: числа характера, инструменты и поле «name»
у сообщений добавляются только по настройке (CLOUD_SEND_*): каждый лишний
ключ — повод для «400 Bad Request», а серия таких отказов уводит ключ в паузу
и превращает всё в 429.

    {"choices": [{"message": {"content": "...", "tool_calls": [...]}}],
     "usage": {"prompt_tokens": 100, "completion_tokens": 200}}

Круг поиска переводится тоже. У Ollama вызов инструмента и его результат
связаны именем, а у OpenAI — идентификаторами: в сообщении ассистента лежит
tool_calls с id, в ответе инструмента — tool_call_id. Без этого перевода
облачный поиск не работал бы: шлюз либо отвечает 400, либо не понимает,
к какому вызову относится найденное. См. openai_messages.

Ответ умеет приходить потоком ("stream": true): тогда реплика печатается
в ленте по кускам, пока модель говорит, вместо того чтобы появиться целиком
в конце. Кусок за куском читает и отдаёт наружу _read_stream, а сам спектакль
получает готовый текст всё равно одним значением — поэтому остальной код
о потоке не знает. Выключатель — CLOUD_STREAM.

Про ключ. Он берётся из settings (или переменной окружения) и уходит ТОЛЬКО
в этом заголовке. Ни в текст ответа, ни в сообщения об ошибках, ни на страницу
он не попадает: вычистить его из чужого текста — обязанность этого модуля,
потому что ошибку шлюза мы показываем прямо в ленте спектакля.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import settings

# Список моделей шлюза кэшируется: проверка готовности состава обращается к нему
# на каждую правку состава, а меняется этот список раз в месяц
_MODELS_CACHE = {"at": 0.0, "names": [], "error": None}


# ── ФАЙЛ .env ────────────────────────────────────────────────────────────────
# Ключ не должен жить ни в коде, ни в git: файл .env рядом с проектом уже
# в .gitignore, и все настройки облака можно держать там. Парсер свой, без
# зависимостей — файл простой: строки КЛЮЧ=ЗНАЧЕНИЕ, комментарии с решётки.

# Имена настроек облака, которые приложение читает из окружения и из .env.
# Список нужен не для чтения (читаем по имени), а чтобы узнавать опечатки
# и склейки: строка в .env, которая упоминает настройку, но не совпадает с её
# именем ровно, — почти всегда ошибка копирования, и молчать про неё нельзя
CLOUD_ENV_NAMES = tuple(dict.fromkeys(
    name for name in (
        "CLOUD_API_KEY", "CLOUD_KEY_ENV", "CLOUD_BASE_URL", "CLOUD_TIMEOUT",
        "CLOUD_SEND_PARAMS", "CLOUD_SEND_TOOLS", "CLOUD_SEND_MESSAGE_NAMES",
        "CLOUD_PASS_OLLAMA_EXTRAS", "CLOUD_RETRY_DELAYS",
        settings.CLOUD_KEY_ENV,      # имя ключа, заданное в настройках
    ) if name
))


def _parse_dotenv(text: str) -> dict:
    """Строки КЛЮЧ=ЗНАЧЕНИЕ из .env: комментарии и пустые строки пропускаются.

    Кавычки вокруг значения снимаются. Значение может быть пустым — оно
    просто не ставится.
    """
    parsed = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("\ufeff"):
            line = line[1:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            continue
        if value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        parsed[key] = value
    return parsed


def _recognized_key(raw_key: str) -> tuple:
    """Настоящее имя настройки по строке из .env и замечание, если строка кривая.

    Зачем это появилось. В образце .env подсказки были написаны одной строкой:
    «Включить: CLOUD_SEND_TOOLS=1». Человек убирал решётку — и получал настройку
    с именем «Включить: CLOUD_SEND_TOOLS». Приложение читало её как чужую
    строку и молча ничего не включало: потом причину искали в шлюзе, а причина
    была в лишних словах перед именем. Теперь такое имя узнаётся, и об этом
    говорится вслух — чтобы строку переименовали, а не гадали.
    """
    key = str(raw_key or "").strip()
    if key in CLOUD_ENV_NAMES:
        return key, ""
    for name in CLOUD_ENV_NAMES:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", key):
            return name, (f".env: строка «{key}» — это настройка {name}. "
                          f"Она прочитана, но строку лучше переименовать")
    if "cloud" in key.lower():
        return "", (f".env: строка «{key}» не знакома приложению — "
                     f"настройка НЕ применена")
    return key, ""


def _load_dotenv() -> None:
    """Прочитать .env из корня проекта в окружение процесса.

    setdefault — настоящая переменная окружения важнее файла: ключ, заданный
    в системе, файл не перекроет.
    """
    dotenv_file = settings.PROJECT_ROOT / ".env"
    if not dotenv_file.is_file():
        return
    try:
        text = dotenv_file.read_text(encoding="utf-8")
    except OSError:
        return    # файл пропал в момент чтения — ничего страшного
    for raw_key, value in _parse_dotenv(text).items():
        key, note = _recognized_key(raw_key)
        if note:
            print(f"  ⚠️  {note}")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv()


# ── ЧТО СЧИТАЕТСЯ ОБЛАЧНОЙ МОДЕЛЬЮ ──────────────────────────────────────────

def _env_flag(name: str, default: bool) -> bool:
    """Переключатель из .env: «1», «true», «да» — включено, «0» — выключено.

    Пусто или непонятное значение — берётся то, что стоит в settings: иначе
    опечатка в .env молча выключала бы то, что человек только что включил.
    """
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "да", "вкл")


def send_params() -> bool:
    """Отправлять ли на шлюз числа участника (temperature и прочие)."""
    return _env_flag("CLOUD_SEND_PARAMS", settings.CLOUD_SEND_PARAMS)


def send_tools() -> bool:
    """Отправлять ли шлюзу инструмент поиска. Без поиска в проекте — и тут нет."""
    return settings.ENABLE_SEARCH and _env_flag("CLOUD_SEND_TOOLS", settings.CLOUD_SEND_TOOLS)


def send_message_names() -> bool:
    """Оставлять ли у сообщений поле «name»."""
    return _env_flag("CLOUD_SEND_MESSAGE_NAMES", settings.CLOUD_SEND_MESSAGE_NAMES)


def pass_extras() -> bool:
    """Отправлять ли min_p, top_k и repeat_penalty: их понимают не все шлюзы."""
    return _env_flag("CLOUD_PASS_OLLAMA_EXTRAS", settings.CLOUD_PASS_OLLAMA_EXTRAS)


def stream_replies() -> bool:
    """Печатать ли ответ облачной модели в ленте по мере генерации."""
    return _env_flag("CLOUD_STREAM", settings.CLOUD_STREAM)


def limit_params() -> bool:
    """Приводить ли числа характера к области, которую понимает вендор."""
    return _env_flag("CLOUD_LIMIT_PARAMS", settings.CLOUD_LIMIT_PARAMS)


def turn_limit() -> int:
    """Сколько секунд может длиться один ход облачной модели целиком.

    CLOUD_TIMEOUT — про один кусочек ответа, а этот срок — про весь ход.
    Разница важна: «думающая» модель шлёт кусочки исправно, но зацикливается
    и молотит случайные токены полчаса — спектакль при этом стоит.
    """
    return _env_int("CLOUD_TURN_LIMIT", settings.CLOUD_TURN_LIMIT)


def show_thinking() -> bool:
    """Показывать ли размышления модели — то, что она говорит сама с собой.

    Размышлений бывает больше самого ответа (у gpt-5-nano — 65 порций против 6),
    и они уже оплачены: это те же выходные токены. Поэтому по умолчанию видны.
    """
    return _env_flag("CLOUD_SHOW_THINKING", settings.CLOUD_SHOW_THINKING)


def _env_int(name: str, default: int) -> int:
    """Целое из .env: пусто или мусор — берём значение из settings."""
    try:
        return max(1, int(float((os.environ.get(name) or "").strip())))
    except (TypeError, ValueError):
        return default


def _env_number_list(name: str, default: tuple) -> tuple:
    """Список секунд из .env: «1, 2, 4» или «(1, 2, 4)». Мусор — берём settings."""
    text = (os.environ.get(name) or "").strip().strip("()[]")
    if not text:
        return tuple(default or ())
    numbers = []
    for part in text.replace(";", ",").split(","):
        try:
            numbers.append(max(0.0, float(part.strip())))
        except ValueError:
            return tuple(default or ())     # непонятная строка — не гадаем
    return tuple(numbers)


def timeout_seconds() -> int:
    """Сколько секунд ждать ответа шлюза.

    Вынесено в .env не для красоты: ответ «думающих» моделей бывает дольше
    любой разумной задержки, и поднять предел должно быть можно без правки кода.
    """
    return _env_int("CLOUD_TIMEOUT", settings.CLOUD_TIMEOUT)


def retry_delays() -> tuple:
    """Паузы перед повтором после 429: свои из .env или те, что в settings."""
    return _env_number_list("CLOUD_RETRY_DELAYS", settings.CLOUD_RETRY_DELAYS)


# Роли, которые понимает схема OpenAI. Всё остальное — слова человека: лучше
# отдать такое сообщение как «user», чем получить 400 на весь запрос
_OPENAI_ROLES = ("system", "user", "assistant", "tool")


def openai_messages(messages: list, keep_names: bool = False) -> list:
    """Наши сообщения — в вид OpenAI, вместе с кругом поиска.

    Диалект круга у Ollama свой: результат инструмента уходит сообщением
    с ролью tool и именем инструмента, и связь держится на имени. У OpenAI
    иначе — на идентификаторах: в сообщении ассистента лежит tool_calls с id,
    а в ответе инструмента — tool_call_id того же вызова. Без этого перевода
    облачный поиск не работал бы: шлюз не знает, к какому вызову относится
    найденное (а некоторые шлюзы отвечают на такое сообщение 400).

    Заодно убираются имена. По схеме OpenAI поле «name» — это латиница, цифры,
    дефис и подчёркивание, а у нас там русские имена: шлюз честно отвечает
    «400 Bad Request», и хорошо, если не на каждом ходу. Двадцать таких
    ответов за минуту переводят ключ в паузу, и на всё сыплется 429 — а причина
    остаётся невидимой, потому что имя в теле никто не заподозрит. Кто говорит,
    и так видно из текста: «Имя говорит: …» дописывается в show.
    """
    result = []
    pending = []    # id вызовов, которым ещё не отдан результат
    made = 0        # сколько имён вызовов пришлось придумать самим

    for message in (messages or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower()
        content = message.get("content")
        if isinstance(content, list):
            # Некоторые шлюзы отдают текст частями, как в потоковом режиме
            content = "".join(str(part.get("text", "")) for part in content
                              if isinstance(part, dict))
        content = "" if content is None else str(content)

        if role == "assistant":
            item = {"role": "assistant", "content": content}
            calls = []
            for call in (message.get("tool_calls") or []):
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name") or "").strip()
                if not name:
                    continue
                call_id = str((call.get("id") if isinstance(call, dict) else "") or "").strip()
                if not call_id:
                    # Шлюз не дал id — придумываем сами: он нужен только для
                    # того, чтобы связать вызов с его результатом в этом же
                    # запросе, и внутри него повторов не бывает
                    made += 1
                    call_id = f"call_{made}"
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": name, "arguments": arguments}})
            if calls:
                item["tool_calls"] = calls
                pending = [call["id"] for call in calls]
            result.append(item)
            continue

        if role in ("tool", "function"):
            if pending:
                item = {"role": "tool", "content": content,
                        "tool_call_id": pending.pop(0)}
            else:
                # Привязывать не к чему (история начинается с результата):
                # отдаём словами человека — сообщение tool без tool_call_id
                # шлюз считает ошибкой
                item = {"role": "user", "content": content}
        else:
            item = {"role": role if role in _OPENAI_ROLES else "user", "content": content}

        if keep_names and message.get("name"):
            item["name"] = str(message["name"])
        result.append(item)

    return result


def is_cloud_model(model: str) -> bool:
    """Модель с префиксом («cloud:qwen/qwen3.7-flash») играет на облаке.

    Префикс — единственный признак, и он же виден в пульте: по нему понятно,
    куда уйдёт реплика, ещё до начала спектакля.
    """
    return bool(model) and str(model).startswith(settings.CLOUD_MODEL_PREFIX)


def bare_model_name(model: str) -> str:
    """«cloud:qwen/qwen3.7-flash» -> «qwen/qwen3.7-flash» — имя для шлюза."""
    name = str(model or "")
    if name.startswith(settings.CLOUD_MODEL_PREFIX):
        return name[len(settings.CLOUD_MODEL_PREFIX):]
    return name


def cloud_model_id(name: str) -> str:
    """Обратно: имя у шлюза -> имя в пульте («cloud:...»)."""
    name = str(name or "")
    if not name or name.startswith(settings.CLOUD_MODEL_PREFIX):
        return name
    return settings.CLOUD_MODEL_PREFIX + name


# ── КЛЮЧ ────────────────────────────────────────────────────────────────────

# Имена переменных окружения, под которыми может лежать ключ, по убыванию
# приоритета. Основное — CLOUD_API_KEY (так предлагает .env.example), но
# подхватываются и варианты из документации приложения: THEATRE_CLOUD_API_KEY
# и простое CLOUD_KEY_ENV — кто-то уже положил ключ под этим именем
_KEY_ENV_NAMES = ("CLOUD_API_KEY", settings.CLOUD_KEY_ENV, "CLOUD_KEY_ENV")


def api_key() -> str:
    """Ключ: переменная окружения (в т.ч. из .env), иначе settings. Пусто — не настроено.

    Ключ часто копируют из документации вместе с кавычками: «Bearer sk-...»
    или “sk-...” — лишнее отрезаем, иначе шлюз ответит 401, и никто не поймёт почему.
    """
    key = ""
    for name in _KEY_ENV_NAMES:
        if name and os.environ.get(name):
            key = os.environ[name]
            break
    key = key or settings.CLOUD_API_KEY or ""
    # В каком бы порядке ни пришло: «Bearer sk-...», «“sk-...”» или всё сразу
    for _ in range(2):
        key = key.strip().strip('"\'').strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
    return key


def key_problem() -> str:
    """Пустой ключ или ключ с непонятными знаками — объясняем словами.

    Заголовки HTTP кодируются однобайтной latin-1, поэтому ключ с кириллицей или
    с неразрывным пробелом из скопированного текста падал бы невнятным
    «can't encode character in position 7»: причину видно, а делать что — нет.
    """
    key = api_key()
    if not key:
        return ("ключ облачного шлюза не задан: положите его в файл .env рядом с проектом "
                "(строка CLOUD_API_KEY=ваш-ключ) или в переменную окружения "
                f"{settings.CLOUD_KEY_ENV}")
    if not key.isascii() or any(symbol.isspace() for symbol in key):
        return ("ключ облачного шлюза выглядит странно: в нём есть пробелы или "
                "не латинские символы. Скопируйте его заново — в ключе только "
                "латинские буквы, цифры и знаки -_")
    return ""


def is_configured() -> bool:
    """Готов ли шлюз к работе. Без ключа облачные участники говорить не смогут."""
    return not key_problem()


def hide_key(text: str) -> str:
    """Убрать ключ из любого текста, который увидит человек.

    Ошибки шлюза попадают в ленту спектакля и в консоль, а шлюз иногда
    пересказывает запрос целиком — вместе с заголовком. Ключ не должен
    уезжать туда, где его увидят или скопируют.
    """
    key = api_key()
    if not key:
        return text or ""
    return str(text).replace(key, "***")


def base_url() -> str:
    """Адрес шлюза: настройка settings или строка CLOUD_BASE_URL в .env.

    Адрес — вещь подвижная (у шлюзов бывают зеркала), поэтому его можно
    переопределить, не трогая код.
    """
    return (os.environ.get("CLOUD_BASE_URL") or settings.CLOUD_BASE_URL).rstrip("/")


def _url(path: str) -> str:
    return f"{base_url()}/{path.lstrip('/')}"


# Что значат коды шлюза — словами, а не номером: по номеру причина не видна,
# а ошибка из чата идёт прямо в ленту спектакля.
# Поля тела, которые в схеме OpenAI отвечают за инструменты поиска
_TOOL_FIELDS = ("tools", "tool_choice", "functions")

# Числа, которые шлюз назвал лишними: он пишет имя поля словами («Unknown
# parameter: 'repeat_penalty'»), и повторять этот отказ на каждом ходу незачем.
# Помним по имени модели: за тем же шлюзом у другого вендора те же min_p, top_k
# и repeat_penalty вполне могут быть поняты
_DROPPED_PARAMS = {}      # модель -> {имена полей}

# Имя поля из слов самого шлюза: «Unknown parameter: 'repeat_penalty'»,
# «Unrecognized request argument supplied: min_p». Берём первое названное —
# в посланиях вроде «Unsupported parameter: 'max_tokens' … Use 'max_completion_tokens'
# instead» лишним объявлено именно оно
_NAMED_FIELD_RE = re.compile(
    r"(?:unknown|unrecognized|unsupported|unexpected|invalid|extra)\b"
    r"[^'\"]{0,40}?['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]",
    re.IGNORECASE)


# Сколько раз можно поправить запрос после отказа шлюза. Один круг мало:
# вендоры называют лишние поля по одному («Unsupported parameter: 'top_k'», потом
# про temperature), а у «Хаоса» в характере их сразу пять, и у модели вроде
# gpt-5-nano, которая чисел не понимает вовсе, он высказывается постепенно.
# Шести кругов хватает, чтобы всё это случилось в одном ходу, а не в каждом:
# убранный ключ запоминается для модели, и дальше ход идёт без отказов
_EASE_ROUNDS = 6


def dropped_params(model: str) -> set:
    """Поля, которых эта модель больше не увидит: шлюз назвал их лишними."""
    return _DROPPED_PARAMS.get(model, set())


def named_field(text: str) -> str:
    """Какое поле шлюз назвал лишним или непонятным — или пустая строка.

    Читаем только его слова: у каждого вендора свои числа, и догадка по имени
    модели была бы гаданием (так, OpenAI-форма не знает repeat_penalty, а vLLM
    за тем же шлюзом его прекрасно понимает).
    """
    found = _NAMED_FIELD_RE.search(text or "")
    return found.group(1) if found else ""


def _eased_body(model: str, body: dict, error):
    """Тело запроса без того, на что шлюз пожаловался (или None).

    Ответ на 400 шлюз пишет словами, и три причины понятны прямо из них:

    Первая. «Unsupported parameter: 'max_tokens'... Use 'max_completion_tokens'
    instead» — так отвечают новые модели OpenAI (gpt-5, o-серия) на старое имя
    поля. Переименовываем и запоминаем: со следующего хода поле уйдёт правильно
    сразу, и отказ не повторится.

    Вторая — та же жалоба, но на поле, которого у вендора нет вовсе: «Unknown
    parameter: 'repeat_penalty'». Так OpenAI-форма отвечает на числа Ollama
    (min_p, top_k, repeat_penalty) у тех вендоров, которые их не понимают,
    а включаются они одной общей настройкой (CLOUD_PASS_OLLAMA_EXTRAS) —
    то есть выключить их выборочно было нельзя, и ход пропадал на каждом круге.
    Теперь названное поле убирается из тела и запоминается для этой модели.

    Третья. Если запрос с инструментом поиска не проходит, а такой же без него
    проходит — модель его не принимает. Признаком этого делится вызывающий:
    он видит, что запрос без инструмента удался (см. _MODELS_WITHOUT_TOOLS).

    None — сказать нечего: тогда ошибка показывается как есть, а не превращается
    в три бесполезных запроса подряд.
    """
    text = str(error)
    named = named_field(text)

    # max_tokens — не лишнее поле, а другое его имя, и шлюз прямо это пишет
    if "max_tokens" in body and (named == "max_tokens" or "max_completion_tokens" in text):
        _RENAMED_MAX_TOKENS.add(model)
        print(f"  ℹ️  Облако: {bare_model_name(model)} — поле числа токенов "
              f"называется иначе, шлю как max_completion_tokens")
        eased = {key: value for key, value in body.items() if key != "max_tokens"}
        eased["max_completion_tokens"] = body["max_tokens"]
        return eased

    if named in _TOOL_FIELDS:
        # Про инструмент шлюз сказал сам — убираем его, не трогая числа
        return {key: value for key, value in body.items() if key not in _TOOL_FIELDS}

    if named and named in body:
        _DROPPED_PARAMS.setdefault(model, set()).add(named)
        print(f"  ℹ️  Облако: {bare_model_name(model)} не знает поля {named} — "
              f"убираю его из запроса и больше не шлю (в теле останутся те числа, "
              f"которые она понимает)")
        return {key: value for key, value in body.items() if key != named}

    if "tools" in body or "tool_choice" in body:
        return {key: value for key, value in body.items() if key not in _TOOL_FIELDS}
    return None


_HTTP_HINTS = {
    400: "шлюз не понял запрос. Обычно дело в лишнем поле в теле: названное поле "
         "приложение убирает само и повторяет запрос, а если отказ повторяется — "
         "выключите числа характеров (CLOUD_SEND_PARAMS) или инструменты поиска "
         "(CLOUD_SEND_TOOLS)",
    401: "шлюз не принял ключ: проверьте строку CLOUD_API_KEY в файле .env",
    402: "на ключе кончились средства: пополните баланс",
    403: "ключ не даёт доступа к этой модели",
    404: "модель или адрес шлюза не найдены: проверьте CLOUD_BASE_URL и имя модели",
    429: "шлюз просит сбавить темп: либо лимит модели, либо пауза по ключу после "
         "серии ошибок. Подождите и попробуйте снова",
}


def _error_text(code: int, reason: str, detail: str) -> str:
    """Ошибка шлюза одной строкой: код, подсказка словами и ответ шлюза."""
    text = f"шлюз ответил HTTP {code} {reason}".strip()
    hint = _HTTP_HINTS.get(code)
    if hint:
        text += f" — {hint}"
    if detail:
        # Шлюз иногда пересказывает запрос целиком: длинный ответ обрезаем
        text += f": {detail[:300]}"
    return text


class GatewayError(str):
    """Текст ошибки шлюза, помнящий код ответа.

    Строка — потому что этот текст печатают и показывают в ленте спектакля как
    обычный текст; код — потому что по нему принимаются решения (например,
    повтор без инструмента поиска). Разбирать для этого текст обратно не надо.
    """

    code = None

    def __new__(cls, text: str, code: int = None):
        error = super().__new__(cls, text)
        error.code = code
        return error


def _wait_before_retry(attempt: int, headers) -> float:
    """Сколько ждать до следующей попытки при 429.

    Своя задержка растёт (1, 2, 4 секунды), но если шлюз прислал Retry-After —
    слушаем его. Огромные значения (пауза по ключу бывает и в 12 часов) в сон
    превращать нельзя: спектакль на это время не остановишь, поэтому берём
    не больше сигнала «подожди немного»
    """
    delays = tuple(retry_delays() or ())
    wait = float(delays[attempt]) if attempt < len(delays) else 0.0
    try:
        offered = float((headers or {}).get("Retry-After"))
        if 0 < offered <= 15:
            wait = max(wait, offered)
    except (TypeError, ValueError):
        pass
    return wait


def _request(path: str, payload=None, method: str = "GET", timeout: int = None,
             retries: int = None, read=None):
    """Один запрос к шлюзу. Возвращает (данные, текст ошибки) — без исключений.

    Ключа в тексте ошибки не бывает: сюда его подставляет только этот метод,
    а наружу уходит уже прогнанным через hide_key.

    Повторяется только 429 и только с задержкой: остальные 4xx — про сам запрос,
    и повторять их бессмысленно (и вредно: серия ошибок уводит ключ в паузу).

    read — как разобрать ответ. Обычный ответ и поток разбираются по-разному,
    а всё остальное (ключ, повторы, срок ожидания, ошибки) у них общее,
    поэтому выбор разбора — не повод для второго такого же метода.
    """
    problem = key_problem()
    if problem:
        return None, problem
    key = api_key()

    headers = {"Authorization": f"Bearer {key}"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(_url(path), data=data, headers=headers, method=method)
    # Шлюз — сам прокси до OpenAI и других API, в этом его смысл. Вести его ещё
    # и через свой локальный прокси (как поиск в интернете) значило бы замедлить
    # запрос, а часто и сломать. Поэтому свой открыватель вообще без прокси.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    seconds = timeout or timeout_seconds()
    delays = tuple(retry_delays() or ())
    attempts = (len(delays) if retries is None else max(0, retries)) + 1

    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=seconds) as response:
                if read is not None:
                    return read(response), None
                body = response.read().decode("utf-8")
            try:
                return json.loads(body), None
            except ValueError:
                return None, hide_key(f"шлюз ответил не данными, а текстом: {body[:200]}")
        except TimeoutError:
            # Своё сообщение вместо «The read operation timed out»: по сырому
            # тексту нельзя понять ни сколько ждали, ни что с этим делать
            return None, GatewayError(
                f"модель не ответила за {seconds} с: шлюз не прислал данные. "
                f"«Думающим» и большим моделям нужно больше времени — "
                f"увеличьте CLOUD_TIMEOUT в .env")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:500]
            except Exception:
                detail = ""
            finally:
                e.close()   # иначе Python ругается на не закрытый ответ ошибки
            if e.code == 429 and attempt + 1 < attempts:
                wait = _wait_before_retry(attempt, getattr(e, "headers", None))
                print(f"  ⏳ Шлюз просит подождать (429), повторяю через {wait:.0f} с "
                      f"— попытка {attempt + 2} из {attempts}")
                time.sleep(wait)
                continue
            return None, GatewayError(hide_key(_error_text(e.code, e.reason, detail)), e.code)
        except Exception as e:
            return None, GatewayError(hide_key(f"шлюз недоступен по адресу {base_url()}: {e}"))

    return None, "шлюз не ответил"      # недостижимо: попытки кончаются возвратом


# ── ПЕРЕВОД ЧИСЕЛ УЧАСТНИКА ─────────────────────────────────────────────────

# Модели, которым поле числа токенов надо называть max_completion_tokens. Узнаём
# это из отказа шлюза (см. _eased_body): угадывать по имени модели — гадание,
# а шлюз говорит прямо
_RENAMED_MAX_TOKENS = set()


def max_tokens_field(model: str = "") -> str:
    """Как называется поле числа токенов у этого шлюза и этой модели.

    Обычно max_tokens. Но новые модели OpenAI его отвергают и просят
    max_completion_tokens — а серия таких отказов уводит ключ в паузу
    и превращается в 429, поэтому такое запоминаем на первый же раз.
    """
    return "max_completion_tokens" if model in _RENAMED_MAX_TOKENS else "max_tokens"


def translate_options(options: dict, model: str = "") -> dict:
    """Числа участника из вида Ollama в вид OpenAI.

    Пустые значения не отправляются совсем: у облачной модели своё значение
    по умолчанию, и «0» вместо «не задано» её бы только испортил.
    """
    source = options or {}
    result = {}
    for ollama_key, openai_key in settings.CLOUD_PARAM_KEYS:
        value = source.get(ollama_key)
        if value is None:
            continue
        if openai_key == "max_tokens":
            openai_key = max_tokens_field(model)
            result[openai_key] = value
            continue
        result[openai_key] = _within_cloud_limits(openai_key, value, model)

    if pass_extras():
        for extra in ("min_p", "top_k", "repeat_penalty"):
            if source.get(extra) is not None:
                result[extra] = source[extra]
    return result


def untranslated_options(options: dict) -> list:
    """Числа, которые на облако НЕ поедут: их в схеме OpenAI нет.

    Нужен затем, чтобы честно сказать об этом вслух: иначе «Циркуль» с min_p 0.4
    на облаке будет молча играть как обычная модель, и разница с локальной
    покажется свойством модели, а не потерянного числа.
    """
    if pass_extras():
        return []
    source = options or {}
    return [key for key in ("min_p", "top_k", "repeat_penalty", "num_ctx") if source.get(key) is not None]


# ── РАЗГОВОР ────────────────────────────────────────────────────────────────

# Облачные модели, отказавшиеся от нашего инструмента поиска (шлюз ответил
# 400 на запрос с tools, а без них ответил). Узнаём это на первом же ходу
# и больше не спрашиваем: серия таких отказов уводит ключ в паузу, и вместо
# реплик в ленте появлялись бы одни ошибки
_MODELS_WITHOUT_TOOLS = set()


def model_takes_tools(model: str) -> bool:
    """Можно ли этому облачному участнику отправлять инструмент поиска."""
    return model not in _MODELS_WITHOUT_TOOLS


def _tool_schema() -> list:
    """Описание инструмента поиска — то же, что уходит в Ollama.

    Без поля «default»: значение по умолчанию у нас и так пять (см. разбор
    вызова в ollama_api), а лишний ключ в схеме — ещё один повод для 400.
    """
    return [{
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Ищет информацию в интернете",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "max_results": {"type": "integer"},
                },
            },
        },
    }]


# В каком поле шлюз отдаёт размышления. У proxyapi это «reasoning» (проверено
# на gpt-5-nano и qwen3-next-thinking), у DeepSeek и vLLM — «reasoning_content»,
# у части движков — «thinking». Читаем все: имя поля — не принцип, а чей-то выбор.
# И к тому же «reasoning_details» — те же размышления, но разложенные по частям
_THINKING_KEYS = ("reasoning", "reasoning_content", "thinking", "reasoning_details")


# О каких числах уже сказали, что их пришлось подвинуть: за один спектакль
# одно и то же предупреждение повторялось бы на каждом ходу
_LIMIT_WARNED = set()


def _within_cloud_limits(field: str, value, model: str = ""):
    """Привести число к области вендора (и сказать вслух, если пришлось).

    Характеры придуманы для Ollama, где temperature 2.0 — это «Хаос», а
    presence_penalty 1.5 — «ничего не повторяй». Облачные модели обучены на
    своём диапазоне (у GLM, например, температура 0.01—0.99), и за его краем
    начинают сыпать случайными токенами — реплика превращается в мусор
    на трёх языках сразу. Поэтому число отдаётся границей, а не как есть;
    выключить это можно настройкой CLOUD_LIMIT_PARAMS.
    """
    limits = (settings.CLOUD_PARAM_LIMITS or {}).get(field) if limit_params() else None
    if not limits:
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    low, high = limits
    if low <= number <= high:
        return value
    better = min(high, max(low, number))
    if (model, field, number) not in _LIMIT_WARNED:
        _LIMIT_WARNED.add((model, field, number))
        print(f"  ⚠️  Облако ({bare_model_name(model)}): {field} {number:g} — вендор "
              f"столько не понимает, шлю {better:g}. Характеры рассчитаны на Ollama: "
              f"для облака выбирайте поспокойнее")
    return better


def _delta_text(delta: dict) -> str:
    """Кусок текста из потока: у части шлюзов он приходит списком частей."""
    return _text_of((delta or {}).get("content"))


def _text_of(piece) -> str:
    """Текст из значения: или строкой, или списком частей с полем «text»."""
    if isinstance(piece, list):
        return "".join(str(part.get("text", "")) for part in piece if isinstance(part, dict))
    return "" if piece is None else str(piece)


def thinking_text(delta: dict) -> str:
    """Размышления модели из куска ответа — то, что она говорит сама с собой.

    Это не реплика: в ленте им отведено своё место, а в историю и в промпт они
    не попадают — иначе следующая модель прочитала бы чужой черновик мыслей как
    сказанное вслух.
    """
    for key in _THINKING_KEYS:
        text = _text_of((delta or {}).get(key))
        if text:
            return text
    return ""


def _message_answer(result: dict) -> dict:
    """Обычный (не потоковый) ответ шлюза — в том же виде, что и поток."""
    choices = (result or {}).get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return {"error": "шлюз вернул ответ без choices"}
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        # Некоторые шлюзы отдают текст частями и без потока
        content = "".join(str(part.get("text", "")) for part in content
                          if isinstance(part, dict))
    return {"content": content, "tool_calls": message.get("tool_calls") or [],
            "thinking": thinking_text(message),
            "usage": (result or {}).get("usage") or {}}


def _read_answer(response) -> dict:
    """Прочитать обычный ответ шлюза и привести его к общему виду."""
    body = response.read().decode("utf-8")
    try:
        return _message_answer(json.loads(body))
    except ValueError:
        return {"error": f"шлюз ответил не данными, а текстом: {body[:200]}"}


def _accumulate_calls(calls: dict, delta: dict) -> None:
    """Собирает вызовы инструментов из потока: они приходят кусками.

    У OpenAI вызов разрезан на фрагменты с одним и тем же index, и имя
    с аргументами приходят частями строки. Прочитать фрагмент как готовый вызов
    нельзя: получился бы инструмент с половиной имени.
    """
    for piece in ((delta or {}).get("tool_calls") or []):
        if not isinstance(piece, dict):
            continue
        index = piece.get("index")
        index = 0 if index is None else index
        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if piece.get("id"):
            call["id"] = str(piece["id"])
        function = piece.get("function") or {}
        if function.get("name"):
            call["name"] += str(function["name"])
        if function.get("arguments"):
            call["arguments"] += str(function["arguments"])


def _glued_calls(calls: dict) -> list:
    """Склеенные куски — в тот же вид, что у обычного ответа."""
    result = []
    for index in sorted(calls):
        call = calls[index]
        if not call["name"]:
            continue
        try:
            arguments = json.loads(call["arguments"]) if call["arguments"] else {}
        except ValueError:
            arguments = {}
        result.append({"id": call["id"], "type": "function",
                       "function": {"name": call["name"], "arguments": arguments}})
    return result


def _read_stream(response, on_delta, on_thought=None, deadline: float = None) -> dict:
    """Читает поток шлюза и отдаёт ответ в том же виде, что и обычный.

    Строка потока — «data: {кусок}», конец — «data: [DONE]». Кусок текста
    сразу уходит в on_delta, а размышления — в on_thought: из этих кусков лента
    и печатает реплику на глазах. Второй аргумент у обоих — «начался новый
    ответ»: за один ход бывает несколько запросов (модель ответила без поиска,
    а после поиска отвечает заново), и тогда прежний текст больше не в счёт.

    deadline — до какого времени (time.monotonic) ходу позволено длиться.
    «Думающая» модель умеет зациклиться и молотить случайные токены полчаса:
    кусочки приходят исправно, поэтому CLOUD_TIMEOUT её не остановит, а спектакль
    всё стоит. Исчерпался срок — обрываем ход и оставляем сказанное (см. «cut»).

    Шлюз может и проигнорировать поток, ответив обычным JSON: разберём и его.
    Просить поток — не повод потерять реплику.
    """
    parts = []
    thoughts = []
    calls = {}
    usage = {}
    cut = False     # ход оборван по времени, а не кончился сам
    wants_thinking = on_thought is not None and show_thinking()
    whole = ""      # всё, что не похоже на поток: разберём целиком в конце

    for raw in response:
        # Срок проверяем перед каждой порцией: сказанное остаётся в parts,
        # и оборванный ход отдаёт то, что модель успела сказать
        if not whole and deadline is not None and time.monotonic() >= deadline:
            cut = True
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            continue                # пустые строки и «сердцебиения» шлюза
        if whole or not line.startswith("data:"):
            whole += line
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue                # нечитаемый кусок: остальное важнее
        usage = chunk.get("usage") or usage
        for choice in (chunk.get("choices") or []):
            delta = choice.get("delta") or choice.get("message") or {}
            if wants_thinking:
                thought = thinking_text(delta)
                if thought:
                    on_thought(thought, not thoughts)
                    thoughts.append(thought)
            piece = _delta_text(delta)
            if piece:
                on_delta(piece, not parts)
                parts.append(piece)
            _accumulate_calls(calls, delta)

    if whole:
        try:
            return _message_answer(json.loads(whole))
        except ValueError:
            return {"error": f"шлюз ответил не данными, а текстом: {whole[:200]}"}

    return {"content": "".join(parts), "tool_calls": _glued_calls(calls),
            "thinking": "".join(thoughts), "usage": usage, "cut": cut}


def chat(model: str, messages: list, options: dict = None, tool_choice: str = None,
         on_delta=None, on_thought=None) -> tuple:
    """Один ход облачной модели. Возвращает (текст, вызовы инструментов).

    Форма ответа — та же, что у Ollama-пути, поэтому весь остальной код
    (поиск в интернете, разбор ошибок, лента) работает без изменений. Ошибка
    приходит текстом в первом элементе: так же ведёт себя и Ollama-путь.

    on_delta — «получатель» ответа по кускам: пока модель говорит, ему достаётся
    очередная порция текста, а on_thought — порция размышлений (шлюз отдаёт их
    отдельным полем, см. thinking_text). Без них (или когда CLOUD_STREAM выключен)
    ответ приходит целиком, как раньше.
    """
    # Срок на весь ход считаем до запроса: он про ход целиком, вместе с ожиданием
    # шлюза, а не про один кусочек. Ноль значит «без предела»
    seconds = turn_limit()
    deadline = time.monotonic() + seconds if seconds > 0 else None

    # Тело запроса нарочно простое: модель, сообщения и stream. Всё остальное
    # (числа характеров, инструмент поиска, имена отправителей) добавляется только
    # если его включили в настройках — см. CLOUD_SEND_* в settings.py. Так облачный
    # участник сначала просто говорит, а лишние поводы для «400 Bad Request»,
    # от которых ключ уходит в паузу и отвечает 429, остаются за дверью
    streaming = stream_replies() and (on_delta is not None or on_thought is not None)
    payload = {
        "model": bare_model_name(model),
        "messages": openai_messages(messages, keep_names=send_message_names()),
        "stream": bool(streaming),
    }
    if send_params():
        payload.update(translate_options(options if options is not None else settings.OPTIONS,
                                        model=model))
    # Поля, на которые этот шлюз уже жаловался, в запрос больше не идут: иначе
    # каждый ход повторял бы один и тот же отказ, а ключ уходил бы в паузу
    for field in dropped_params(model):
        payload.pop(field, None)

    uses_tools = send_tools() and model_takes_tools(model)
    if uses_tools:
        payload["tools"] = _tool_schema()
        if tool_choice:
            # «any» у Ollama значит «обязан вызвать инструмент», у OpenAI — «required»
            payload["tool_choice"] = "required" if tool_choice == "any" else tool_choice

    def ask(body):
        """Один запрос — с потоком или без: разбор ответа выбирается здесь."""
        def read(response):
            if streaming:
                return _read_stream(response, on_delta, on_thought, deadline)
            return _read_answer(response)
        return _request("chat/completions", payload=body, method="POST", read=read)

    body = payload
    answer, error = ask(body)

    # 400 значит «запрос не понят». Причина почти всегда в нашем же поле, и она
    # узнаётся по словам самого шлюза — такое лечим (см. _eased_body). Кругов
    # нужно несколько: OpenAI-подобные вендоры называют поля по одному, и ход
    # с пятью числами характера требует до пяти запросов. Зато каждый убранный
    # ключ запоминается, и следующие ходы идут без отказов вовсе
    for _ in range(_EASE_ROUNDS):
        if getattr(error, "code", None) != 400:
            break
        eased = _eased_body(model, body, error)
        if eased is None:
            break
        body = eased
        answer, error = ask(body)

    # Метку «инструмент не принят» ставим только тогда, когда запрос без него
    # правда прошёл: иначе по 400 другой причины модель была бы помечена зря
    if not error and uses_tools and "tools" not in body:
        _MODELS_WITHOUT_TOOLS.add(model)
        print(f"  ℹ️  Облако: {bare_model_name(model)} не приняла инструмент поиска — "
              f"дальше говорю с ней без него")

    if error:
        print(f"  ⚠️  Облако ({bare_model_name(model)}): {error}")
        return f"[ОШИБКА: {error}]", []

    if answer.get("error"):
        problem = hide_key(answer["error"])
        print(f"  ⚠️  Облако ({bare_model_name(model)}): {problem}")
        return f"[ОШИБКА: {problem}]", []

    if answer.get("cut"):
        # Не молчим: иначе оборванная реплика выглядела бы свойством модели
        note = (f"ход длился дольше {seconds:g} с и оборван: модель зациклилась "
                f"и не перешла к ответу (CLOUD_TURN_LIMIT, 0 — без предела)")
        print(f"  ⚠️  Облако ({bare_model_name(model)}): {note}")
        if not (answer.get("content") or "").strip() and not answer.get("tool_calls"):
            return f"[ОШИБКА: {note}]", []

    usage = answer.get("usage") or {}
    if usage:
        print(f"  ☁️  {bare_model_name(model)}: токенов {usage.get('prompt_tokens', '?')} "
              f"+ {usage.get('completion_tokens', '?')}")

    return answer.get("content") or "", answer.get("tool_calls") or []


def fetch_models(force: bool = False, timeout: int = 20) -> tuple:
    """Список моделей, доступных на шлюзе. Возвращает (имена, текст ошибки).

    Тот же смысл, что у /api/tags в Ollama: без него в пульте нечего выбирать.
    """
    global _MODELS_CACHE
    if not force and _MODELS_CACHE["names"] \
            and time.monotonic() - _MODELS_CACHE["at"] < settings.CLOUD_MODELS_CACHE_TTL:
        return _MODELS_CACHE["names"], _MODELS_CACHE["error"]

    # Список моделей — не разговор: повторять его незачем, ответ нужен сейчас
    result, error = _request("models", timeout=timeout, retries=0)
    names = []
    if not error:
        for item in (result.get("data") or []):
            if isinstance(item, dict) and item.get("id"):
                names.append(str(item["id"]))
    _MODELS_CACHE = {"at": time.monotonic(), "names": names, "error": error}
    return names, error


def status() -> dict:
    """Состояние облака для раздела «00 · Готовность» и списка моделей."""
    if not is_configured():
        return {"configured": False, "base_url": base_url(),
                "models": [], "error": ""}
    models, error = fetch_models()
    return {"configured": True, "base_url": base_url(),
            "models": models, "error": error or ""}
