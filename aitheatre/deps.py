"""Необязательные библиотеки: tiktoken, ddgs, markdown.Их может не быть — приложение обязано работать и без них, поэтому проверка
«есть ли библиотека» живёт отдельно от кода, а модули спрашивают флаги
(deps.MARKDOWN_AVAILABLE и подобные).

TIKTOKEN_CACHE_DIR задаётся до первого импорта tiktoken, поэтому это отдельный
модуль, а не функция. Настройка кодировки вывода сделана ещё раньше — в самом
начале aitheatre/__init__.py: печать начинается уже при загрузке настроек.
"""

import os

from .settings import PROJECT_ROOT


# Кэш BPE-файлов tiktoken держим рядом с проектом. По умолчанию tiktoken
# складывает их во временную папку системы, а её могут очистить - тогда при
# следующем запуске он снова полезет в сеть.
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(PROJECT_ROOT / ".tiktoken_cache"))

# Подсчёт токенов через tiktoken, если он есть и смог инициализироваться.
TIKTOKEN_AVAILABLE = False
TIKTOKEN_ENCODER = None
try:
    import tiktoken
except ImportError:
    print("⚠️  tiktoken не установлен - используется приближённый подсчёт")
    print("   Установите: pip install tiktoken")
else:
    try:
        # get_encoding() при пустом кэше идёт в сеть за файлом BPE,
        # поэтому ловим любое исключение, а не только ImportError.
        TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
        TIKTOKEN_AVAILABLE = True
        print("✅ tiktoken доступен - точный подсчёт токенов")
    except Exception as e:
        print(f"⚠️  tiktoken не удалось инициализировать ({e})")
        print("   Используется приближённый подсчёт токенов")

# Поиск в интернете
try:
    from ddgs import DDGS
    SEARCH_AVAILABLE = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        SEARCH_AVAILABLE = True
    except ImportError:
        SEARCH_AVAILABLE = False
        print("⚠️  ddgs не установлен. Поиск в интернете отключён.")
        print("   Установите: pip install ddgs")

# Markdown для форматирования постов
try:
    import markdown
    MARKDOWN_AVAILABLE = True
except ImportError:
    MARKDOWN_AVAILABLE = False
