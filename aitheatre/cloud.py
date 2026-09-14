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

Про ключ. Он берётся из settings (или переменной окружения) и уходит ТОЛЬКО
в этом заголовке. Ни в текст ответа, ни в сообщения об ошибках, ни на страницу
он не попадает: вычистить его из чужого текста — обязанность этого модуля,
потому что ошибку шлюза мы показываем прямо в ленте спектакля.
"""

import json
import os
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


def _load_dotenv() -> None:
    """Прочитать .env из корня проекта в окружение процесса.

    setdefault — настоящая переменная окружения важнее файла: ключ, заданный
    в системе, файл не перекроет.
    """
    dotenv_file = settings.PROJECT_ROOT / ".env"
    if not dotenv_file.is_file():
        return
    try:
        for key, value in _parse_dotenv(dotenv_file.read_text(encoding="utf-8")).items():
            os.environ.setdefault(key, value)
    except OSError:
        pass    # файл пропал в момент чтения — ничего страшного


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


def plain_messages(messages: list) -> list:
    """Сообщения в самой простой форме: только «role» и «content».

    Зачем убирать имена. По схеме OpenAI поле «name» — это латиница, цифры,
    дефис и подчёркивание, а у нас там русские имена: шлюз честно отвечает
    «400 Bad Request», и хорошо, если не на каждом ходу. Двадцать таких
    ответов за минуту переводят ключ в паузу, и на всё сыплется 429 —
    а причина остаётся невидимой, потому что имя в теле никто не заподозрит.

    Кто говорит, и так видно из текста: «Имя говорит: …» дописывается в show.
    """
    return [{"role": str(message.get("role") or "user"), "content": message.get("content") or ""}
            for message in (messages or []) if isinstance(message, dict)]


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
_HTTP_HINTS = {
    400: "шлюз не понял запрос. Обычно дело в лишнем или неверном поле в теле: "
         "попробуйте выключить числа характеров и инструменты поиска (CLOUD_SEND_*)",
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


def _wait_before_retry(attempt: int, headers) -> float:
    """Сколько ждать до следующей попытки при 429.

    Своя задержка растёт (1, 2, 4 секунды), но если шлюз прислал Retry-After —
    слушаем его. Огромные значения (пауза по ключу бывает и в 12 часов) в сон
    превращать нельзя: спектакль на это время не остановишь, поэтому берём
    не больше сигнала «подожди немного»
    """
    delays = tuple(settings.CLOUD_RETRY_DELAYS or ())
    wait = float(delays[attempt]) if attempt < len(delays) else 0.0
    try:
        offered = float((headers or {}).get("Retry-After"))
        if 0 < offered <= 15:
            wait = max(wait, offered)
    except (TypeError, ValueError):
        pass
    return wait


def _request(path: str, payload=None, method: str = "GET", timeout: int = None,
             retries: int = None):
    """Один запрос к шлюзу. Возвращает (данные, текст ошибки) — без исключений.

    Ключа в тексте ошибки не бывает: сюда его подставляет только этот метод,
    а наружу уходит уже прогнанным через hide_key.

    Повторяется только 429 и только с задержкой: остальные 4xx — про сам запрос,
    и повторять их бессмысленно (и вредно: серия ошибок уводит ключ в паузу).
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
    delays = tuple(settings.CLOUD_RETRY_DELAYS or ())
    attempts = (len(delays) if retries is None else max(0, retries)) + 1

    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=timeout or settings.CLOUD_TIMEOUT) as response:
                body = response.read().decode("utf-8")
            try:
                return json.loads(body), None
            except ValueError:
                return None, hide_key(f"шлюз ответил не данными, а текстом: {body[:200]}")
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
            return None, hide_key(_error_text(e.code, e.reason, detail))
        except Exception as e:
            return None, hide_key(f"шлюз недоступен по адресу {base_url()}: {e}")

    return None, "шлюз не ответил"      # недостижимо: попытки кончаются возвратом


# ── ПЕРЕВОД ЧИСЕЛ УЧАСТНИКА ─────────────────────────────────────────────────

def translate_options(options: dict) -> dict:
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
        result[openai_key] = value

    if settings.CLOUD_PASS_OLLAMA_EXTRAS:
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
    if settings.CLOUD_PASS_OLLAMA_EXTRAS:
        return []
    source = options or {}
    return [key for key in ("min_p", "top_k", "repeat_penalty", "num_ctx") if source.get(key) is not None]


# ── РАЗГОВОР ────────────────────────────────────────────────────────────────

def _tool_schema() -> list:
    """Описание инструмента поиска — то же, что уходит в Ollama."""
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
                    "max_results": {"type": "integer", "default": 5},
                },
            },
        },
    }]


def chat(model: str, messages: list, options: dict = None, tool_choice: str = None) -> tuple:
    """Один ход облачной модели. Возвращает (текст, вызовы инструментов).

    Форма ответа — та же, что у Ollama-пути, поэтому весь остальной код
    (поиск в интернете, разбор ошибок, лента) работает без изменений. Ошибка
    приходит текстом в первом элементе: так же ведёт себя и Ollama-путь.
    """
    # Тело запроса нарочно простое: модель, сообщения и stream. Всё остальное
    # (числа характеров, инструмент поиска, имена отправителей) добавляется только
    # если его включили в настройках — см. CLOUD_SEND_* в settings.py. Так облачный
    # участник сначала просто говорит, а лишние поводы для «400 Bad Request»,
    # от которых ключ уходит в паузу и отвечает 429, остаются за дверью
    payload = {
        "model": bare_model_name(model),
        "messages": list(messages or []) if send_message_names() else plain_messages(messages),
        "stream": False,
    }
    if send_params():
        payload.update(translate_options(options if options is not None else settings.OPTIONS))

    if send_tools():
        payload["tools"] = _tool_schema()
        if tool_choice:
            # «any» у Ollama значит «обязан вызвать инструмент», у OpenAI — «required»
            payload["tool_choice"] = "required" if tool_choice == "any" else tool_choice

    result, error = _request("chat/completions", payload=payload, method="POST")
    if error:
        print(f"  ⚠️  Облако ({bare_model_name(model)}): {error}")
        return f"[ОШИБКА: {error}]", []

    choices = result.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return "[ОШИБКА: шлюз вернул ответ без choices]", []

    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        # Некоторые шлюзы отдают текст частями, как в потоковом режиме
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

    usages = result.get("usage") or {}
    if usages:
        print(f"  ☁️  {bare_model_name(model)}: токенов {usages.get('prompt_tokens', '?')} "
              f"+ {usages.get('completion_tokens', '?')}")

    return content, (message.get("tool_calls") or [])


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
