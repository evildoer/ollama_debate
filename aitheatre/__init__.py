"""AI Театр: пакет приложения.

Раньше всё лежало в одном файле на 4700 строк — сервер, страница, промпты,
расчёт видеопамяти и жребий характеров. Теперь это восемь модулей, по одному на
смысловой блок:

    settings.py    что правится руками: состав, характеры, параметры Ollama, пути
    deps.py        необязательные библиотеки: tiktoken, ddgs, markdown
    text.py        markdown, формулы и подсчёт токенов
    search.py      поиск в интернете (DuckDuckGo) с прокси и без него
    avatars.py     грим: поиск и скачивание картинок
    ollama_api.py  общение с Ollama и оценка «влезет ли модель в VRAM»
    show.py        спектакль: состав, характеры, сессия и поток дебатов
    page.py        страница: HTML, CSS и клиентский скрипт
    web.py         веб-сцена: Flask, Socket.IO, маршруты

Запуск не изменился: `python ollama_debate_web.py` или `python .` из корня проекта.

Этот файл — только для удобства чтения и подмен: он выкладывает наружу публичные
имена, чтобы работало `from aitheatre import DebateSession`. Никакой логики здесь
нет, и `web` намеренно не импортируется: поднимать Flask тому, кто пришёл за
одним расчётом токенов, незачем (`from aitheatre import web` — когда понадобится).
"""

import sys as _sys

# Логи с эмодзи не должны ронять программу: на консоли с кодировкой cp1251/ascii
# (русская Windows) печать падает с UnicodeEncodeError. Делается раньше всего
# остального: настройки печатают о себе уже при загрузке, а при перенаправлении
# вывода (`python ollama_debate_web.py > log.txt`) нужен ещё и line_buffering —
# иначе всё копится блоками и предупреждения видны только в конце.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

from . import avatars
from . import deps
from . import ollama_api
from . import page
from . import search
from . import settings
from . import show
from . import text

# Настройки, которые чаще всего правят и читают снаружи
from .settings import (
    AVATAR_DIR,
    CHARACTER_DRIFT,
    CHARACTER_PRESETS,
    DEFAULT_JUDGE_INSTRUCTION,
    DEFAULT_JUDGE_RULES,
    DEFAULT_STATIC_INSTRUCTIONS,
    MALE_NAMES,
    FEMALE_NAMES,
    OPTIONS,
    PARTICIPANTS,
    PER_PARTICIPANT_OPTION_KEYS,
    RANDOMIZE_CHARACTERS,
    SETTINGS_FILE,
    THINK_MODES,
    VRAM_SAFE_CTX_STEP,
)

# Текст: разметка реплик и бюджет контекста
from .text import (
    estimate_tokens,
    markdown_to_html,
    trim_history_by_tokens,
    trim_history_with_report,
)

# Грим и костюмы
from .avatars import (
    generate_avatar_for_participant,
    sanitize_avatar_name,
)

# Ollama: модели, параметры, память
from .ollama_api import (
    ask_model,
    check_models_available,
    check_vram_fit,
    estimated_kv_bytes,
    fetch_model_parameters,
    fetch_ollama_models,
    load_vram_measurements,
    models_problem_message,
    suggest_safe_ctx,
    unload_model,
)

# Спектакль: состав, характеры, сессия
from .show import (
    DebateSession,
    apply_cast_patch,
    build_new_cast,
    character_parameters,
    create_post,
    draw_character,
    forget_theatre_settings,
    load_theatre_settings,
    run_debate_thread,
    save_theatre_settings,
    session,
)

# Страница целиком (HTML + CSS + JS одной строкой)
from .page import HTML_TEMPLATE

__all__ = [
    "settings", "deps", "text", "search", "avatars", "ollama_api", "show", "page",
    "AVATAR_DIR", "CHARACTER_DRIFT", "CHARACTER_PRESETS", "DEFAULT_JUDGE_INSTRUCTION",
    "DEFAULT_JUDGE_RULES", "DEFAULT_STATIC_INSTRUCTIONS", "MALE_NAMES", "FEMALE_NAMES",
    "OPTIONS", "PARTICIPANTS", "PER_PARTICIPANT_OPTION_KEYS", "RANDOMIZE_CHARACTERS",
    "SETTINGS_FILE", "THINK_MODES", "VRAM_SAFE_CTX_STEP",
    "estimate_tokens", "markdown_to_html", "trim_history_by_tokens",
    "trim_history_with_report",
    "generate_avatar_for_participant", "sanitize_avatar_name",
    "ask_model", "check_models_available", "check_vram_fit", "estimated_kv_bytes",
    "fetch_model_parameters", "fetch_ollama_models", "load_vram_measurements",
    "models_problem_message", "suggest_safe_ctx", "unload_model",
    "DebateSession", "apply_cast_patch", "build_new_cast", "character_parameters",
    "create_post", "draw_character", "forget_theatre_settings", "load_theatre_settings",
    "run_debate_thread", "save_theatre_settings", "session", "HTML_TEMPLATE",
]
