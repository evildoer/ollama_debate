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
    {"model": "qwen/qwen3.7-flash", "messages": [...], "temperature": 0.8, ...}

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


# ── ЧТО СЧИТАЕТСЯ ОБЛАЧНОЙ МОДЕЛЬЮ ──────────────────────────────────────────

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

def api_key() -> str:
    """Ключ из переменной окружения, иначе из settings. Пусто — не настроено.

    Ключ часто копируют из документации вместе с кавычками: «Bearer sk-...»
    или “sk-...” — лишнее отрезаем, иначе шлюз ответит 401, и никто не поймёт почему.
    """
    from_env = os.environ.get(settings.CLOUD_KEY_ENV, "") if settings.CLOUD_KEY_ENV else ""
    key = (from_env or settings.CLOUD_API_KEY or "").strip()
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
        return ("ключ облачного шлюза не задан: впишите его в settings.CLOUD_API_KEY "
                f"или положите в переменную окружения {settings.CLOUD_KEY_ENV}")
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


def _url(path: str) -> str:
    return f"{settings.CLOUD_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _request(path: str, payload=None, method: str = "GET", timeout: int = None):
    """Один запрос к шлюзу. Возвращает (данные, текст ошибки) — без исключений.

    Ключа в тексте ошибки не бывает: сюда его подставляет только этот метод,
    а наружу уходит уже прогнанным через hide_key.
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
    try:
        with urllib.request.urlopen(request, timeout=timeout or settings.CLOUD_TIMEOUT) as response:
            body = response.read().decode("utf-8")
        return json.loads(body), None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            detail = ""
        finally:
            e.close()   # иначе Python ругается на не закрытый ответ ошибки
        return None, hide_key(f"шлюз ответил HTTP {e.code} {e.reason}: {detail}".strip())
    except Exception as e:
        return None, hide_key(f"шлюз недоступен по адресу {settings.CLOUD_BASE_URL}: {e}")


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
    payload = {
        "model": bare_model_name(model),
        "messages": messages,
        "stream": False,
    }
    payload.update(translate_options(options if options is not None else settings.OPTIONS))

    if settings.ENABLE_SEARCH:
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

    result, error = _request("models", timeout=timeout)
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
        return {"configured": False, "base_url": settings.CLOUD_BASE_URL,
                "models": [], "error": ""}
    models, error = fetch_models()
    return {"configured": True, "base_url": settings.CLOUD_BASE_URL,
            "models": models, "error": error or ""}
