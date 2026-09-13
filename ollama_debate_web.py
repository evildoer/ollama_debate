#!/usr/bin/env python3
"""
Ollama AI Debate - Современный веб-интерфейс с WebSocket
=========================================================

Установка:
    pip install flask flask-socketio ddgs markdown

Запуск:
    python ollama_debate_web.py
"""

import urllib.request
import urllib.error
import json
import logging
import subprocess
import time
import threading
import webbrowser
import os
import sys
import hashlib
import ssl
import re
import random
import traceback
from pathlib import Path
from urllib.parse import quote
from flask import Flask, render_template_string, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit

# Логи с эмодзи не должны ронять программу: при перенаправлении вывода
# или на консоли с кодировкой cp1251/ascii печать падает с UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        # line_buffering: при перенаправлении вывода (`python ollama_debate_web.py > log.txt`)
        # иначе всё копится блоками и предупреждения видны только в конце
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

# Кэш BPE-файлов tiktoken держим рядом с проектом. По умолчанию tiktoken
# складывает их во временную папку системы, а её могут очистить - тогда при
# следующем запуске он снова полезет в сеть.
os.environ.setdefault(
    "TIKTOKEN_CACHE_DIR",
    str(Path(__file__).resolve().parent / ".tiktoken_cache")
)

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

# ╔══════════════════════════════════════════════════════════╗
# ║                    НАСТРОЙКИ ПРОКСИ                      ║
# ╚══════════════════════════════════════════════════════════╝

ENABLE_PROXY = True  # Включить/выключить использование прокси
PROXY = "http://127.0.0.1:10808"  # Адрес прокси-сервера

if ENABLE_PROXY and PROXY:
    print(f"🌐 Используется прокси: {PROXY}")
else:
    PROXY = None

# ╔══════════════════════════════════════════════════════════╗
# ║                    НАСТРОЙКИ УЧАСТНИКОВ                  ║
# ╚══════════════════════════════════════════════════════════╝

PARTICIPANTS = [
    {"model": "r1"},
    {"model": "g1"},
    {"model": "q1"},
    {"model": "q1", "is_judge": True},  # Судья - оценивает выступления
    {"model": "human", "is_moderator": True},  # Модератор (вы)
]

# ╔══════════════════════════════════════════════════════════╗
# ║                    ДРУГИЕ НАСТРОЙКИ                      ║
# ╚══════════════════════════════════════════════════════════╝

ENABLE_THINKING = True
ENABLE_SEARCH = True
MIN_SEARCHES = 0
ENABLE_AVATAR_GENERATION = True
UNLOAD_AFTER_DEBATE = True
# Держать в памяти только текущего говорящего: иначе Ollama ~5 минут после
# ответа не выгружает модель, и к третьему персонажу в памяти оказываются
# все сразу (у моделей ниже это ~7 + 7 + 6 ГБ + контекст).
UNLOAD_OTHER_MODELS = True
MODELS_CACHE_TTL = 5  # секунд: на столько кэшируется список моделей Ollama

# Проверка, влезет ли модель в видеопамять. Считаем «нужно» = веса + KV-кэш
# и сравниваем с VRAM_SAFETY_FACTOR от всего объёма: остаток оставляем буферам
# вычислений и рабочему столу.
VRAM_SAFETY_FACTOR = 0.9
KV_CACHE_BYTES_PER_ELEM = 2  # Ollama держит KV-кэш в f16 по умолчанию
VRAM_SAFE_MIN_CTX = 2048     # ниже этого контекста спектакль уже не работает
VRAM_SAFE_CTX_STEP = 1024    # предлагаемые num_ctx округляем до этого шага

# Оптимизация GPU
OPTIONS = {
    "num_ctx": 16384,
    "num_predict": 8192,  # Увеличено для очень длинных ответов
    "num_thread": 0,
    "num_gpu": 999,
}

# Безопасный запас токенов (не используем весь контекст)
CONTEXT_SAFETY_MARGIN = 500  # токенов

# Кэш для хранения информации о поддержке tools моделями
MODELS_TOOLS_SUPPORT = {}  # {"model_name": True/False}

# Кэш списка скачанных моделей Ollama: {"at": monotonic, "models": {имя: размер}, "error": str}
_OLLAMA_MODELS_CACHE = {"at": 0.0, "models": {}, "error": None}

# Кэш моделей, загруженных в память: {"at": monotonic, "models": [...], "error": str}
_LOADED_MODELS_CACHE = {"at": 0.0, "models": [], "error": None}
LOADED_MODELS_CACHE_TTL = 1.5  # секунд

# Кэш объёма видеопамяти (nvidia-smi): {"at": monotonic, "info": {total, used}}
_GPU_MEMORY_CACHE = {"at": 0.0, "info": {}}
GPU_MEMORY_CACHE_TTL = 10  # секунд

# Метаданные моделей (/api/show): {имя: model_info}
_MODEL_INFO_CACHE = {}

# Измеренные размеры моделей в памяти: {имя: {"ctx", "size", "size_vram"}}.
# Файл рядом с проектом, чтобы после перезапуска оценка была точной, а не только
# по размеру файла модели.
_VRAM_MEASUREMENTS = {}
VRAM_MEASUREMENTS_FILE = Path(__file__).resolve().parent / ".vram_cache.json"

# Настройки, которые не должны теряться при перезапуске (правила судьи)
SETTINGS_FILE = Path(__file__).resolve().parent / ".theatre_settings.json"
# Чтобы не повторять одно и то же предупреждение на каждом опросе статуса
_RAM_SPILL_WARNED = set()

# Кэш для хранения URL аватаров по ключам (чтобы не искать повторно)
AVATAR_URL_CACHE = {}  # {"avatar_keywords": "image_url"}

# Дефолтные правила общения с плейсхолдерами
# Плейсхолдеры: {ИМЯ}, {СОБЕСЕДНИКИ}, {ТЕМА}
DEFAULT_STATIC_INSTRUCTIONS = [
    'Ты — {ИМЯ}.',
    'Ты участвуешь в диалоге вместе с: {СОБЕСЕДНИКИ}.',
    'Тема обсуждения: "{ТЕМА}".',
    'ИГРАЙ ЭТУ РОЛЬ ОТ ПЕРВОГО ЛИЦА (Я, МНЕ, МОЁ).',
    'ОБРАЩАЙСЯ к собеседникам по именам когда отвечаешь на их реплики.',
    'ГОВОРИ О СЕБЕ В ПЕРВОМ ЛИЦЕ. Можешь описывать свои действия в *звёздочках*.',
    'Учитывай всё что говорили другие участники и реагируй на их слова.',
    'Отвечай на русском языке.',
    'КРИТИЧЕСКИ ВАЖНО: Пиши МАКСИМУМ 4-5 предложений. Будь лаконичным.',
    'Используй поиск в интернете для фактологических утверждений.',
    'При поиске НЕ указывай годы.'
]

# Правила для роли судьи: их можно менять прямо в интерфейсе
# (кнопка «Редактировать инструкции и руководства» на ходу режиссёра)
DEFAULT_JUDGE_RULES = [
    'Ты — {ИМЯ}, независимый судья этого спора.',
    'Тема обсуждения: "{ТЕМА}".',
    'Ты оцениваешь выступления участников: {СОБЕСЕДНИКИ}.',
    'Отвечай на русском языке.',
    'Будь объективным и кратким.'
]

# Дефолтная инструкция для судьи
DEFAULT_JUDGE_INSTRUCTION = (
    'Ты — СТРОГИЙ судья этого спора. Твоя задача — объективно и критически оценить выступления участников. '
    'НЕ реагируй на обращения к тебе от других участников. '
    'Ты не участвуешь в дискуссии, а только оцениваешь её. '
    'ОЦЕНИВАЙ ОТНОСИТЕЛЬНО: сравнивай участников между собой, не ставь всем высокие оценки. '
    'Критерии оценки (будь строгим!): '
    '- 1-2 балла: поверхностные аргументы, отсутствие конкретики, уход от темы '
    '- 3 балла: средняя аргументация, есть факты но мало анализа '
    '- 4 балла: хорошая аргументация, конкретные примеры, логичные выводы '
    '- 5 баллов: выдающаяся аргументация, уникальные insights, безупречная логика '
    'ВАЖНО: Не все должны получать 4-5 баллов! Распределяй оценки: кто-то 2-3, кто-то 4, максимум один 5. '
    'Для каждого участника укажи: '
    '1) Краткое содержание его речи (1-2 предложения) '
    '2) Оценку от 1 до 5 баллов с подробным обоснованием '
    'Формат ответа: '
    '**Имя участника** — X/5 баллов '
    'Краткое содержание: ... '
    'Обоснование оценки: ... (критически!) '
    'В конце дай общее резюме раунда: кто выступил лучше/хуже и почему (3-4 предложения).'
)

# ============================================================
# СЛУЧАЙНЫЕ ИМЕНА И ЭМОДЗИ ДЛЯ ПЕРСОНАЖЕЙ
# ============================================================

MALE_NAMES = [
    "Алексей", "Борис", "Виктор", "Григорий", "Дмитрий",
    "Евгений", "Фёдор", "Георгий", "Иван", "Кирилл",
    "Леонид", "Максим", "Николай", "Олег", "Павел",
    "Роман", "Сергей", "Тимофей", "Ульян", "Филипп",
    "Харитон", "Эдуард", "Юрий", "Ярослав", "Артём",
    "Владимир", "Геннадий", "Денис", "Игорь", "Константин"
]

FEMALE_NAMES = [
    "Анастасия", "Белла", "Валентина", "Галина", "Дарья",
    "Екатерина", "Жанна", "Зинаида", "Ирина", "Ксения",
    "Лариса", "Мария", "Наталья", "Ольга", "Полина",
    "Раиса", "Светлана", "Татьяна", "Ульяна", "Феодосия",
    "Харита", "Элина", "Юлия", "Ярослава", "Алина",
    "Варвара", "Галина", "Диана", "Елизавета", "Кристина"
]

AVATAR_EMOJIS = [
    "🎭", "🎪", "🎨", "🎬", "🎯", "🎲", "🎸", "🎺",
    "🦊", "🐺", "🦁", "🐯", "🦅", "🐉", "🦄", "🐙",
    "🧙", "🧝", "🧛", "🧜", "🧚", "🦹", "🦸", "🥷",
    "👨‍🎓", "👩‍🎓", "👨‍🔬", "👩‍🔬", "👨‍🎨", "👩‍🎨",
    "👨‍💻", "👩‍💻", "👨‍🚀", "👩‍🚀", "🕵️", "👮",
    "🤴", "👸", "🤵", "👰", "🧑‍🎤", "🧑‍🚀"
]

PROFESSIONS = [
    "философ", "учёный", "писатель", "художник", "музыкант",
    "архитектор", "инженер", "врач", "юрист", "программист",
    "детектив", "журналист", "повар", "пилот", "космонавт",
    "учитель", "психолог", "экономист", "историк", "биолог",
    "физик", "математик", "химик", "астроном", "геолог",
    "режиссёр", "актёр", "продюсер", "сценарист", "оператор",
    "дизайнер", "фотограф", "модельер", "стилист", "визажист",
    "бизнесмен", "банкир", "трейдер", "маркетолог", "аналитик",
    "политик", "дипломат", "военный", "полицейский", "спасатель",
    "фермер", "рыбак", "охотник", "путешественник", "исследователь"
]

# Имена, эмодзи и профессии раздаются в /api/participants - без повторов внутри
# одного ответа.

# ============================================================
# ПАПКА ДЛЯ АВАТАРОВ
# ============================================================

AVATAR_DIR = Path("avatars")

def setup_avatar_dir():
    if not AVATAR_DIR.exists():
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)

def compute_file_checksum(filepath: Path) -> str:
    return hashlib.md5(filepath.read_bytes()).hexdigest()[:8]

def compute_data_checksum(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()[:8]

def sanitize_avatar_name(keywords: str) -> str:
    """Имя файла аватара: кириллица сохраняется, остальные символы заменяются на '_'."""
    return re.sub(r'[^a-zа-яё0-9_]', '_', keywords.lower().replace(' ', '_'), flags=re.IGNORECASE)

def proxy_modes() -> list:
    """Порядок сетевых попыток: сначала через прокси (если включён), затем напрямую."""
    return [PROXY, None] if PROXY else [None]

# ============================================================
# ПОИСК И СКАЧИВАНИЕ ИЗОБРАЖЕНИЙ
# ============================================================

def ddgs_search(kind: str, method: str, query: str, max_results: int = 5) -> tuple:
    """
    Ищет через ddgs: 3 попытки через прокси (если включён), затем 3 попытки напрямую.
    Возвращает (results, last_error).
    """
    last_error = None
    
    for proxy in proxy_modes():
        mode = f"через прокси {proxy}" if proxy else "без прокси"
        if proxy is None and PROXY:
            print(f"  🔄 {kind}: прокси не помог, пробую без прокси...")
        
        for attempt in range(3):
            try:
                ddgs_kwargs = {"proxy": proxy} if proxy else {}
                with DDGS(**ddgs_kwargs) as ddgs:
                    results = list(getattr(ddgs, method)(
                        query,
                        safesearch="off",
                        max_results=max_results
                    ))
                
                if proxy is None and PROXY:
                    print(f"  ✅ {kind} без прокси успешен!")
                return results, None
            except Exception as e:
                last_error = e
                if attempt < 2:
                    print(f"  ⚠️  {kind} ({mode}): попытка {attempt + 1} не удалась: {e}, пробую ещё раз...")
                    time.sleep(2)
                else:
                    print(f"  ⚠️  {kind} ({mode}): ошибка после 3 попыток: {e}")
    
    return [], last_error

def search_images(query: str, max_results: int = 5) -> str:
    if not SEARCH_AVAILABLE:
        return json.dumps([])
    
    results, _ = ddgs_search("Поиск изображений", "images", query, max_results)
    image_urls = [r.get('image', '') for r in results if r.get('image')]
    return json.dumps(image_urls)

def _save_avatar_image(image_data: bytes, base_name: str) -> str:
    """Сохраняет картинку в /avatars, переиспользуя файл с той же контрольной суммой."""
    checksum = compute_data_checksum(image_data)
    
    for existing_file in AVATAR_DIR.glob(f"{base_name}*.jpg"):
        if compute_file_checksum(existing_file) == checksum:
            return str(existing_file)
    
    filepath = AVATAR_DIR / f"{base_name}_{checksum}.jpg"
    filepath.write_bytes(image_data)
    return str(filepath)

def _build_image_opener(proxy_url):
    """Opener для картинок: без проверки SSL, через прокси или напрямую."""
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    # Пустой ProxyHandler({}) означает "игнорировать прокси из переменных окружения"
    proxy_handler = (
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url else urllib.request.ProxyHandler({})
    )
    return urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ssl_context))

def download_image_with_checksum(url: str, base_name: str) -> str:
    setup_avatar_dir()
    
    # Кодируем URL для поддержки не-ASCII символов (кириллица и т.д.)
    encoded_url = quote(url, safe=':/?&=#%@!~')
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
    }
    
    max_retries = 3
    for proxy in proxy_modes():
        mode = f"через прокси {proxy}" if proxy else "без прокси"
        if proxy is None and PROXY:
            print(f"  🔄 Прокси не помог, пробую скачать без прокси...")
        
        opener = _build_image_opener(proxy)
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(encoded_url, headers=headers)
                with opener.open(req, timeout=60) as response:
                    image_data = response.read()
                
                filepath = _save_avatar_image(image_data, base_name)
                if proxy is None and PROXY:
                    print(f"  ✅ Скачивание без прокси успешно!")
                return filepath
                
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"  ⚠️  Попытка {attempt + 1} скачать ({mode}) не удалась: {e}, пробую ещё раз...")
                    time.sleep(3)
                else:
                    print(f"  ⚠️  Не удалось скачать изображение ({mode}) после {max_retries} попыток: {e}")
    
    return ""

def generate_avatar_for_participant(participant: dict) -> str:
    print(f"🎭 Генерация аватара для участника: {participant}")
    if not ENABLE_AVATAR_GENERATION or not SEARCH_AVAILABLE:
        print(f"⚠️  Генерация аватаров отключена или поиск недоступен")
        return ""
    
    display_name = participant["display_name"]
    avatar_keywords = participant.get("avatar_keywords", display_name)
    print(f"🔑 Ключевые слова для аватара: {avatar_keywords}")
    
    # Проверяем кэш URL аватаров - если уже искали этот запрос, используем сохранённый URL
    if avatar_keywords in AVATAR_URL_CACHE:
        cached_url = AVATAR_URL_CACHE[avatar_keywords]
        print(f"🎨 Подготовка грима для: '{avatar_keywords}' (из кэша)")
        
        # Создаём безопасное имя файла, сохраняя кириллицу
        base_name = sanitize_avatar_name(avatar_keywords)
        
        # Пробуем скачать по закэшированному URL
        filepath = download_image_with_checksum(cached_url, base_name)
        if filepath:
            filename = Path(filepath).name
            print(f"  ✅ Грим готов: {filename}")
            return f"/avatars/{filename}"
        else:
            # Если не удалось скачать, удаляем из кэша и пробуем поиск заново
            del AVATAR_URL_CACHE[avatar_keywords]
            print(f"  ⚠️  Не удалось скачать из кэша, пробую поиск заново...")
    
    # Если не в кэше или не удалось скачать - делаем поиск
    print(f"🎨 Подготовка грима для: '{avatar_keywords}'")
    image_urls_json = search_images(avatar_keywords, max_results=5)
    
    try:
        image_urls = json.loads(image_urls_json)
    except:
        image_urls = []
    
    if image_urls:
        # Сохраняем первый URL в кэш
        AVATAR_URL_CACHE[avatar_keywords] = image_urls[0]
        
        # Создаём безопасное имя файла, сохраняя кириллицу
        base_name = sanitize_avatar_name(avatar_keywords)
        
        filepath = download_image_with_checksum(image_urls[0], base_name)
        if filepath:
            filename = Path(filepath).name
            print(f"  ✅ Грим готов: {filename}")
            return f"/avatars/{filename}"
    
    print(f"  ⚠️  Грим не найден")
    return ""

# ============================================================
# MARKDOWN
# ============================================================

def markdown_to_html(text: str) -> str:
    if not MARKDOWN_AVAILABLE:
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
        text = re.sub(r'~~(.+?)~~', r'<del>\1</del>', text)
        text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
        text = text.replace('\n', '<br>')
        return text
    
    return markdown.markdown(text, extensions=['nl2br'])

# ============================================================
# FLASK APP + SOCKET.IO
# ============================================================

app = Flask(__name__)
app.config['SECRET_KEY'] = 'debate-secret-key-change-in-production'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Отключаем логирование GET запросов к /api/status чтобы не засорять консоль
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)

# Единый источник правды о ходе спектакля - глобальный экземпляр DebateSession,
# объявленный ниже после определения класса.
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_URL = f"{OLLAMA_BASE_URL}/api/chat"
OLLAMA_GENERATE_URL = f"{OLLAMA_BASE_URL}/api/generate"
OLLAMA_SHOW_URL = f"{OLLAMA_BASE_URL}/api/show"
OLLAMA_TAGS_URL = f"{OLLAMA_BASE_URL}/api/tags"
OLLAMA_PS_URL = f"{OLLAMA_BASE_URL}/api/ps"

def estimate_tokens(text: str) -> int:
    """
    Подсчёт количества токенов в тексте.
    Если tiktoken доступен - точный подсчёт, иначе приближённая оценка.
    """
    if TIKTOKEN_AVAILABLE and TIKTOKEN_ENCODER:
        # Точный подсчёт через tiktoken
        return len(TIKTOKEN_ENCODER.encode(text))
    else:
        # Fallback: приближённая оценка. Кириллица в cl100k режется примерно по 2
        # символа на токен, латиница и пунктуация - по 4. Лучше переоценить:
        # заниженный подсчёт ведёт к переполнению контекста модели.
        cyrillic = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")
        return max(1, cyrillic // 2 + (len(text) - cyrillic) // 4)

def trim_history_by_tokens(messages: list, system_prompt_tokens: int) -> list:
    """
    Обрезает историю сообщений на основе подсчёта токенов.
    Возвращает обрезанный список сообщений.
    """
    # Вычисляем доступное пространство для истории
    available_tokens = max(
        0,
        OPTIONS["num_ctx"] - OPTIONS["num_predict"] - CONTEXT_SAFETY_MARGIN - system_prompt_tokens
    )
    
    # Подсчитываем токены в каждом сообщении
    messages_with_tokens = []
    for msg in messages:
        content = msg.get("content", "")
        tokens = estimate_tokens(content)
        messages_with_tokens.append((msg, tokens))
    
    # Подсчитываем общие токены
    total_tokens = sum(tokens for _, tokens in messages_with_tokens)
    
    # Если вписываемся - возвращаем всё
    if total_tokens <= available_tokens:
        print(f"  📊 История: {total_tokens} токенов (доступно: {available_tokens}) ✅")
        return messages
    
    # Обрезаем старые сообщения, пока не влезем
    print(f"  📊 История: {total_tokens} токенов (доступно: {available_tokens}) ⚠️ Обрезка...")
    
    trimmed_messages = []
    current_tokens = 0
    
    # Идём с конца (новые сообщения важнее)
    for msg, tokens in reversed(messages_with_tokens):
        if current_tokens + tokens <= available_tokens:
            trimmed_messages.insert(0, msg)
            current_tokens += tokens
        else:
            break
    
    removed_count = len(messages) - len(trimmed_messages)
    print(f"  ✂️  Удалено {removed_count} сообщений, осталось {len(trimmed_messages)} ({current_tokens} токенов)")
    
    return trimmed_messages

def check_model_tools_support(model: str) -> bool:
    """
    Проверяет поддержку tools моделью через API /api/show.
    Возвращает True если модель поддерживает tools, False иначе.
    """
    try:
        data = {"name": model}
        req = urllib.request.Request(
            OLLAMA_SHOW_URL,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
            capabilities = result.get("capabilities", [])
            return "tools" in capabilities
    except Exception as e:
        print(f"  ⚠️  Ошибка проверки capabilities: {e}")
        return False

# ============================================================
# ПРОВЕРКА, ЧТО МОДЕЛИ ИЗ PARTICIPANTS УСТАНОВЛЕНЫ В OLLAMA
# ============================================================

def model_is_installed(model: str, available: set) -> bool:
    """
    Есть ли модель в Ollama. «r1» считается установленной, если в Ollama есть
    «r1:latest» - этот тег Ollama подставляет сама.
    Если тег указан явно («q1:q4_K_M»), нужно ТОЧНОЕ совпадение: иначе модель,
    которой нет, прошла бы проверку, и спектакль упал бы уже на сцене.
    По той же причине нельзя считать «q1» и «q1:q4_K_M» одной моделью: у них
    разные файлы, и выгрузка одной не выгружает другую.
    """
    if not model:
        return False
    if model in available:
        return True
    
    if ":" in model:
        return False
    
    return f"{model}:latest" in available

def fetch_ollama_models(force: bool = False, timeout: int = 5) -> tuple:
    """
    Спрашивает у Ollama список СКАЧАННЫХ моделей (/api/tags) - это просто файлы
    на диске, ничего не загружается в память.
    Возвращает ({имя: размер_в_байтах}, текст ошибки). Результат ненадолго
    кэшируется, чтобы перезагрузка страницы не дёргала Ollama каждый раз.
    """
    global _OLLAMA_MODELS_CACHE
    
    if not force and time.monotonic() - _OLLAMA_MODELS_CACHE["at"] < MODELS_CACHE_TTL:
        return _OLLAMA_MODELS_CACHE["models"], _OLLAMA_MODELS_CACHE["error"]
    
    models, error = {}, None
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
        for m in data.get("models", []):
            name = m.get("name") or m.get("model") or ""
            if name:
                models[name] = m.get("size") or 0
    except Exception as e:
        error = f"Ollama недоступен по адресу {OLLAMA_BASE_URL} ({e})"
    
    _OLLAMA_MODELS_CACHE = {"at": time.monotonic(), "models": models, "error": error}
    return models, error

def as_int(value) -> int:
    """Размеры из API Ollama - числа, но иногда строки; чужой формат не должен ронять сайдбар."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

def fetch_loaded_models(force: bool = False, timeout: int = 5) -> tuple:
    """
    Спрашивает у Ollama, какие модели СЕЙЧАС в памяти (/api/ps) и сколько места
    занимают. Возвращает (список словарей, текст ошибки); у каждой модели:
    name, size (всего), size_vram (сколько лежит в видеопамяти),
    context_length, expires_at.
    Результат ненадолго кэшируется: страница опрашивает статус каждые 3 секунды,
    и без кэша каждый клиент дёргал бы Ollama на каждый запрос. force=True
    берёт свежие данные (для выгрузки моделей нужна точная картина).
    """
    global _LOADED_MODELS_CACHE
    
    if not force and time.monotonic() - _LOADED_MODELS_CACHE["at"] < LOADED_MODELS_CACHE_TTL:
        return _LOADED_MODELS_CACHE["models"], _LOADED_MODELS_CACHE["error"]
    
    models, error = [], None
    try:
        with urllib.request.urlopen(OLLAMA_PS_URL, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
        for m in data.get("models", []) or []:
            # Чужой формат одного элемента не должен обнулять весь список:
            # иначе сайдбар решил бы, что Ollama недоступна
            if not isinstance(m, dict):
                continue
            name = m.get("name") or m.get("model") or ""
            if not name:
                continue
            models.append({
                "name": name,
                "size": as_int(m.get("size")),
                "size_vram": as_int(m.get("size_vram")),
                "context_length": as_int(m.get("context_length")),
                "expires_at": str(m.get("expires_at") or ""),
            })
    except Exception as e:
        error = str(e)
    
    if not error:
        for loaded in models:
            # Запоминаем реальные размеры: следующая оценка «влезет/не влезет»
            # станет точной, а не по размеру файла
            record_vram_measurement(
                loaded["name"], loaded["context_length"], loaded["size"], loaded["size_vram"])
            warn_if_partially_in_ram(loaded)
    
    _LOADED_MODELS_CACHE = {"at": time.monotonic(), "models": models, "error": error}
    return models, error

def fetch_gpu_memory() -> dict:
    """
    Объём видеопамяти через nvidia-smi, если он есть (для процентов в сайдбаре).
    Возвращает {"total": байт, "used": байт} либо {} - тогда показываем только
    абсолютные гигабайты. Опрос внешнего процесса кэшируем на 10 секунд.
    """
    global _GPU_MEMORY_CACHE
    
    if time.monotonic() - _GPU_MEMORY_CACHE["at"] < GPU_MEMORY_CACHE_TTL:
        return _GPU_MEMORY_CACHE["info"]
    
    info = {}
    try:
        kwargs = {"capture_output": True, "text": True, "timeout": 5}
        if os.name == "nt":
            # Иначе на Windows у пользователя каждые 10 секунд мигает консольное окно
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
            **kwargs
        )
        if result.returncode == 0:
            total_mb = used_mb = 0
            for line in (result.stdout or "").strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    total_mb += int(float(parts[0]))
                    used_mb += int(float(parts[1]))
            if total_mb:
                info = {"total": total_mb * 1024 * 1024, "used": used_mb * 1024 * 1024}
    except Exception:
        # nvidia-smi нет, это не NVIDIA или он ругается - просто не показываем проценты
        info = {}
    
    _GPU_MEMORY_CACHE = {"at": time.monotonic(), "info": info}
    return info

# ============================================================
# ВЛЕЗЕТ ЛИ МОДЕЛЬ В ВИДЕОПАМЯТЬ (num_ctx и подсказка безопасного значения)
# ============================================================

def load_theatre_settings():
    """
    Возвращает правила судьи, сохранённые в прошлых запусках: имена участников
    каждый спектакль новые, а правила судьи - общая настройка роли.
    """
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            rules = data.get("judge_rules")
            if isinstance(rules, list) and rules:
                session.judge_rules = [str(r) for r in rules]
                print(f"⚖️  Загружены сохранённые правила судьи: {len(session.judge_rules)} пунктов")
    except Exception as e:
        print(f"  ⚠️  Не читается {SETTINGS_FILE.name}: {e}")

def save_theatre_settings():
    try:
        SETTINGS_FILE.write_text(
            json.dumps({"judge_rules": session.judge_rules}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Не сохраняется {SETTINGS_FILE.name}: {e}")

def resolve_model_name(model: str, available: dict) -> str:
    """«r1» -> «r1:latest»: имя, под которым модель реально лежит в Ollama."""
    if not model:
        return ""
    if model in available:
        return model
    if ":" in model:
        return model
    tagged = f"{model}:latest"
    return tagged if tagged in available else model

def fetch_model_info(model: str) -> dict:
    """
    Метаданные модели через /api/show (блоки, головы, длины ключей) - по ним
    считаем размер KV-кэша. Веса не грузятся, ответ кэшируется.
    """
    if not model:
        return {}
    if model in _MODEL_INFO_CACHE:
        return _MODEL_INFO_CACHE[model]
    
    info = {}
    try:
        req = urllib.request.Request(
            OLLAMA_SHOW_URL,
            data=json.dumps({"model": model}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            info = json.loads(response.read().decode('utf-8')).get("model_info", {}) or {}
    except Exception as e:
        print(f"  ⚠️  Не удалось прочитать метаданные модели {model}: {e}")
    
    _MODEL_INFO_CACHE[model] = info
    return info

def kv_bytes_per_token(info: dict) -> int:
    """
    Сколько KV-кэша съедает один токен контекста.
    У гибридных архитектур (Qwen3.5 и подобные) полноценное внимание только
    у каждого full_attention_interval-го блока, остальные - линейные/SSM слои
    с постоянным состоянием: без этой поправки оценка врёт в разы.
    """
    arch = info.get("general.architecture", "") or ""
    prefix = f"{arch}." if arch else ""
    
    blocks = as_int(info.get(prefix + "block_count"))
    if not blocks:
        return 0
    
    interval = as_int(info.get(prefix + "full_attention_interval"))
    attention_blocks = max(1, blocks // interval) if interval > 1 else blocks
    
    head_count = max(1, as_int(info.get(prefix + "attention.head_count")))
    kv_heads = as_int(info.get(prefix + "attention.head_count_kv")) or head_count
    head_dim = as_int(info.get(prefix + "attention.key_length"))
    if not head_dim:
        head_dim = max(1, as_int(info.get(prefix + "embedding_length")) // head_count)
    
    # ключи и значения одного размера
    return 2 * attention_blocks * kv_heads * head_dim * KV_CACHE_BYTES_PER_ELEM

def estimated_kv_bytes(info: dict, num_ctx: int) -> int:
    """
    KV-кэш под весь контекст. У моделей со скользящим окном кэш таких слоёв
    ограничен окном, а не всем контекстом.
    """
    per_token = kv_bytes_per_token(info)
    if not per_token:
        return 0
    
    arch = info.get("general.architecture", "") or ""
    window = as_int(info.get(f"{arch}.attention.sliding_window"))
    if window and window < num_ctx:
        return per_token * window
    return per_token * num_ctx

def load_vram_measurements():
    """Подхватывает размеры моделей, измеренные в прошлых запусках."""
    global _VRAM_MEASUREMENTS
    try:
        if VRAM_MEASUREMENTS_FILE.exists():
            data = json.loads(VRAM_MEASUREMENTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _VRAM_MEASUREMENTS = {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        print(f"  ⚠️  Не читается {VRAM_MEASUREMENTS_FILE.name}: {e}")

def save_vram_measurements():
    try:
        VRAM_MEASUREMENTS_FILE.write_text(
            json.dumps(_VRAM_MEASUREMENTS, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Не сохраняется {VRAM_MEASUREMENTS_FILE.name}: {e}")

def record_vram_measurement(name: str, ctx: int, size: int, size_vram: int):
    """
    Запоминает реальный размер модели в памяти при известном контексте.
    Больший контекст точнее для оценки, поэтому старую запись перезаписывает
    только такая же или более подробная.
    """
    if not name or ctx <= 0 or size <= 0:
        return
    
    old = _VRAM_MEASUREMENTS.get(name)
    if old and as_int(old.get("ctx")) >= ctx and as_int(old.get("size")) == size:
        return
    
    _VRAM_MEASUREMENTS[name] = {"ctx": ctx, "size": size, "size_vram": size_vram}
    save_vram_measurements()

def vram_base_bytes(resolved_name: str, info: dict, available: dict) -> tuple:
    """
    Сколько занимает модель БЕЗ KV-кэша под num_ctx, и точная ли это цифра.
    Точная, если модель уже измерена в /api/ps. Иначе берём размер файла: у него
    нет буферов вычислений, зато сверху есть запас VRAM_SAFETY_FACTOR.
    """
    per_token = kv_bytes_per_token(info)
    measured = _VRAM_MEASUREMENTS.get(resolved_name)
    if measured and per_token:
        base = as_int(measured.get("size")) - per_token * as_int(measured.get("ctx"))
        return max(0, base), True
    return as_int(available.get(resolved_name)), False

def estimate_vram_need(model: str, num_ctx: int, available: dict) -> dict:
    """Сколько VRAM нужно модели при заданном num_ctx (веса + KV-кэш)."""
    resolved = resolve_model_name(model, available)
    info = fetch_model_info(resolved)
    base, measured = vram_base_bytes(resolved, info, available)
    kv = estimated_kv_bytes(info, num_ctx)
    return {
        "model": model,
        "resolved": resolved,
        "num_ctx": num_ctx,
        "base": base,
        "kv": kv,
        "need": base + kv,
        "measured": measured,
    }

def suggest_safe_ctx(model: str, num_ctx: int, budget: int, available: dict) -> tuple:
    """
    Наибольший num_ctx, при котором модель ещё влезает в бюджет VRAM.
    Возвращает (num_ctx | None, сколько_байт_нужно | 0).
    """
    resolved = resolve_model_name(model, available)
    info = fetch_model_info(resolved)
    per_token = kv_bytes_per_token(info)
    if not per_token:
        return None, 0
    
    base, _ = vram_base_bytes(resolved, info, available)
    room = budget - base
    if room < per_token * VRAM_SAFE_MIN_CTX:
        return None, 0
    
    safe = min(num_ctx, room // per_token)
    safe = (safe // VRAM_SAFE_CTX_STEP) * VRAM_SAFE_CTX_STEP
    if safe < VRAM_SAFE_MIN_CTX:
        return None, 0
    
    return safe, base + per_token * safe

def check_vram_fit(models: list, num_ctx: int = None) -> dict:
    """
    Проверяет, влезут ли модели спектакля в видеопамять при текущем num_ctx.
    Ничего не грузит: только /api/show, /api/tags и уже измеренные размеры.
    """
    num_ctx = as_int(num_ctx) or as_int(OPTIONS.get("num_ctx")) or 4096
    gpu = fetch_gpu_memory()
    
    result = {
        "checked": False,
        "ok": True,
        "num_ctx": num_ctx,
        "gpu_total": as_int(gpu.get("total")),
        "budget": 0,
        "warnings": [],
        "error": "",
    }
    if not result["gpu_total"]:
        result["error"] = "объём видеопамяти неизвестен (nvidia-smi не отвечает)"
        return result
    
    available, models_error = fetch_ollama_models()
    if models_error:
        result["error"] = models_error
        return result
    
    result["checked"] = True
    result["budget"] = int(result["gpu_total"] * VRAM_SAFETY_FACTOR)
    
    for model in dict.fromkeys(models or []):
        if not model or model == "human":
            continue
        if not model_is_installed(model, set(available.keys())):
            continue  # про отсутствующие модели сообщает отдельная проверка
        
        est = estimate_vram_need(model, num_ctx, available)
        if est["need"] <= result["budget"]:
            continue
        
        safe_ctx, safe_need = suggest_safe_ctx(model, num_ctx, result["budget"], available)
        result["warnings"].append({
            "model": model,
            "resolved": est["resolved"],
            "need": est["need"],
            "safe_ctx": safe_ctx,
            "safe_need": safe_need,
            "measured": est["measured"],
        })
    
    result["ok"] = not result["warnings"]
    return result

def vram_warning_lines(status: dict) -> list:
    """Человекочитаемые строки про нехватку видеопамяти - для лога и интерфейса."""
    if not status.get("checked") or status.get("ok"):
        return []
    
    budget_gb = status["budget"] / 1e9
    lines = []
    for w in status["warnings"]:
        if w["safe_ctx"]:
            advice = f"безопасный num_ctx: {w['safe_ctx']} (тогда нужно ~{w['safe_need'] / 1e9:.1f} ГБ)"
        else:
            advice = "даже с минимальным контекстом не влезает целиком - часть считает процессор"
        tail = "" if w["measured"] else " [оценка по размеру файла]"
        lines.append(
            f"{w['model']}: при num_ctx {status['num_ctx']} нужно ~{w['need'] / 1e9:.1f} ГБ, "
            f"а доступно ~{budget_gb:.1f} ГБ из {status['gpu_total'] / 1e9:.1f} ГБ. {advice}{tail}"
        )
    return lines

def vram_spill_bytes(loaded: dict, gpu_total: int = 0) -> int:
    """
    Сколько памяти модели физически не поместилось в видеокарту.
    Два сигнала:
      • Ollama сама сказала, что положила в VRAM меньше, чем занимает модель;
      • Ollama заявила больше, чем на карте есть - так бывает на Windows, где
        часть памяти уходит в общую, и её считает процессор.
    """
    total, vram = as_int(loaded.get("size")), as_int(loaded.get("size_vram"))
    if not total or not vram:
        return 0
    if gpu_total and vram > gpu_total:
        return max(0, total - gpu_total)
    if vram < total:
        return total - vram
    return 0

def warn_if_partially_in_ram(loaded: dict):
    """
    Если модель не поместилась в видеопамять целиком, остальное считает
    процессор - это заметно медленнее. Сообщаем один раз на модель и контекст,
    иначе предупреждение повторялось бы на каждом опросе статуса.
    """
    spill = vram_spill_bytes(loaded, as_int(fetch_gpu_memory().get("total")))
    if not spill:
        return
    
    total = as_int(loaded.get("size"))
    ctx = as_int(loaded.get("context_length"))
    key = (loaded.get("name"), ctx)
    if key in _RAM_SPILL_WARNED:
        return
    _RAM_SPILL_WARNED.add(key)
    
    print(f"  ⚠️  {loaded['name']}: занимает {total / 1e9:.1f} ГБ, из них ~{spill / 1e9:.1f} ГБ "
          f"не поместились в видеопамять и считаются процессором (num_ctx={ctx}). "
          f"Уменьшите num_ctx.")

# Размеры моделей, измеренные в прошлых запусках: с ними подсказки num_ctx точные
load_vram_measurements()

def unload_other_show_models(current_model: str, show_models: set):
    """
    Перед ходом current_model выгружает из памяти остальные модели спектакля.
    Чужие модели (не из этого спектакля) не трогаем.
    """
    if not UNLOAD_OTHER_MODELS:
        return
    
    loaded_models, error = fetch_loaded_models(force=True)
    if error:
        return
    
    for loaded_name in [m["name"] for m in loaded_models]:
        if model_is_installed(current_model, {loaded_name}):
            continue
        if any(model_is_installed(m, {loaded_name}) for m in show_models):
            print(f"  🧹 Выгружаю из памяти: {loaded_name}")
            unload_model(loaded_name)

def check_models_available(models: list, force: bool = False) -> dict:
    """
    Проверяет, что все нужные модели есть в Ollama.
    Возвращает {"ok": bool, "missing": [...], "error": str | None}.
    """
    required = [m for m in models if m and m != "human"]
    if not required:
        return {"ok": True, "missing": [], "error": None}
    
    available, error = fetch_ollama_models(force=force)
    if error:
        return {"ok": False, "missing": required, "error": error}
    
    missing = [m for m in required if not model_is_installed(m, available)]
    return {"ok": not missing, "missing": missing, "error": None}

def report_models_status(models: list):
    """Печатает при старте, все ли нужные модели есть в Ollama."""
    status = check_models_available(models, force=True)
    if status["error"]:
        print(f"  ⚠️  {models_problem_message(status)}")
        print("     Приложение запустится, но начать спектакль не получится.")
    elif status["missing"]:
        print(f"  ❌ Нет моделей: {', '.join(status['missing'])}")
        print(f"     {models_problem_message(status)}")
    else:
        print("  ✅ Все модели на месте")
    
    # Сколько это заняло бы памяти, если загрузить всё сразу
    available, _ = fetch_ollama_models()
    sizes = {}
    for required in models:
        for name, size in available.items():
            if required != "human" and model_is_installed(required, {name}):
                sizes[required] = size
                break
    if sizes:
        parts = " + ".join(f"{m} {s / 1e9:.1f}" for m, s in sizes.items())
        total = sum(sizes.values()) / 1e9
        print(f"  📦 На диске: {parts} = {total:.1f} ГБ (в памяти держится только текущий говорящий)")
    
    # Влезут ли модели в видеопамять при текущем num_ctx
    vram_status = check_vram_fit(models)
    vram_lines = vram_warning_lines(vram_status)
    if vram_lines:
        print(f"  ⚠️  Не хватает видеопамяти (num_ctx {vram_status['num_ctx']}):")
        for line in vram_lines:
            print(f"     • {line}")
        print("     Спектакль пойдёт, но такие модели будут считать заметно медленнее")
    elif vram_status.get("checked"):
        print(f"  ✅ Видеопамяти хватает на num_ctx {vram_status['num_ctx']}")
    elif vram_status.get("error"):
        print(f"  ℹ️  Проверка видеопамяти пропущена: {vram_status['error']}")
    
    loaded, _ = fetch_loaded_models(force=True)
    if loaded:
        parts = ", ".join(
            f"{m['name']} {m['size_vram'] / 1e9:.1f} ГБ VRAM" if m["size_vram"]
            else f"{m['name']} {m['size'] / 1e9:.1f} ГБ RAM"
            for m in loaded
        )
        print(f"  📦 Сейчас в памяти: {parts}")

def models_problem_message(status: dict) -> str:
    """Человекочитаемое объяснение, почему спектакль нельзя начать."""
    if status.get("error"):
        return f"{status['error']}. Проверьте, что Ollama запущена."
    
    missing = status.get("missing") or []
    if missing:
        pulls = " ; ".join(f"ollama pull {m}" for m in missing)
        return f"В Ollama нет моделей: {', '.join(missing)}. Скачайте их: {pulls}"
    
    return ""


def ask_model_with_tools(model: str, messages: list, supports_tools: bool = True, tool_choice: str = None) -> tuple:
    """
    Отправляет запрос к модели. Автоматически определяет поддержку tools.
    
    Parameters:
        tool_choice: "auto" (по умолчанию), "any" (обязан вызвать инструмент), 
                     или None (не использовать)
    """
    # Проверяем кэш поддержки tools
    if model not in MODELS_TOOLS_SUPPORT:
        # Первый запрос - проверяем через /api/show
        if ENABLE_SEARCH and supports_tools:
            print(f"  🔍 Проверяю поддержку инструментов для модели {model}...")
            supports = check_model_tools_support(model)
            MODELS_TOOLS_SUPPORT[model] = supports
            if supports:
                print(f"  ✅ Модель {model} поддерживает инструменты")
            else:
                print(f"  ⚠️  Модель {model} НЕ поддерживает инструменты")
    
    # Теперь делаем реальный запрос с учётом поддержки tools
    try:
        data = {
            "model": model,
            "messages": messages,
            "options": OPTIONS,
            "stream": False
        }
        if not ENABLE_THINKING:
            data["think"] = False
        
        # Добавляем tools только если модель их поддерживает
        if ENABLE_SEARCH and supports_tools and MODELS_TOOLS_SUPPORT.get(model, False):
            data["tools"] = [{
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Ищет информацию в интернете",
                    "parameters": {
                        "type": "object",
                        "required": ["query"],
                        "properties": {
                            "query": {"type": "string", "description": "Поисковый запрос"},
                            "max_results": {"type": "integer", "default": 5}
                        }
                    }
                }
            }]
            
            # Добавляем tool_choice если указан
            if tool_choice:
                data["tool_choice"] = tool_choice
        
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
            if "message" in result:
                msg = result["message"]
                return msg.get("content", "") or "", msg.get("tool_calls") or []
            return "", []
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else "Нет деталей ошибки"
        print(f"  ⚠️  HTTP ошибка {e.code}: {e.reason}")
        print(f"  📋 Детали: {error_body}")
        return f"[ОШИБКА: HTTP {e.code}]", []
    except Exception as e:
        print(f"  ⚠️  Ошибка запроса к модели: {e}")
        return f"[ОШИБКА: {e}]", []

def search_web(query: str, max_results: int = 5) -> str:
    if not SEARCH_AVAILABLE:
        return "Поиск недоступен"
    
    print(f"  🔍 Поиск: '{query}'")
    
    results, last_error = ddgs_search("Поиск", "text", query, max_results)
    
    if not results:
        if last_error:
            print(f"  ⚠️  Ошибка поиска: {last_error}")
            return f"Ошибка поиска: {last_error}"
        return f"По запросу '{query}' ничего не найдено."
    
    output = "Результаты поиска:\n\n"
    for i, result in enumerate(results, 1):
        title = result.get('title', 'Без названия')
        body = result.get('body', 'Без описания')
        href = result.get('href', '')
        output += f"{i}. {title}\n   {body}\n   {href}\n\n"
    
    return output.strip()

def ask_model(model: str, messages: list, participant_name: str) -> tuple:
    search_queries = []
    search_count = 0
    max_searches = 3
    max_iterations = 8
    force_tool_use = False  # Флаг для принудительного использования инструмента через tool_choice
    forced_attempts = 0     # Счётчик попыток принудительного поиска
    max_forced_attempts = 2 # Максимум попыток принудительного поиска
    content = ""
    
    # Вычисляем нормализованное имя один раз
    participant_name_normalized = participant_name.lower().replace(" ", "_")
    
    for iteration in range(max_iterations):
        # Режиссёр завершил спектакль - не тратим время на новые поиски
        if session.moderator_finished and content and content.strip():
            return content, search_count, search_queries
        
        # Если нужен принудительный поиск - передаём tool_choice="any"
        current_tool_choice = "any" if force_tool_use else None
        
        content, tool_calls = ask_model_with_tools(model, messages, tool_choice=current_tool_choice)
        tool_calls = tool_calls or []
        
        # Сбрасываем флаг после использования
        if force_tool_use:
            force_tool_use = False
            forced_attempts += 1
        
        if not tool_calls and content and content.strip():
            # Принудительный поиск только если модель поддерживает tools
            if search_count < MIN_SEARCHES and forced_attempts < max_forced_attempts and MODELS_TOOLS_SUPPORT.get(model, False):
                print(f"  🔍 Принудительный поиск (попытка {forced_attempts + 1}/{max_forced_attempts})...")
                messages.append({"role": "assistant", "content": content, "name": participant_name_normalized})
                messages.append({
                    "role": "user",
                    "content": "Используй инструмент поиска для получения актуальной информации.",
                    "name": "system"
                })
                force_tool_use = True  # В следующий раз требуем инструмент через API
                continue
            else:
                return content, search_count, search_queries
        
        if not tool_calls:
            break
        
        messages.append({
            "role": "assistant",
            "content": content or "",
            "tool_calls": tool_calls,
            "name": participant_name_normalized
        })
        
        has_search = False
        for tc in tool_calls:
            func = tc.get("function", {}) or {}
            func_name = func.get("name", "")
            
            # В большинстве случаев arguments - словарь, но некоторые сборки
            # Ollama отдают его JSON-строкой, а иногда и не тем типом вообще.
            func_args = func.get("arguments", {})
            if isinstance(func_args, str):
                try:
                    func_args = json.loads(func_args)
                except json.JSONDecodeError:
                    func_args = {}
            if not isinstance(func_args, dict):
                func_args = {}
            
            if func_name == "search_web" and search_count < max_searches:
                query = str(func_args.get("query", "") or "")
                try:
                    max_results = int(func_args.get("max_results", 5))
                except (TypeError, ValueError):
                    max_results = 5
                
                session.current_action = "searching"
                session.search_query = query
                search_queries.append(query)
                search_count += 1
                
                result = search_web(query, max_results)
                
                messages.append({
                    "role": "tool",
                    "tool_name": "search_web",
                    "content": result,
                    "name": "search_web"
                })
                
                session.current_action = "thinking"
                session.search_query = None
                has_search = True
            else:
                messages.append({
                    "role": "tool",
                    "tool_name": func_name,
                    "content": "[лимит поисков исчерпан]",
                    "name": func_name
                })
        
        if not has_search:
            break
    
    if (not content or not content.strip()) and not session.moderator_finished:
        messages.append({
            "role": "user",
            "content": "Дай свой финальный ответ на русском языке.",
            "name": "system"
        })
        
        try:
            data = {
                "model": model,
                "messages": messages,
                "options": OPTIONS,
                "stream": False
            }
            if not ENABLE_THINKING:
                data["think"] = False
            
            req = urllib.request.Request(
                OLLAMA_URL,
                data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            
            with urllib.request.urlopen(req, timeout=120) as response:
                result = json.loads(response.read().decode('utf-8'))
                if "message" in result:
                    content = result["message"].get("content", "")
                    if content and content.strip():
                        return content, search_count, search_queries
        except Exception as e:
            print(f"  ⚠️  Ошибка финального запроса: {e}")
    
    return content or "[Модель не дала ответ]", search_count, search_queries

def unload_model(model: str):
    try:
        data = {"model": model, "keep_alive": 0}
        req = urllib.request.Request(
            OLLAMA_GENERATE_URL,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=30)
    except:
        pass

def create_post(display_name: str, model_used: str, content: str, round_num: int, 
                avatar_url: str = None, avatar_emoji: str = None,
                search_count: int = 0, search_queries: list = None,
                role: str = "participant", gender: str = "male") -> dict:
    """Единая функция создания поста для любого участника (human или AI)"""
    if search_queries is None:
        search_queries = []
    
    # Определяем роль и соответствующую иконку
    role_icons = {
        "participant": "🎭",
        "moderator": "🎬",
        "judge": "⚖️"
    }
    
    role_names = {
        "participant": "Участник",
        "moderator": "Модератор",
        "judge": "Судья"
    }
    
    return {
        "id": len(session.posts) + 1,
        "display_name": display_name,
        "model_used": model_used,
        "avatar_url": avatar_url,
        "avatar_emoji": avatar_emoji or role_icons.get(role, "📣"),
        "content": content,
        "content_html": markdown_to_html(content),
        "round": round_num,
        "timestamp": time.strftime("%H:%M"),
        "search_count": search_count,
        "search_queries": search_queries,
        "role": role,
        "role_icon": role_icons.get(role, "🎭"),
        "role_name": role_names.get(role, "Участник"),
        "gender": gender,
        "gender_symbol": "♂" if gender == "male" else "♀"
    }

class DebateSession:
    """Инкапсулирует состояние и логику дебатов"""
    
    def __init__(self):
        self.running = False
        self.topic = ""
        self.posts = []
        self.current_round = 0
        self.current_participant = None
        self.current_action = None
        self.search_query = None
        self.finished = False
        self.avatars = {}
        self.avatar_emojis = {}  # {"display_name": "🎭"}
        self.instructions = {}
        self.static_instructions = []  # Настраиваемые статичные инструкции
        self.judge_rules = list(DEFAULT_JUDGE_RULES)  # настраиваемые правила судьи
        self.waiting_for_human = False
        self.moderator_message = None
        self.moderator_finished = False
        self.runtime_participants = []
        self.conversation_history = []
    
    def reset(self, topic: str, runtime_participants: list, avatars: dict, 
              instructions: dict, avatar_emojis: dict = None, static_instructions: list = None,
              judge_rules: list = None):
        """Сброс состояния для новых дебатов"""
        self.running = True
        self.topic = topic
        self.posts = []
        self.current_round = 0
        self.current_participant = None
        self.current_action = None
        self.search_query = None
        self.finished = False
        self.avatars = avatars
        self.avatar_emojis = avatar_emojis or {}
        self.instructions = instructions
        self.static_instructions = static_instructions or []
        # Правила судьи - глобальная настройка, а не свойство спектакля: при новом
        # спектакле они сохраняются, иначе правки модератора терялись бы
        self.judge_rules = list(judge_rules) if judge_rules else (self.judge_rules or list(DEFAULT_JUDGE_RULES))
        self.waiting_for_human = False
        self.moderator_message = None
        self.moderator_finished = False
        self.runtime_participants = runtime_participants
        self.conversation_history = []
    
    def clear(self):
        """Полный сброс состояния (кнопка «Новый спектакль»)"""
        self.__init__()
    
    def add_post(self, display_name: str, model_used: str, content: str, round_num: int,
                 search_count: int = 0, search_queries: list = None, is_moderator: bool = False, is_judge: bool = False, gender: str = "male"):
        """Добавляет пост в историю и список постов"""
        avatar_url = self.avatars.get(display_name)
        avatar_emoji = self.avatar_emojis.get(display_name, "📣")
        
        # Определяем роль
        role = "participant"
        if is_moderator:
            role = "moderator"
        elif is_judge:
            role = "judge"
        
        post = create_post(display_name, model_used, content, round_num, 
                          avatar_url, avatar_emoji, search_count, search_queries, role, gender)
        self.posts.append(post)
        
        if content.strip():
            self.conversation_history.append({
                "display_name": display_name,
                "content": content,
                "is_moderator": is_moderator,
                "is_judge": is_judge,
                "round": round_num
            })
        
        return post
    
    def current_participant_is_moderator(self) -> bool:
        """Ждёт ли сейчас хода режиссёр (участник с is_moderator)."""
        if not (self.waiting_for_human and self.current_participant):
            return False
        
        return any(
            p["display_name"] == self.current_participant and p.get("is_moderator", False)
            for p in self.runtime_participants
        )
    
    def moderator_messages(self) -> list:
        """Указания режиссёра из истории (пустые отбрасываются)."""
        return [
            post["content"] for post in self.conversation_history
            if post.get("is_moderator", False) and post["content"].strip()
        ]
    
    def get_system_prompt(self, participant: dict, all_names: list) -> str:
        """Генерирует системный промпт для участника с заменой плейсхолдеров"""
        # Исключаем модератора и судью из списка собеседников
        other_names = [
            name for name in all_names 
            if name != participant["display_name"]
            and not any(p["display_name"] == name and (p.get("is_moderator") or p.get("is_judge")) 
                       for p in self.runtime_participants)
        ]
        
        # Проверяем, является ли этот участник судьёй
        is_judge = participant.get("is_judge", False)
        
        # Определяем правила общения (статичные инструкции)
        if is_judge:
            # Правила судьи настраиваются в интерфейсе, по умолчанию - DEFAULT_JUDGE_RULES
            rules = [instr for instr in (self.judge_rules or DEFAULT_JUDGE_RULES) if instr.strip()]
            print(f"  ⚖️  Используем правила судьи для {participant['display_name']} ({len(rules)} пунктов)")
        elif self.static_instructions and len(self.static_instructions) > 0:
            rules = [instr for instr in self.static_instructions if instr.strip()]
            print(f"  📋 Используем пользовательские правила для {participant['display_name']}: {len(rules)} пунктов")
        else:
            rules = DEFAULT_STATIC_INSTRUCTIONS
            print(f"  📋 Используем дефолтные правила для {participant['display_name']}")
        
        # Заменяем плейсхолдеры в каждом правиле
        processed_rules = []
        for rule in rules:
            # {ИМЯ} → имя текущего участника
            rule = rule.replace("{ИМЯ}", participant["display_name"])
            # {СОБЕСЕДНИКИ} → список других участников через запятую
            rule = rule.replace("{СОБЕСЕДНИКИ}", ", ".join(other_names))
            # {ТЕМА} → тема обсуждения
            rule = rule.replace("{ТЕМА}", self.topic)
            processed_rules.append(rule)
        
        # Объединяем все правила
        system_prompt = " ".join(processed_rules)
        
        # Добавляем индивидуальную инструкцию участника с чётким заголовком
        custom_instruction = self.instructions.get(participant["display_name"], "")
        
        # Для судьи используем дефолтную инструкцию если нет своей
        if is_judge and not (custom_instruction and custom_instruction.strip()):
            custom_instruction = DEFAULT_JUDGE_INSTRUCTION
            print(f"  ⚖️  Применяю дефолтную инструкцию судьи для {participant['display_name']}")
        
        if custom_instruction and custom_instruction.strip():
            # Заменяем плейсхолдеры в личной инструкции тоже
            custom_instruction = custom_instruction.replace("{ИМЯ}", participant["display_name"])
            custom_instruction = custom_instruction.replace("{СОБЕСЕДНИКИ}", ", ".join(other_names))
            custom_instruction = custom_instruction.replace("{ТЕМА}", self.topic)
            
            system_prompt += f"\n\nТВОИ ЛИЧНЫЕ ИНСТРУКЦИИ (обязательны к исполнению):\n{custom_instruction.strip()}"
            print(f"  📝 Применяю индивидуальную инструкцию для {participant['display_name']}: {custom_instruction[:50]}...")
        
        # Добавляем инструкции по поиску
        if ENABLE_SEARCH:
            min_search_text = ""
            if MIN_SEARCHES > 0:
                min_search_text = f" Сделай минимум {MIN_SEARCHES} поиск(ов) перед ответом."
            
            system_prompt += (
                " Если есть сомнения в фактах или мнениях - используй поиск для уточнения. "
                "При поиске НЕ указывай год."
                + min_search_text
            )
        
        # Добавляем указания режиссёра из истории (фильтруем пустые)
        moderator_messages = self.moderator_messages()
        if moderator_messages:
            system_prompt += "\n\nУКАЗАНИЯ ОТ РУКОВОДСТВА (обязательны к исполнению):\n"
            for msg in moderator_messages:
                system_prompt += f"• {msg}\n"
        
        return system_prompt
    
    def build_messages_for_ai(self, participant: dict, round_num: int) -> list:
        """Строит список сообщений для AI модели"""
        # Исключаем модераторов и судей из списка участников для промпта
        non_moderator_names = [
            p["display_name"] for p in self.runtime_participants 
            if not p.get("is_moderator", False) and not p.get("is_judge", False)
        ]
        
        # Вычисляем имена один раз
        participant_name = participant["display_name"]
        participant_name_normalized = participant_name.lower().replace(" ", "_")
        
        # Проверяем, является ли этот участник судьёй
        is_judge = participant.get("is_judge", False)
        
        # Получаем полный системный промпт (включая все инструкции)
        system_prompt = self.get_system_prompt(participant, non_moderator_names)
        
        messages = [
            {"role": "system", "content": system_prompt, "name": "system"},
        ]
        
        # Фильтруем реплики модератора и судьи (они уже в системном промпте)
        non_moderator_history = [
            post for post in self.conversation_history 
            if not post.get("is_moderator", False) and not post.get("is_judge", False)
        ]
        
        # Для судьи фильтруем только сообщения текущего раунда до него
        if is_judge:
            # Находим индекс ТЕКУЩЕГО судьи в списке участников
            judge_idx = next(
                (i for i, p in enumerate(self.runtime_participants) 
                 if p["display_name"] == participant_name and p.get("is_judge", False)),
                -1
            )
            
            # Получаем только посты текущего раунда (исключаем посты других судей)
            current_round_posts = [
                post for post in non_moderator_history 
                if post.get("round", 0) == round_num
            ]
            
            # Если судья первый в раунде - у него нет постов для оценки
            if judge_idx <= 0:
                # Судья первый - оценивает предыдущий раунд или говорит что нет данных
                if not current_round_posts:
                    messages.append({
                        "role": "user",
                        "content": f'Как {participant_name}, оцени предыдущий раунд. Если это первый раунд и никто ещё не говорил, скажи что оценивать нечего.',
                        "name": participant_name_normalized
                    })
                    return messages
            else:
                # Судья не первый - берём посты до него в текущем раунде
                # Находим посты участников которые идут до этого судьи
                participants_before_judge = [
                    p["display_name"] for i, p in enumerate(self.runtime_participants) 
                    if i < judge_idx and not p.get("is_moderator", False) and not p.get("is_judge", False)
                ]
                
                # Фильтруем посты только от тех кто до судьи в этом раунде
                current_round_posts = [
                    post for post in current_round_posts
                    if post["display_name"] in participants_before_judge
                ]
            
            # Преобразуем в формат сообщений
            history_messages = []
            for post in current_round_posts:
                speaker_name = post["display_name"]
                content = post["content"]
                speaker_name_normalized = speaker_name.lower().replace(" ", "_")
                
                history_messages.append({
                    "role": "user",
                    "content": f"{speaker_name} говорит: {content}",
                    "name": speaker_name_normalized
                })
            
            # Добавляем историю в messages
            messages.extend(history_messages)
            
            # Добавляем финальный запрос для судьи
            if current_round_posts:
                messages.append({
                    "role": "user",
                    "content": f'Как {participant_name}, оцени выступления участников в этом раунде. Для каждого участника укажи оценку от 1 до 5 баллов и краткое содержание его речи.',
                    "name": participant_name_normalized
                })
            else:
                messages.append({
                    "role": "user",
                    "content": f'Как {participant_name}, в этом раунде до тебя никто не говорил. Скажи что оценивать нечего.',
                    "name": participant_name_normalized
                })
            
            return messages
        
        # Преобразуем в формат сообщений
        history_messages = []
        for post in non_moderator_history:
            speaker_name = post["display_name"]
            content = post["content"]
            speaker_name_normalized = speaker_name.lower().replace(" ", "_")
            
            if speaker_name == participant_name:
                history_messages.append({
                    "role": "assistant",
                    "content": content,
                    "name": speaker_name_normalized
                })
            else:
                history_messages.append({
                    "role": "user",
                    "content": f"{speaker_name} говорит: {content}",
                    "name": speaker_name_normalized
                })
        
        # Умная обрезка истории на основе подсчёта токенов
        system_prompt_tokens = estimate_tokens(system_prompt)
        trimmed_history = trim_history_by_tokens(history_messages, system_prompt_tokens)
        
        # Добавляем обрезанную историю в messages
        messages.extend(trimmed_history)
        
        if round_num == 1 and len([p for p in self.conversation_history if not p.get("is_moderator", False)]) == 0:
            # Первый участник начинает обсуждение - не нужно обращаться к другим, они ещё не говорили
            messages.append({
                "role": "user", 
                "content": f'Как {participant_name}, ты начинаешь обсуждение на тему "{self.topic}". Представься, обозначь свою позицию по теме и предложи другим участникам высказаться.',
                "name": participant_name_normalized
            })
        else:
            # Находим последний пост НЕ от модератора (модератор уже в системном промпте)
            non_moderator_posts = [p for p in self.conversation_history if not p.get("is_moderator", False)]
            last_post = non_moderator_posts[-1] if non_moderator_posts else None
            
            if last_post:
                last_speaker = last_post["display_name"]
                
                # Проверяем количество постов в истории
                if len(non_moderator_posts) == 1:
                    # Только один участник говорил - отвечаем только ему
                    messages.append({
                        "role": "user",
                        "content": f'{last_speaker} только что сказал: "{last_post["content"]}". Как {participant_name}, ты тоже начинаешь обсуждение. Ответь {last_speaker} и вырази свою позицию по теме.',
                        "name": participant_name_normalized
                    })
                else:
                    # Несколько участников уже говорили - отвечаем последнему и другим
                    messages.append({
                        "role": "user",
                        "content": f'{last_speaker} только что сказал: "{last_post["content"]}". Как {participant_name}, ответь ему и другим участникам, обращаясь по именам.',
                        "name": participant_name_normalized
                    })
            else:
                messages.append({
                    "role": "user",
                    "content": f'Как {participant_name}, продолжай диалог, обращаясь к другим участникам по именам.',
                    "name": participant_name_normalized
                })
        
        return messages
    
    def handle_ai_turn(self, participant: dict, round_num: int) -> tuple:
        """Обрабатывает ход AI участника. Возвращает (response, search_count, search_queries)"""
        self.current_action = "thinking"
        
        messages = self.build_messages_for_ai(participant, round_num)
        
        response, search_count, search_queries = ask_model(
            model=participant["model"],
            messages=messages,
            participant_name=participant["display_name"]
        )
        
        self.add_post(
            display_name=participant["display_name"],
            model_used=participant["model"],
            content=response,
            round_num=round_num,
            search_count=search_count,
            search_queries=search_queries,
            is_judge=participant.get("is_judge", False),
            gender=participant.get("gender", "male")
        )
        
        self.current_action = None
        time.sleep(0.5)
        
        return response, search_count, search_queries

# Глобальный экземпляр сессии
session = DebateSession()

# Правила судьи из прошлых запусков (можно менять в интерфейсе на ходу режиссёра)
load_theatre_settings()

def run_debate_thread(topic: str):
    print(f"🎬 Поток дебатов запущен для темы: {topic}")
    runtime_participants = session.runtime_participants
    print(f"👥 Участников в сессии: {len(runtime_participants)}")
    
    if not runtime_participants:
        # Без участников цикл ниже крутился бы вечно и съедал ядро процессора
        print("❌ Нет участников - спектакль невозможен")
        session.finished = True
        session.running = False
        return
    
    # Модели этого спектакля: их выгружаем по окончании и держим по одной в памяти
    show_models = {p.get("model", "") for p in runtime_participants
                   if p.get("model") and p.get("model") != "human"}
    
    print("\n🎭 Используем подготовленный грим и костюмы...")
    for participant in runtime_participants:
        display_name = participant.get("display_name", "")
        if display_name and display_name in session.avatars:
            print(f"  ✅ {display_name}: готов к выходу на подмостки")
        else:
            print(f"  ⚠️  {display_name}: грим не подготовлен (будет эмодзи)")
    
    round_num = 0
    
    try:
        while True:
            round_num += 1
            session.current_round = round_num
            print(f"\n🎭 Акт {round_num}")
            
            print(f"  🎭 Персонажи: {[p.get('display_name', '') + ' (' + p.get('model', '') + ')' for p in runtime_participants]}")
            
            for participant in runtime_participants:
                # Режиссёр мог завершить спектакль прямо посреди акта: тогда не ждём
                # конца круга, а останавливаемся на ближайшем участнике
                if session.moderator_finished:
                    print("  ⏹  Режиссёр завершил спектакль - прерываю акт")
                    break
                
                print(f"  🎭 На сцене: {participant.get('display_name', '')} (модель: {participant.get('model', '')})")
                session.current_participant = participant.get("display_name", "")
                
                if participant.get("model") == "human":
                    session.moderator_message = None
                    session.current_action = None
                    time.sleep(0.2)
                    
                    session.current_action = "waiting"
                    session.waiting_for_human = True
                    print(f"\n⏳ Ожидание реплики от {participant['display_name']}...")
                    
                    while True:
                        time.sleep(0.5)
                        
                        if session.moderator_finished:
                            print(f"\n✅ Спектакль завершён режиссёром")
                            session.finished = True
                            session.waiting_for_human = False
                            break
                        
                        current_message = session.moderator_message
                        if current_message is not None:
                            session.waiting_for_human = False
                            
                            if current_message.strip():
                                post = session.add_post(
                                    display_name=participant.get("display_name", ""),
                                    model_used="human",
                                    content=current_message,
                                    round_num=round_num,
                                    search_count=0,
                                    search_queries=[],
                                    is_moderator=participant.get("is_moderator", False),
                                    gender=participant.get("gender", "male")
                                )
                                print(f"🎬 {participant.get('display_name', '')}: {current_message[:50]}")
                                socketio.emit('new_post', post)
                            else:
                                print(f"🎬 {participant.get('display_name', '')} пропустил действие")
                            
                            session.moderator_message = None
                            session.current_action = None
                            break
                    
                    if session.moderator_finished:
                        break
                else:
                    # В памяти оставляем только того, кто сейчас говорит
                    unload_other_show_models(participant.get("model", ""), show_models)
                    
                    session.current_action = "thinking"
                    
                    session.handle_ai_turn(participant, round_num)
                    
                    post = session.posts[-1] if session.posts else None
                    if post:
                        socketio.emit('new_post', post)
                    
                    session.current_action = None
                    time.sleep(0.5)
            
            if session.moderator_finished:
                break
    except Exception as e:
        # Не даём потоку умереть тихо: иначе интерфейс навсегда остаётся
        # в состоянии "идёт спектакль", а кнопки перестают реагировать.
        print(f"❌ Спектакль прерван из-за ошибки: {e}")
        traceback.print_exc()
    finally:
        # Всегда завершаем спектакль явно, при любом выходе из цикла - в том
        # числе при ошибке, чтобы клиент разблокировал кнопки.
        session.finished = True
        session.running = False
        session.current_participant = None
        session.current_action = None
        
        if UNLOAD_AFTER_DEBATE:
            for model in show_models:
                unload_model(model)

# ============================================================
# СОВРЕМЕННЫЙ ДИЗАЙН
# ============================================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AI Театр</title>
    <link rel="icon" href="/favicon.ico" type="image/x-icon">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Oswald:wght@400;700&family=Raleway:wght@400;600&display=swap');
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Georgia, 'Times New Roman', Times, serif; background: #ffffff; min-height: 100vh; color: #000000; line-height: 1.6; font-size: 16px; }
        .layout { display: flex; min-height: 100vh; }
        .main-content { flex: 1; padding: 40px 60px; }
        .sidebar { width: 320px; background: #ffffff; border-left: 1px solid #000000; padding: 40px 30px; position: sticky; top: 0; height: 100vh; overflow-y: auto; }
        .container { width: 55%; margin: 0 auto; }
        .header { background: #ffffff; border-bottom: 1px solid #000000; padding: 40px 0 30px 0; margin-bottom: 40px; text-align: center; }
        .header-date { font-size: 14px; letter-spacing: 1px; margin-bottom: 20px; text-transform: uppercase; }
        .header h1 { font-family: Georgia, serif; font-size: 80px; color: #000000; margin-bottom: 15px; font-weight: normal; letter-spacing: 2px; font-variant: small-caps; }
        .header-subtitle { font-size: 16px; font-style: italic; font-weight: normal; border-top: 1px solid #000000; padding-top: 15px; margin-top: 15px; }
        .header-topic { font-size: 24px; font-weight: bold; color: #000000; margin-top: 20px; padding: 20px; border: 2px solid #000000; text-align: center; line-height: 1.4; min-height: 60px; white-space: pre-wrap; word-wrap: break-word; }
        .card { background: #ffffff; border: none; border-top: 1px solid #000000; border-bottom: 1px solid #000000; padding: 30px 0; margin-bottom: 40px; }
        .card h2 { font-family: Georgia, serif; font-size: 40px; margin-bottom: 30px; color: #000000; font-weight: normal; text-align: center; letter-spacing: 1px; }
        .participants-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 40px; margin-bottom: 30px; }
        .participant-card { border: none; border-top: 1px solid #000000; padding: 25px 0; background: #ffffff; }
        .avatar-container { display: flex; justify-content: center; margin-bottom: 20px; }
        .avatar-preview { width: 140px; height: 140px; border: 1px solid #000000; overflow: hidden; display: flex; align-items: center; justify-content: center; font-size: 70px; cursor: pointer; background: #ffffff; }
        .avatar-preview:hover { border: 2px solid #000000; }
        .avatar-preview img { width: 100%; height: 100%; object-fit: cover; filter: grayscale(100%); }
        .input-group { margin-bottom: 20px; }
        .input-group label { display: block; font-family: Georgia, serif; font-weight: normal; margin-bottom: 8px; color: #000000; font-size: 16px; text-transform: uppercase; letter-spacing: 1px; }
        .input-group input, .input-group textarea { width: 100%; padding: 10px; border: 1px solid #000000; font-size: 18px; background: #ffffff; color: #000000; font-family: Georgia, serif; }
        .input-group input:focus, .input-group textarea:focus { outline: none; border: 2px solid #000000; }
        .input-group textarea { resize: vertical; min-height: 80px; font-family: Georgia, serif; line-height: 1.6; }
        .btn { padding: 12px 30px; border: 1px solid #000000; font-family: Georgia, serif; font-size: 16px; font-weight: normal; cursor: pointer; margin-right: 15px; letter-spacing: 1px; text-transform: uppercase; }
        .btn-primary { background: #000000; color: #ffffff; }
        .btn-primary:hover { background: #333333; }
        .btn-secondary { background: #ffffff; color: #000000; }
        .btn-secondary:hover { background: #f5f5f5; }
        .btn:disabled { opacity: 0.3; cursor: not-allowed; }
        .sidebar-section { margin-bottom: 40px; padding-bottom: 30px; border-bottom: 1px solid #000000; }
        .sidebar-title { font-family: Georgia, serif; font-size: 14px; font-weight: normal; color: #000000; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 15px; }
        .status-bar { background: #ffffff; border: none; border-left: 3px solid #000000; color: #000000; padding: 15px 20px; font-size: 16px; margin-bottom: 20px; font-style: italic; line-height: 1.6; }
        .status-bar.active { border-left: 4px solid #000000; }
        .post { background: #ffffff; border: none; border-top: 1px solid #000000; padding: 40px 0; margin-bottom: 0; display: flex; gap: 30px; }
        .post-avatar { flex-shrink: 0; }
        .post-avatar img { width: 150px; height: 150px; object-fit: cover; border: 1px solid #000000; filter: grayscale(100%); }
        .post-avatar .emoji { width: 150px; height: 150px; background: #ffffff; border: 1px solid #000000; display: flex; align-items: center; justify-content: center; font-size: 75px; }
        .post-content { flex: 1; min-width: 0; }
        .post-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #000000; }
        .post-author { font-family: Georgia, serif; font-size: 36px; font-weight: normal; color: #000000; letter-spacing: 1px; }
        .post-model { font-size: 14px; color: #000000; font-family: Georgia, serif; margin-top: 5px; font-style: italic; }
        .post-time { font-size: 14px; color: #000000; font-style: italic; }
        .post-text { font-size: 18px; line-height: 1.8; color: #000000; word-wrap: break-word; overflow-wrap: break-word; text-align: justify; }
        .post-text p { margin-bottom: 15px; }
        .post-text p:last-child { margin-bottom: 0; }
        .post-text strong { font-weight: bold; }
        .post-text em { font-style: italic; }
        .post-text code { background: #ffffff; padding: 2px 6px; font-family: 'Courier New', monospace; border: 1px solid #000000; }
        .post-text ul, .post-text ol { margin: 15px 0; padding-left: 30px; }
        .post-text li { margin-bottom: 10px; line-height: 1.7; }
        .post-text li::marker { font-weight: bold; }
        
        /* Стили для ролей */
        .role-badge { 
            display: inline-block; 
            padding: 2px 8px; 
            border-radius: 3px; 
            font-size: 11px; 
            font-weight: bold; 
            margin-right: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .role-participant { 
            background: #e3f2fd; 
            color: #1976d2; 
            border: 1px solid #1976d2;
        }
        .role-moderator { 
            background: #fff3e0; 
            color: #f57c00; 
            border: 1px solid #f57c00;
        }
        .role-judge { 
            background: #f3e5f5; 
            color: #7b1fa2; 
            border: 1px solid #7b1fa2;
        }
        
        .search-info { background: transparent; padding: 20px 0 0 0; margin-top: 25px; font-size: 14px; color: #000000; font-style: italic; border-top: 1px solid #000000; }
        .search-info strong { font-weight: normal; font-style: normal; text-transform: uppercase; letter-spacing: 2px; display: block; margin-bottom: 10px; font-size: 13px; }
        .search-query { display: inline; margin-right: 12px; }
        .search-query:not(:last-child)::after { content: " • "; }
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.95); cursor: pointer; }
        .modal-content { margin: auto; display: block; max-width: 90%; max-height: 90%; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); border: 1px solid #000000; filter: grayscale(100%); }
        .modal-close { position: absolute; top: 20px; right: 40px; color: white; font-size: 40px; font-weight: bold; cursor: pointer; }
        .footer { text-align: center; color: #000000; padding: 30px 0; font-size: 14px; border-top: 1px solid #000000; margin-top: 40px; font-style: italic; letter-spacing: 1px; }
    </style>
</head>
<body>
    <div class="layout">
        <div class="main-content">
            <div class="container">
                <div class="header">
                    <div class="header-date" id="headerDate"></div>
                    <h1>AI Театр</h1>
                    <div class="header-subtitle">Спектакль нейросетей • Акт I</div>
                    <div class="header-topic" id="topicDisplay">—</div>
                </div>
                <div class="card" id="setupCard">
                    <h2>Персонажи</h2>
                    <div class="participants-grid" id="participantsSetup"></div>
                </div>
                <div class="card" id="topicCard">
                    <h2>Сюжет</h2>
                    <div class="input-group">
                        <textarea id="topicInput" rows="3" placeholder="Опишите сюжет сцены..."></textarea>
                    </div>
                    <div id="modelsWarning" style="display:none;margin-bottom:20px;padding:15px;border:2px solid #b00020;color:#b00020;font-size:15px;line-height:1.5;"></div>
                    <div id="vramWarning" style="display:none;margin-bottom:20px;padding:15px;border:2px solid #b8860b;color:#8a6d00;font-size:15px;line-height:1.5;"></div>
                    <button class="btn btn-primary" id="startBtn" onclick="startDebate()">🎭 Начать спектакль</button>
                    <button class="btn btn-secondary" id="newBtn" onclick="resetDebate()" style="display:none;">🎭 Новый спектакль</button>
                    <div style="margin-top:10px;font-size:12px;color:#666;">Ctrl+Enter для отправки</div>
                </div>
                <div class="card" id="staticInstructionsCard">
                    <h2>Правила общения</h2>
                    <p style="font-size:14px;color:#666;margin-bottom:15px;font-style:italic;">Эти правила будут добавлены в системный промпт для всех участников. Вы можете использовать плейсхолдеры:</p>
                    <div style="font-size:13px;color:#333;margin-bottom:15px;padding:10px;background:#f9f9f9;border:1px solid #ddd;">
                        <strong>Доступные плейсхолдеры:</strong><br>
                        <code>{ИМЯ}</code> — имя текущего участника<br>
                        <code>{СОБЕСЕДНИКИ}</code> — список других участников через запятую<br>
                        <code>{ТЕМА}</code> — тема обсуждения
                    </div>
                    <div id="staticInstructionsList"></div>
                    <button class="btn btn-secondary" onclick="addStaticInstruction()" style="margin-top:10px;">➕ Добавить правило</button>
                </div>
                <div id="posts"></div>
                <div id="moderatorPanel" style="display:none; margin-top:30px; padding:20px; border:2px solid #000000;">
                    <h3 style="margin:0 0 15px 0; font-size:20px; text-transform:uppercase; letter-spacing:2px;">Ваша реплика, режиссёр</h3>
                    <textarea id="moderatorInput" rows="4" style="width:100%; padding:12px; border:2px solid #000000; font-size:16px; font-family:Georgia,serif; margin-bottom:15px;" placeholder="Напишите реплику или оставьте пустым чтобы пропустить действие..."></textarea>
                    <div style="display:flex; gap:15px; margin-bottom:20px;">
                        <button class="btn btn-primary" onclick="sendModeratorMessage()">Отправить</button>
                        <button class="btn btn-secondary" id="finishBtn" onclick="finishDebate()">Завершить спектакль</button>
                    </div>
                    <div style="display:flex; gap:10px; align-items:center; margin-bottom:15px;">
                        <input id="topicChangeInput" type="text" style="flex:1; padding:10px; border:2px solid #000000; font-size:16px; font-family:Georgia,serif;" placeholder="Сменить тему обсуждения..." onkeydown="if (event.key === 'Enter') { event.preventDefault(); changeTopic(); }">
                        <button class="btn btn-secondary" onclick="changeTopic()" style="margin:0;">🎯 Сменить тему</button>
                    </div>
                    <div style="margin-top:10px; font-size:12px; font-style:italic; margin-bottom:20px;">💡 Пустое сообщение = пропуск действия • Ctrl+Enter для отправки • смена темы сразу попадает в системные промпты участников</div>
                    
                    <!-- Панель редактирования инструкций и руководств -->
                    <div style="border-top:1px solid #000; padding-top:20px; margin-top:20px;">
                        <h4 style="margin:0 0 10px 0; font-size:16px; text-transform:uppercase; letter-spacing:1px;">📋 Управление инструкциями</h4>
                        <button class="btn btn-secondary" onclick="toggleInstructionsEditor()" style="margin-bottom:15px;">🔧 Редактировать инструкции и руководства</button>
                        
                        <div id="instructionsEditor" style="display:none;">
                            <div style="margin-bottom:20px;">
                                <label style="display:block; font-weight:bold; margin-bottom:8px; font-size:14px;">Правила общения (для всех участников):</label>
                                <div style="font-size:12px;color:#666;margin-bottom:10px;">Плейсхолдеры: <code>{ИМЯ}</code>, <code>{СОБЕСЕДНИКИ}</code>, <code>{ТЕМА}</code></div>
                                <div id="staticInstructionsEditor"></div>
                                <button class="btn btn-secondary" onclick="addStaticInstructionEditor()" style="margin-top:10px; padding:6px 15px; font-size:14px;">➕ Добавить правило</button>
                            </div>
                            
                            <div style="margin-bottom:20px;">
                                <label style="display:block; font-weight:bold; margin-bottom:8px; font-size:14px;">Руководства (указания модератора):</label>
                                <div id="moderatorMessagesEditor"></div>
                                <button class="btn btn-secondary" onclick="addModeratorMessageEditor()" style="margin-top:10px; padding:6px 15px; font-size:14px;">➕ Добавить руководство</button>
                            </div>
                            
                            <div style="margin-bottom:20px;">
                                <label style="display:block; font-weight:bold; margin-bottom:8px; font-size:14px;">⚖️ Правила для роли судьи:</label>
                                <div style="font-size:12px;color:#666;margin-bottom:10px;">Плейсхолдеры: <code>{ИМЯ}</code>, <code>{СОБЕСЕДНИКИ}</code>, <code>{ТЕМА}</code></div>
                                <div id="judgeRulesEditor"></div>
                                <button class="btn btn-secondary" onclick="addJudgeRuleEditor()" style="margin-top:10px; padding:6px 15px; font-size:14px;">➕ Добавить правило судьи</button>
                                <div style="font-size:12px;color:#666;margin-top:10px;font-style:italic;">Личный системный промпт судьи — ниже, в блоке «Индивидуальные инструкции».</div>
                            </div>
                            
                            <div style="margin-bottom:20px;">
                                <label style="display:block; font-weight:bold; margin-bottom:8px; font-size:14px;">Индивидуальные инструкции участников:</label>
                                <div id="participantInstructionsEditor"></div>
                            </div>
                            
                            <button class="btn btn-primary" onclick="saveInstructions()" style="margin-top:10px;">💾 Применить изменения</button>
                        </div>
                    </div>
                </div>
                <div class="footer">
                    <button class="btn btn-secondary" onclick="shutdownServer(false)">Покинуть театр</button>
                </div>
            </div>
        </div>
        <div class="sidebar">
            <div class="sidebar-section">
                <div class="sidebar-title">Статус</div>
                <div id="statusBar" class="status-bar" style="display:none;"></div>
                <div id="statusPlaceholder" style="color:#666;font-size:13px;">Ожидание начала...</div>
            </div>
            <div class="sidebar-section">
                <div class="sidebar-title">Память GPU</div>
                <div id="vramDisplay" style="color:#000000;font-size:13px;line-height:1.6;">—</div>
            </div>
            <div class="sidebar-section">
                <div class="sidebar-title">Персонажи</div>
                <div id="participantsDisplay" style="color:#000000;font-size:13px;line-height:1.6;"></div>
            </div>
            <div class="sidebar-section">
                <div class="sidebar-title">Правила общения</div>
                <div id="rulesDisplay" style="color:#000000;font-size:12px;line-height:1.5;font-style:italic;"></div>
            </div>
            <div class="sidebar-section">
                <div class="sidebar-title">Инструкции от руководства</div>
                <div id="moderatorInstructionsDisplay" style="color:#000000;font-size:12px;line-height:1.5;font-weight:bold;"></div>
            </div>
        </div>
    </div>
    <div id="avatarModal" class="modal" onclick="closeAvatarModal()">
        <span class="modal-close">&times;</span>
        <img class="modal-content" id="avatarModalImg">
    </div>
    <!-- Клиент Socket.IO лежит рядом с проектом: свежие посты приходят сразу,
         а опрос /api/status остаётся страховкой -->
    <script src="/static/socket.io.min.js"></script>
    <script>
        function escapeHtml(s) {
            return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
        }
        
        let participants = [];
        let debateRunning = false;
        let pollInterval = null;
        let lastPostCount = 0;
        let staticInstructions = [];
        let instructionsTick = 0;
        let defaultJudgePrompt = '';  // им заполняется пустое поле промпта судьи
        
        // Загружаем участников и статичные инструкции
        fetch('/api/participants')
            .then(r => r.json())
            .then(data => { participants = data.participants; renderParticipantsSetup(); renderModelsWarning(data.models_status); renderVramWarning(data.vram_status); });
        
        refreshMemory();  // сразу видно, что уже загружено в Ollama (могут быть чужие модели)
        
        // Socket.IO - ускоритель: по событию new_post сразу тянем статус, поэтому
        // реплика появляется без задержки в 3 секунды. Если клиент не загрузился,
        // страница молча живёт на polling'е.
        let socket = null;
        if (typeof io === 'function') {
            try {
                socket = io();
                ['new_post', 'state_update'].forEach(evt =>
                    socket.on(evt, () => { if (debateRunning) updatePosts(); }));
            } catch (e) {
                console.warn('Socket.IO недоступен, обновляемся опросом:', e);
                socket = null;
            }
        }
        
        fetch('/api/static_instructions')
            .then(r => r.json())
            .then(data => { staticInstructions = data.static_instructions; renderStaticInstructions(); });
        
        function renderStaticInstructions() {
            const container = document.getElementById('staticInstructionsList');
            container.innerHTML = staticInstructions.map((instr, idx) => `
                <div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;">
                    <textarea id="static-instr-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" onchange="updateStaticInstruction(${idx}, this.value)">${instr}</textarea>
                    <button class="btn btn-secondary" onclick="removeStaticInstruction(${idx})" style="padding:8px 12px;margin:0;">❌</button>
                </div>
            `).join('');
        }
        
        function updateStaticInstruction(idx, value) {
            staticInstructions[idx] = value;
        }
        
        function addStaticInstruction() {
            staticInstructions.push('Новая инструкция...');
            renderStaticInstructions();
        }
        
        function removeStaticInstruction(idx) {
            staticInstructions.splice(idx, 1);
            renderStaticInstructions();
        }
        
        function renderModelsWarning(status) {
            const box = document.getElementById('modelsWarning');
            if (!box) return;
            const missing = status && status.missing ? status.missing : [];
            if (!status || (status.ok && missing.length === 0)) {
                box.style.display = 'none';
                box.innerHTML = '';
                return;
            }
            box.innerHTML = `⚠️ ${
                status.error
                    ? `${status.error}. Проверьте, что Ollama запущена.`
                    : `В Ollama нет моделей: <b>${missing.join(', ')}</b>. Скачайте: <code>${missing.map(m => 'ollama pull ' + m).join(' ; ')}</code>`
            }`;
            box.style.display = 'block';
        }
        
        function renderParticipantsSetup() {
            const container = document.getElementById('participantsSetup');
            container.innerHTML = participants.map((p, idx) => {
                // Определяем роль и стиль
                let roleBadge = '';
                if (p.is_moderator) {
                    roleBadge = '<span class="role-badge role-moderator" style="position:absolute;top:10px;right:10px;">🎬 МОДЕРАТОР</span>';
                } else if (p.is_judge) {
                    roleBadge = '<span class="role-badge role-judge" style="position:absolute;top:10px;right:10px;">⚖️ СУДЬЯ</span>';
                } else {
                    roleBadge = '<span class="role-badge role-participant" style="position:absolute;top:10px;right:10px;">🎭 УЧАСТНИК</span>';
                }
                
                return `
                <div class="participant-card" style="position:relative;">
                    ${roleBadge}
                    <div class="avatar-container">
                        <div class="avatar-preview" id="avatar-preview-${idx}" onclick="openAvatarModal(${idx})">
                            ${p.avatar_url ? `<img src="${p.avatar_url}">` : (p.avatar_emoji || (p.is_moderator ? '🎬' : (p.is_judge ? '⚖️' : '📣')))}
                        </div>
                    </div>
                    <div class="input-group">
                        <label>Имя</label>
                        <input type="text" id="name-${idx}" value="${p.display_name || ''}" ${debateRunning ? 'readonly' : ''} placeholder="Введите имя" onchange="updateParticipant(${idx}, 'display_name', this.value)">
                    </div>
                    <div class="input-group">
                        <label>Пол</label>
                        <select id="gender-${idx}" ${debateRunning ? 'disabled' : ''} onchange="updateParticipant(${idx}, 'gender', this.value)" style="width:100%; padding:10px; border:1px solid #000000; font-size:18px; background:#ffffff; color:#000000; font-family:Georgia,serif;">
                            <option value="male" ${p.gender === 'male' ? 'selected' : ''}>♂ Мужской</option>
                            <option value="female" ${p.gender === 'female' ? 'selected' : ''}>♀ Женский</option>
                        </select>
                    </div>
                    <div class="input-group">
                        <label>Ключевые слова для аватара</label>
                        <input type="text" id="keywords-${idx}" value="${p.avatar_keywords || ''}" ${debateRunning ? 'readonly' : ''} placeholder="Например: философ учёный" onchange="updateParticipant(${idx}, 'avatar_keywords', this.value)">
                    </div>
                    <button class="btn btn-secondary" onclick="searchAvatar(${idx})" ${debateRunning ? 'disabled' : ''}>🔍 Найти аватар</button>
                    <div class="input-group" style="margin-top:15px;">
                        <label>Инструкция</label>
                        <textarea id="instruction-${idx}" ${debateRunning ? 'readonly' : ''} placeholder="Дополнительная инструкция..." onchange="updateParticipant(${idx}, 'instruction', this.value)">${p.instruction || ''}</textarea>
                    </div>
                    <div style="font-size:12px;color:#999;margin-top:10px;">Модель: ${p.model} | Пол: ${p.gender === 'male' ? '♂' : '♀'}</div>
                </div>
                `;
            }).join('');
        }
        
        function updateParticipant(idx, field, value) { participants[idx][field] = value; }
        
        function searchAvatar(idx) {
            const keywords = participants[idx].avatar_keywords || participants[idx].display_name;
            if (!keywords) { alert('Сначала введите имя или ключевые слова для аватара'); return; }
            
            // Блокируем кнопку во время загрузки
            const btn = event.target;
            if (btn.disabled) return;
            btn.disabled = true;
            btn.textContent = '⏳ Загрузка...';
            
            const preview = document.getElementById(`avatar-preview-${idx}`);
            const fallbackEmoji = participants[idx].avatar_emoji || (participants[idx].is_moderator ? '🎬' : '📣');
            preview.innerHTML = '⏳';
            
            fetch(`/api/avatar/${encodeURIComponent(keywords)}`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ keywords: keywords, participant_idx: idx })
            }).then(r => r.json()).then(data => {
                if (data.avatar_url) { participants[idx].avatar_url = data.avatar_url; preview.innerHTML = `<img src="${data.avatar_url}">`; }
                else { preview.innerHTML = '❌'; setTimeout(() => preview.innerHTML = fallbackEmoji, 2000); }
                btn.disabled = false;
                btn.textContent = '🔍 Найти аватар';
            }).catch(err => { console.error('Ошибка поиска аватара:', err); preview.innerHTML = '❌'; setTimeout(() => preview.innerHTML = fallbackEmoji, 2000); btn.disabled = false; btn.textContent = '🔍 Найти аватар'; });
        }
        
        function openAvatarModal(idx) { const u = participants[idx].avatar_url; if (u) { document.getElementById('avatarModalImg').src = u; document.getElementById('avatarModal').style.display = 'block'; } }
        function closeAvatarModal() { document.getElementById('avatarModal').style.display = 'none'; }
        
        function startDebate() {
            const topic = document.getElementById('topicInput').value.trim();
            if (!topic) { alert('Введите тему!'); return; }
            participants.forEach((p, idx) => { const n = document.getElementById(`name-${idx}`); const k = document.getElementById(`keywords-${idx}`); if (n) p.display_name = n.value.trim(); if (k) p.avatar_keywords = k.value.trim(); });
            if (participants.filter(p => !p.display_name).length > 0) { alert('У всех участников должны быть имена!'); return; }
            debateRunning = true;
            document.getElementById('startBtn').disabled = true;
            document.getElementById('newBtn').style.display = 'none';
            document.getElementById('posts').innerHTML = '';
            document.getElementById('setupCard').style.display = 'none';
            document.getElementById('topicCard').style.display = 'none';
            document.getElementById('staticInstructionsCard').style.display = 'none';
            renderParticipantsSetup();
            const instructions = {};
            participants.forEach(p => { const el = document.getElementById(`instruction-${participants.indexOf(p)}`); if (el && el.value.trim()) instructions[p.display_name] = el.value.trim(); });
            lastPostCount = 0;
            document.getElementById('topicDisplay').textContent = topic;
            // Пол и роль рисует общий рендер сайдбара: раньше здесь был свой вариант
            // без бейджа роли, и сайдбар «переключался» только со следующим опросом
            updateSidebarParticipants();
            const avatars = {}; participants.forEach(p => { if (p.avatar_url) avatars[p.display_name] = p.avatar_url; });
            // Собираем статичные инструкции из формы
            const currentStaticInstructions = [];
            staticInstructions.forEach((_, idx) => {
                const el = document.getElementById(`static-instr-${idx}`);
                if (el && el.value.trim()) currentStaticInstructions.push(el.value.trim());
            });
            
            fetch('/api/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ topic, instructions, participants, avatars, static_instructions: currentStaticInstructions }) })
            .then(r => r.json()).then(data => { 
                if (data.success) {
                    // Спектакль пошёл: убираем баннер с прошлой неудачной попытки,
                    // иначе он висел бы с устаревшим текстом до перезагрузки
                    const box = document.getElementById('modelsWarning');
                    if (box) { box.style.display = 'none'; box.innerHTML = ''; }
                    pollInterval = setInterval(updatePosts, 3000);
                    updateSidebarParticipants(); // Обновляем сайдбар при старте
                } else {
                    // Откатываем интерфейс и объясняем причину, а не оставляем его заблокированным
                    showStartError(data.error || 'неизвестная ошибка');
                }
            })
            .catch(err => { console.error('Ошибка запуска:', err); showStartError(err.message); });
        }
        
        function showStartError(message) {
            debateRunning = false;
            document.getElementById('startBtn').disabled = false;
            document.getElementById('setupCard').style.display = 'block';
            document.getElementById('topicCard').style.display = 'block';
            document.getElementById('staticInstructionsCard').style.display = 'block';
            document.getElementById('posts').innerHTML = '';
            document.getElementById('newBtn').style.display = 'none';
            renderParticipantsSetup();
            
            const box = document.getElementById('modelsWarning');
            if (box) { box.innerHTML = `⚠️ Не удалось начать спектакль: ${message}`; box.style.display = 'block'; }
            alert('Не удалось начать спектакль: ' + message);
        }
        
        function resetDebate() {
            debateRunning = false;
            document.getElementById('startBtn').disabled = false;
            document.getElementById('newBtn').style.display = 'none';
            document.getElementById('posts').innerHTML = '';
            document.getElementById('statusBar').style.display = 'none';
            document.getElementById('statusPlaceholder').style.display = 'block';
            document.getElementById('topicInput').value = '';
            document.getElementById('setupCard').style.display = 'block';
            document.getElementById('topicCard').style.display = 'block';
            document.getElementById('staticInstructionsCard').style.display = 'block';
            document.getElementById('moderatorPanel').style.display = 'none';
            const exitBtn = document.querySelector('.footer .btn'); if (exitBtn) exitBtn.style.display = 'inline-block';
            document.getElementById('topicDisplay').textContent = '—';
            document.getElementById('participantsDisplay').innerHTML = '';
            lastPostCount = 0;
            if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
            renderParticipantsSetup();
            fetch('/api/reset', {method: 'POST'}).catch(err => console.error('Ошибка сброса:', err));
        }
        
        function addPost(post) {
            const postsDiv = document.getElementById('posts');
            const emoji = post.avatar_emoji || '📣';
            let avatarHtml = post.avatar_url ? `<img src="${post.avatar_url}" onclick="showAvatarFull('${post.avatar_url}')">` : `<div class="emoji">${emoji}</div>`;
            let searchInfo = '';
            if (post.search_count > 0) { searchInfo = `<div class="search-info"><strong>Источники</strong>${post.search_queries.map(q => `<span class="search-query">"${q}"</span>`).join('')}</div>`; }
            
            // Определяем стиль для роли
            const role = post.role || 'participant';
            const roleIcon = post.role_icon || '🎭';
            const roleName = post.role_name || 'Участник';
            const roleClass = `role-${role}`;
            
            const postDiv = document.createElement('div');
            postDiv.className = `post`;
            const genderSymbol = post.gender === 'male' ? '♂' : '♀';
            postDiv.innerHTML = `<div class="post-avatar">${avatarHtml}</div><div class="post-content"><div class="post-header"><div><div class="post-author"><span class="role-badge ${roleClass}">${roleIcon} ${roleName}</span> ${post.display_name} ${genderSymbol}</div><div class="post-model">модель: ${post.model_used}</div></div><div class="post-time">${post.timestamp} | Акт ${post.round}</div></div><div class="post-text">${post.content_html || post.content}</div>${searchInfo}</div>`;
            postsDiv.appendChild(postDiv);
        }
        
        function showAvatarFull(url) { document.getElementById('avatarModalImg').src = url; document.getElementById('avatarModal').style.display = 'block'; }
        
        // Предупреждение «модель не влезает в VRAM»: показывается до старта,
        // спектакль не блокирует - просто честно говорит, что будет медленнее
        function renderVramWarning(status) {
            const box = document.getElementById('vramWarning');
            if (!box) return;
            const warnings = (status && status.warnings) || [];
            if (!warnings.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
            
            const gb = b => (b / 1e9).toFixed(1);
            const items = warnings.map(w => {
                let text = `<b>${escapeHtml(w.model)}</b>: при num_ctx ${status.num_ctx} нужно ~${gb(w.need)} ГБ, `
                    + `а доступно ~${gb(status.budget)} ГБ из ${gb(status.gpu_total)} ГБ. `;
                if (w.safe_ctx) {
                    text += `Поставьте num_ctx <b>${w.safe_ctx}</b> (тогда ~${gb(w.safe_need)} ГБ)`;
                } else {
                    text += 'Даже с минимальным контекстом модель не влезает целиком — часть будет считать процессор';
                }
                if (!w.measured) text += ' <span style="opacity:.7;">[оценка по размеру файла]</span>';
                return `<div style="margin-bottom:8px;">${text}</div>`;
            }).join('');
            
            box.innerHTML = `⚠️ <b>Не хватает видеопамяти</b><div style="margin-top:8px;">${items}</div>`
                + '<div style="margin-top:8px;font-size:13px;">Спектакль пойдёт и так, но такие модели будут считать медленнее: уменьшите <code>num_ctx</code> в OPTIONS.</div>';
            box.style.display = 'block';
        }
        
        function renderLoadedModels(loaded, gpu, error) {
            const el = document.getElementById('vramDisplay');
            if (!el) return;
            const gb = b => (b / 1e9).toFixed(1);
            const esc = escapeHtml;
            
            if (error) { el.innerHTML = '<div style="color:#b00020;">Ollama недоступна</div>'; return; }
            if (!loaded || loaded.length === 0) { el.innerHTML = '<div style="color:#999;">В памяти сейчас ничего нет</div>'; return; }
            
            const total = (gpu && gpu.total) ? gpu.total : 0;
            el.innerHTML = loaded.map(m => {
                const vram = m.size_vram || 0;
                const size = vram ? `${gb(vram)} ГБ в VRAM` : `${gb(m.size)} ГБ в RAM`;
                const pct = (vram && total) ? ` <span style="color:#666;">(${Math.round(vram / total * 100)}% из ${gb(total)} ГБ)</span>` : '';
                const ctx = m.context_length ? `<div style="color:#666;font-size:12px;">контекст: ${m.context_length}</div>` : '';
                // Не поместилась в VRAM: либо Ollama сама это сказала, либо заявила
                // больше, чем есть на карте (на Windows часть уходит в общую память)
                let spillBytes = 0;
                if (m.size && vram) {
                    if (total && vram > total) spillBytes = Math.max(0, m.size - total);
                    else if (vram < m.size) spillBytes = m.size - vram;
                }
                const spill = spillBytes
                    ? `<div style="color:#b00020;font-size:12px;">⚠️ ещё ${gb(spillBytes)} ГБ в RAM</div>` : '';
                return `<div style="margin-bottom:10px;"><strong>${esc(m.name)}</strong><div>${size}${pct}</div>${ctx}${spill}</div>`;
            }).join('');
            if (total) {
                el.innerHTML += `<div style="color:#666;font-size:12px;border-top:1px solid #000;padding-top:8px;">Занято на GPU: ${gb(gpu.used)} из ${gb(total)} ГБ</div>`;
            }
        }
        
        // Обновить только блок памяти: он нужен и до спектакля, и после занавеса,
        // когда polling уже остановлен, а модели как раз выгружаются
        function refreshMemory() {
            fetch('/api/status', {cache: 'no-store'}).then(r => r.json())
                .then(d => renderLoadedModels(d.loaded_models, d.gpu_memory, d.loaded_models_error))
                .catch(() => {});
        }
        
        // После занавеса модели выгружаются НЕ мгновенно (гигабайты уходят
        // в память не сразу), поэтому обновляем панель ещё несколько раз,
        // а не двумя разовыми замерами
        let memorySettleTimer = null;
        let memorySettleTicks = 0;
        
        function settleMemoryPanel() {
            memorySettleTicks = 8;  // ~40 секунд наблюдения
            if (memorySettleTimer) return;
            memorySettleTimer = setInterval(() => {
                refreshMemory();
                if (--memorySettleTicks <= 0) {
                    clearInterval(memorySettleTimer);
                    memorySettleTimer = null;
                }
            }, 5000);
            refreshMemory();
        }
        
        let statusRequestInFlight = false;
        
        function updatePosts() {
            // Опрос и событие Socket.IO могут сработать одновременно, а запрос несёт
            // lastPostCount: два параллельных ответа добавили бы один пост дважды.
            // Второй вызов пропускаем - следующий опрос всё равно подхватит новое.
            if (statusRequestInFlight) return;
            statusRequestInFlight = true;
            
            fetch(`/api/status?lastPostCount=${lastPostCount}`, {cache: 'no-store'}).then(r => r.json()).then(data => {
                const statusDiv = document.getElementById('statusBar');
                const statusPlaceholder = document.getElementById('statusPlaceholder');
                const moderatorPanel = document.getElementById('moderatorPanel');
                statusDiv.style.display = 'block'; statusPlaceholder.style.display = 'none';
                if (data.topic) document.getElementById('topicDisplay').textContent = data.topic;
                if (data.waiting_for_human) {
                    if (moderatorPanel.style.display !== 'block') { moderatorPanel.style.display = 'block'; const mi = document.getElementById('moderatorInput'); if (mi && !mi.value.trim()) mi.focus(); }
                    const finishBtn = document.getElementById('finishBtn');
                    if (finishBtn) finishBtn.style.display = data.current_participant_is_moderator ? 'inline-block' : 'none';
                    statusDiv.classList.add('active');
                    statusDiv.innerHTML = `<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">Акт ${data.current_round}</div><div>${data.current_participant}</div><div style="font-style:italic;font-size:12px;margin-top:8px;">Ваш ход!</div>`;
                } else { moderatorPanel.style.display = 'none'; }
                if (data.running && !data.waiting_for_human) {
                    statusDiv.classList.add('active');
                    let at = data.current_action === 'searching' ? `Ищет: "${data.search_query}"` : data.current_action === 'waiting' ? 'Готовит реплику...' : 'Говорит реплику...';
                    statusDiv.innerHTML = `<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">Акт ${data.current_round}</div><div>${data.current_participant}</div><div style="font-style:italic;font-size:12px;margin-top:8px;">${at}</div>`;
                } else if (data.finished) {
                    statusDiv.classList.remove('active');
                    statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;">🎭 Занавес</div>';
                    document.getElementById('startBtn').disabled = false;
                    document.getElementById('newBtn').style.display = 'inline-block';
                    moderatorPanel.style.display = 'none';
                    const eb = document.querySelector('.footer .btn'); if (eb) eb.style.display = 'none';
                    clearInterval(pollInterval);
                    pollInterval = null;
                    settleMemoryPanel();
                }
                if (data.new_posts && data.new_posts.length > 0) data.new_posts.forEach(post => addPost(post));
                if (typeof data.total_posts === 'number') lastPostCount = data.total_posts;
                // Показываем, какая модель сейчас в памяти и сколько занимает
                renderLoadedModels(data.loaded_models, data.gpu_memory, data.loaded_models_error);
                // Инструкции в сайдбаре меняются только вручную, поэтому обновляем
                // их раз в 30 секунд, а не на каждом опросе
                if (instructionsTick++ % 10 === 0) updateSidebarParticipants();
            }).catch(err => {
                console.error('Ошибка обновления статуса:', err);
                // Если сервер недоступен — значит он остановлен
                // Обновляем статус и скрываем кнопку выхода
                const statusDiv = document.getElementById('statusBar');
                statusDiv.style.display = 'block';
                statusDiv.classList.remove('active');
                statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">🎭 Театр закрыт</div><div style="font-style:italic;">Спектакль сохранён для просмотра</div>';
                
                const exitBtn = document.querySelector('.footer .btn');
                if (exitBtn) exitBtn.style.display = 'none';
                
                // Останавливаем polling чтобы не спамить ошибками
                if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
            })
            .finally(() => { statusRequestInFlight = false; });
        }
        
        // Смена темы на ходу режиссёра: сервер вернёт новую тему, её подхватят
        // и системные промпты следующих реплик, и заголовок страницы
        function changeTopic() {
            const input = document.getElementById('topicChangeInput');
            const topic = input.value.trim();
            if (!topic) { alert('Введите новую тему'); return; }
            
            fetch('/api/moderator/topic', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({topic: topic}) })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        input.value = '';
                        document.getElementById('topicDisplay').textContent = data.topic;
                        alert('✅ Тема изменена: ' + data.topic);
                    } else {
                        alert('❌ ' + (data.error || 'не удалось сменить тему'));
                    }
                })
                .catch(err => { console.error('Ошибка смены темы:', err); alert('❌ ' + err.message); });
        }
        
        function sendModeratorMessage() {
            const input = document.getElementById('moderatorInput');
            fetch('/api/moderator/message', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: input.value}) })
            .then(r => r.json()).then(data => { if (data.success) { input.value = ''; document.getElementById('moderatorPanel').style.display = 'none'; } else alert('Ошибка: ' + (data.error || 'неизвестная')); })
            .catch(err => { console.error('Ошибка:', err); alert('Ошибка: ' + err.message); });
        }
        
        function updateSidebarParticipants() {
            // Получаем актуальные инструкции с сервера
            fetch('/api/moderator/instructions')
            .then(r => r.json())
            .then(data => {
                // Создаём словарь индивидуальных инструкций
                const currentInstructions = {};
                if (data.participant_instructions) {
                    data.participant_instructions.forEach(p => {
                        if (p.instruction && p.instruction.trim()) {
                            currentInstructions[p.name] = p.instruction;
                        }
                    });
                }
                
                // Обновляем отображение персонажей в сайдбаре
                document.getElementById('participantsDisplay').innerHTML = participants.map(p => {
                    const instruction = currentInstructions[p.display_name];
                    const genderSymbol = p.gender === 'male' ? '♂' : '♀';
                    
                    // Определяем иконку роли
                    let roleIcon = '🎭';
                    let roleLabel = ' <span style="color:#1976d2;font-size:11px;font-weight:bold;">УЧАСТНИК</span>';
                    if (p.is_moderator) {
                        roleIcon = '🎬';
                        roleLabel = ' <span style="color:#f57c00;font-size:11px;font-weight:bold;">МОДЕРАТОР</span>';
                    } else if (p.is_judge) {
                        roleIcon = '⚖️';
                        roleLabel = ' <span style="color:#7b1fa2;font-size:11px;font-weight:bold;">СУДЬЯ</span>';
                    }
                    
                    // Как и в ленте: пол сразу после имени
                    let html = `<div style="margin-bottom:12px;">${roleIcon} <strong>${p.display_name}</strong> ${genderSymbol}${roleLabel} <small>(${p.model})</small>`;
                    if (instruction) {
                        html += `<br><em style="margin-left:10px;">${instruction}</em>`;
                    }
                    return html + '</div>';
                }).join('');
                
                // Обновляем блок "Правила общения"
                const rulesDisplay = document.getElementById('rulesDisplay');
                if (data.static_instructions && data.static_instructions.length > 0) {
                    rulesDisplay.innerHTML = data.static_instructions
                        .filter(rule => rule.trim())
                        .map(rule => `<div style="margin-bottom:8px;">• ${rule}</div>`)
                        .join('');
                } else {
                    rulesDisplay.innerHTML = '<div style="color:#999;">Правила не заданы</div>';
                }
                
                // Обновляем блок "Инструкции от руководства"
                const modInstructionsDisplay = document.getElementById('moderatorInstructionsDisplay');
                if (data.moderator_messages && data.moderator_messages.length > 0) {
                    modInstructionsDisplay.innerHTML = data.moderator_messages
                        .filter(msg => msg.trim())
                        .map(msg => `<div style="margin-bottom:8px;">• ${msg}</div>`)
                        .join('');
                } else {
                    modInstructionsDisplay.innerHTML = '<div style="color:#999;font-weight:normal;">Нет указаний от руководства</div>';
                }
            })
            .catch(err => console.error('Ошибка обновления сайдбара:', err));
        }
        
        function saveInstructions() {
            // Собираем static_instructions
            const staticInstructions = [];
            const staticContainer = document.getElementById('staticInstructionsEditor');
            staticContainer.querySelectorAll('textarea').forEach(ta => {
                if (ta.value.trim()) staticInstructions.push(ta.value.trim());
            });
            
            // Собираем moderator_messages
            const moderatorMessages = [];
            const modContainer = document.getElementById('moderatorMessagesEditor');
            modContainer.querySelectorAll('textarea').forEach(ta => {
                if (ta.value.trim()) moderatorMessages.push(ta.value.trim());
            });
            
            // Собираем индивидуальные инструкции участников
            const participantInstructions = [];
            const participantContainer = document.getElementById('participantInstructionsEditor');
            participantContainer.querySelectorAll('div[data-participant]').forEach(div => {
                const textarea = div.querySelector('textarea');
                const name = (div.dataset.participant || '').trim();
                if (textarea && name) {
                    participantInstructions.push({ name: name, instruction: textarea.value });
                }
            });
            
            // Собираем правила судьи
            const judgeRules = [];
            document.getElementById('judgeRulesEditor').querySelectorAll('textarea').forEach(ta => {
                if (ta.value.trim()) judgeRules.push(ta.value.trim());
            });
            
            // Отправляем на сервер
            fetch('/api/moderator/instructions', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    static_instructions: staticInstructions,
                    moderator_messages: moderatorMessages,
                    judge_rules: judgeRules,
                    participant_instructions: participantInstructions
                })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    alert('✅ Инструкции обновлены!');
                    document.getElementById('instructionsEditor').style.display = 'none';
                    // Обновляем сайдбар с актуальными инструкциями
                    updateSidebarParticipants();
                } else {
                    alert('❌ Ошибка: ' + (data.error || 'неизвестная'));
                }
            })
            .catch(err => {
                console.error('Ошибка сохранения:', err);
                alert('❌ Ошибка сохранения: ' + err.message);
            });
        }
        
        function finishDebate() {
            if (confirm('Завершить дебаты и выйти?')) {
                // СРАЗУ обновляем UI, не дожидаясь сервера
                document.getElementById('moderatorPanel').style.display = 'none';
                
                const statusDiv = document.getElementById('statusBar');
                statusDiv.style.display = 'block';
                statusDiv.classList.remove('active');
                statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">🎭 Спектакль завершён</div><div style="font-style:italic;">Режиссёр завершил представление</div>';
                document.getElementById('statusPlaceholder').style.display = 'none';
                
                // СРАЗУ скрываем кнопку выхода
                const exitBtn = document.querySelector('.footer .btn');
                if (exitBtn) exitBtn.style.display = 'none';
                
                // Останавливаем polling
                if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
                
                // Отправляем запросы на сервер (если он ещё жив)
                fetch('/api/moderator/finish', {method: 'POST'}).catch(() => {});
                shutdownServer(true);
            }
        }
        
        function shutdownServer(skipConfirm = false) {
            if (skipConfirm || confirm('Завершить работу сервера?')) {
                // СРАЗУ останавливаем polling
                if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
                
                // СРАЗУ скрываем кнопку выхода — чтобы нельзя было нажать повторно
                const exitBtn = document.querySelector('.footer .btn');
                if (exitBtn) exitBtn.style.display = 'none';
                
                // СРАЗУ обновляем UI
                document.getElementById('setupCard').style.display = 'none';
                document.getElementById('topicCard').style.display = 'none';
                document.getElementById('moderatorPanel').style.display = 'none';
                
                const statusDiv = document.getElementById('statusBar');
                statusDiv.style.display = 'block';
                statusDiv.classList.remove('active');
                statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">🎭 Театр закрыт</div><div style="font-style:italic;">Спектакль сохранён для просмотра</div>';
                
                // Отправляем запрос на сервер (если он ещё жив)
                fetch('/api/shutdown', {method: 'POST'}).catch(() => {});
            }
        }
        
        // Функции для редактирования инструкций и руководств
        function toggleInstructionsEditor() {
            const editor = document.getElementById('instructionsEditor');
            if (editor.style.display === 'none') {
                editor.style.display = 'block';
                loadInstructionsForEdit();
            } else {
                editor.style.display = 'none';
            }
        }
        
        function loadInstructionsForEdit() {
            fetch('/api/moderator/instructions')
            .then(r => r.json())
            .then(data => {
                defaultJudgePrompt = data.default_judge_prompt || '';
                renderStaticInstructionsEditor(data.static_instructions);
                renderModeratorMessagesEditor(data.moderator_messages);
                renderJudgeRulesEditor(data.judge_rules || []);
                renderParticipantInstructionsEditor(data.participant_instructions || []);
            })
            .catch(err => console.error('Ошибка загрузки инструкций:', err));
        }
        
        function renderStaticInstructionsEditor(instructions) {
            const container = document.getElementById('staticInstructionsEditor');
            container.innerHTML = instructions.map((instr, idx) => `
                <div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;">
                    <textarea id="static-instr-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;">${instr}</textarea>
                    <button class="btn btn-secondary" onclick="removeStaticInstructionEditor(${idx})" style="padding:8px 12px;margin:0;">❌</button>
                </div>
            `).join('');
        }
        
        function renderModeratorMessagesEditor(messages) {
            const container = document.getElementById('moderatorMessagesEditor');
            container.innerHTML = messages.map((msg, idx) => `
                <div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;">
                    <textarea id="mod-msg-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;">${msg}</textarea>
                    <button class="btn btn-secondary" onclick="removeModeratorMessageEditor(${idx})" style="padding:8px 12px;margin:0;">❌</button>
                </div>
            `).join('');
        }
        
        function renderJudgeRulesEditor(rules) {
            const container = document.getElementById('judgeRulesEditor');
            container.innerHTML = rules.map((rule, idx) => `
                <div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;">
                    <textarea id="judge-rule-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;">${escapeHtml(rule)}</textarea>
                    <button class="btn btn-secondary" onclick="removeJudgeRuleEditor(${idx})" style="padding:8px 12px;margin:0;">❌</button>
                </div>
            `).join('');
        }
        
        function addJudgeRuleEditor() {
            const container = document.getElementById('judgeRulesEditor');
            const idx = container.children.length;
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;';
            div.innerHTML = `
                <textarea id="judge-rule-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Новое правило судьи..."></textarea>
                <button class="btn btn-secondary" onclick="removeJudgeRuleEditor(${idx})" style="padding:8px 12px;margin:0;">❌</button>
            `;
            container.appendChild(div);
        }
        
        function removeJudgeRuleEditor(idx) {
            const el = document.getElementById(`judge-rule-edit-${idx}`);
            if (el) el.parentElement.remove();
        }
        
        function renderParticipantInstructionsEditor(participantInstructions) {
            const container = document.getElementById('participantInstructionsEditor');
            if (participantInstructions.length === 0) {
                container.innerHTML = '<div style="color:#666;font-style:italic;font-size:13px;">Нет AI-участников для редактирования</div>';
                return;
            }
            container.innerHTML = participantInstructions.map((p, idx) => {
                // Определяем роль участника
                let roleBadge = '<span class="role-badge role-participant">🎭 УЧАСТНИК</span>';
                let borderColor = '#ccc';
                let rows = 3;
                
                if (p.is_judge) {
                    roleBadge = '<span class="role-badge role-judge">⚖️ СУДЬЯ</span>';
                    borderColor = '#7b1fa2';
                    rows = 8; // Больше строк для судьи
                }
                
                // Имя лежит в data-атрибуте: раньше его брали из текста label,
                // а туда попал бейдж роли - и инструкции сохранялись под именем
                // «🎭 УЧАСТНИК Варвара», то есть никогда не применялись
                const value = (p.instruction && p.instruction.trim())
                    ? p.instruction
                    : (p.is_judge ? defaultJudgePrompt : '');
                
                return `
                <div data-participant="${escapeHtml(p.name)}" style="margin-bottom:15px;padding:10px;border:2px solid ${borderColor};border-radius:4px;background:${p.is_judge ? '#fafafa' : 'white'};">
                    <label style="display:block;font-weight:bold;margin-bottom:5px;font-size:13px;">${roleBadge} ${escapeHtml(p.name)}:</label>
                    ${p.is_judge ? '<div style="font-size:11px;color:#666;margin-bottom:5px;font-style:italic;">Системный промпт судьи (можно редактировать):</div>' : ''}
                    <textarea id="participant-instr-edit-${idx}" rows="${rows}" style="width:100%;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Дополнительная инструкция для ${escapeHtml(p.name)}...">${escapeHtml(value)}</textarea>
                </div>
            `}).join('');
        }
        
        function addStaticInstructionEditor() {
            const container = document.getElementById('staticInstructionsEditor');
            const idx = container.children.length;
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;';
            div.innerHTML = `
                <textarea id="static-instr-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Новая инструкция..."></textarea>
                <button class="btn btn-secondary" onclick="removeStaticInstructionEditor(${idx})" style="padding:8px 12px;margin:0;">❌</button>
            `;
            container.appendChild(div);
        }
        
        function addModeratorMessageEditor() {
            const container = document.getElementById('moderatorMessagesEditor');
            const idx = container.children.length;
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;';
            div.innerHTML = `
                <textarea id="mod-msg-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Новое руководство..."></textarea>
                <button class="btn btn-secondary" onclick="removeModeratorMessageEditor(${idx})" style="padding:8px 12px;margin:0;">❌</button>
            `;
            container.appendChild(div);
        }
        
        function removeStaticInstructionEditor(idx) {
            const el = document.getElementById(`static-instr-edit-${idx}`);
            if (el) el.parentElement.remove();
        }
        
        function removeModeratorMessageEditor(idx) {
            const el = document.getElementById(`mod-msg-edit-${idx}`);
            if (el) el.parentElement.remove();
        }
        
        document.getElementById('topicInput').addEventListener('keydown', function(e) { if (e.ctrlKey && e.key === 'Enter') startDebate(); });
        document.getElementById('moderatorInput').addEventListener('keydown', function(e) { if (e.ctrlKey && e.key === 'Enter') sendModeratorMessage(); });
        const now = new Date();
        document.getElementById('headerDate').textContent = now.toLocaleDateString('ru-RU', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
    </script>
</body>
</html>
"""

# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.root_path, 'favicon.ico', mimetype='image/x-icon')

@app.route('/avatars/<path:filename>')
def serve_avatar(filename):
    return send_from_directory(AVATAR_DIR, filename)

@app.route('/api/static_instructions')
def get_static_instructions():
    """Возвращает дефолтные статичные инструкции"""
    return jsonify({"static_instructions": DEFAULT_STATIC_INSTRUCTIONS})

@app.route('/api/participants')
def get_participants():
    participants_data = []
    
    # Создаём копии списков для гарантии уникальности
    available_male_names = MALE_NAMES.copy()
    available_female_names = FEMALE_NAMES.copy()
    available_emojis = AVATAR_EMOJIS.copy()
    available_professions = PROFESSIONS.copy()
    
    for p in PARTICIPANTS:
        # Случайно выбираем пол
        gender = random.choice(["male", "female"])
        
        # Выбираем уникальное имя с правильной логикой
        if gender == "male":
            if available_male_names:
                name = random.choice(available_male_names)
                available_male_names.remove(name)
            elif available_female_names:
                # Если мужских имён нет, берём женское
                name = random.choice(available_female_names)
                available_female_names.remove(name)
            else:
                name = "Участник"
        else:  # gender == "female"
            if available_female_names:
                name = random.choice(available_female_names)
                available_female_names.remove(name)
            elif available_male_names:
                # Если женских имён нет, берём мужское
                name = random.choice(available_male_names)
                available_male_names.remove(name)
            else:
                name = "Участник"
        
        # Выбираем уникальную эмодзи
        if available_emojis:
            emoji = random.choice(available_emojis)
            available_emojis.remove(emoji)
        else:
            emoji = "📣"
        
        # Выбираем уникальную профессию для ключевых слов
        if available_professions:
            profession = random.choice(available_professions)
            available_professions.remove(profession)
        else:
            profession = "человек"
        
        participants_data.append({
            "model": p["model"],
            "display_name": name,
            "avatar_keywords": profession,
            "avatar_emoji": emoji,
            "gender": gender,
            "is_moderator": p.get("is_moderator", False),
            "is_judge": p.get("is_judge", False)
        })
    
    return jsonify({
        "participants": participants_data,
        # Сразу сообщаем интерфейсу, если нужных моделей нет в Ollama
        "models_status": check_models_available([p["model"] for p in PARTICIPANTS]),
        # И если они не влезают в видеопамять при текущем num_ctx
        "vram_status": check_vram_fit([p["model"] for p in PARTICIPANTS])
    })

@app.route('/api/avatar/<keywords>')
def get_avatar(keywords):
    base_name = sanitize_avatar_name(keywords)
    for avatar_file in AVATAR_DIR.glob(f"{base_name}*.jpg"):
        return jsonify({"avatar_url": f"/avatars/{avatar_file.name}"})
    return jsonify({"avatar_url": None})

@app.route('/api/avatar/<keywords>', methods=['POST'])
def refresh_avatar(keywords):
    print(f"🔍 Запрос аватара для: {keywords}")
    try:
        data = request.get_json(force=True, silent=True) or {}
        participant_idx = data.get('participant_idx')
        print(f"📋 Индекс участника: {participant_idx}")
        
        if participant_idx is None:
            return jsonify({"error": "participant_idx не указан"}), 400
        
        if not PARTICIPANTS:
            return jsonify({"error": "Нет моделей"}), 400
        
        # generate_avatar_for_participant() работает только с именем и ключевыми словами
        participant = {
            "display_name": keywords,
            "avatar_keywords": keywords
        }
        
        # Кнопка «Найти аватар» должна искать заново, а не отдавать закэшированную ссылку
        AVATAR_URL_CACHE.pop(keywords, None)
        
        avatar_url = generate_avatar_for_participant(participant)
        
        if avatar_url:
            return jsonify({"avatar_url": avatar_url})
        return jsonify({"avatar_url": None})
    except Exception as e:
        print(f"⚠️  Ошибка при поиске аватара: {e}")
        return jsonify({"avatar_url": None, "error": str(e)})

@app.route('/api/start', methods=['POST'])
def start():
    if session.running:
        return jsonify({"success": False, "error": "Уже запущено"})
    
    data = request.get_json(silent=True) or {}
    topic = data.get("topic", "") or ""
    instructions = data.get("instructions", {}) or {}
    participants_data = data.get("participants", []) or []
    avatars = data.get("avatars", {}) or {}
    static_instructions = data.get("static_instructions", []) or []
    
    if not isinstance(participants_data, list):
        participants_data = []
    participants_data = [p for p in participants_data if isinstance(p, dict)]
    
    # Собираем эмодзи из участников
    avatar_emojis = {}
    for p in participants_data:
        name = p.get("display_name", "")
        if name and p.get("avatar_emoji"):
            avatar_emojis[name] = p["avatar_emoji"]
    
    if not topic:
        return jsonify({"success": False, "error": "Тема не указана"})
    
    if not participants_data:
        return jsonify({"success": False, "error": "Не выбрано ни одного участника"})
    
    # Участник без модели не сможет говорить, а поток дебатов упал бы уже на сцене
    participants_without_model = [
        p.get("display_name") or "без имени"
        for p in participants_data
        if p.get("model") != "human" and not p.get("model")
    ]
    if participants_without_model:
        return jsonify({
            "success": False,
            "error": f"У участников не указана модель: {', '.join(participants_without_model)}"
        })
    
    # Живая проверка перед стартом: модели могли удалить, а Ollama - перезапустить
    models_status = check_models_available(
        [p.get("model", "") for p in participants_data], force=True
    )
    if not models_status["ok"]:
        problem = models_problem_message(models_status)
        print(f"⛔ Спектакль не начат: {problem}")
        # Сбрасываем running чтобы можно было попробовать снова
        session.running = False
        return jsonify({"success": False, "error": problem})
    
    print(f"🎭 Запускаем спектакль с {len(participants_data)} участниками")
    session.reset(topic, participants_data, avatars, instructions, avatar_emojis, static_instructions)
    
    print(f"🎭 Готовые персонажи: {avatars}")
    print(f"🎭 Эмодзи: {avatar_emojis}")
    print(f"🎭 Статичные инструкции: {static_instructions}")
    
    thread = threading.Thread(target=run_debate_thread, args=(topic,))
    thread.daemon = True
    thread.start()
    return jsonify({"success": True})

@app.route('/api/reset', methods=['POST'])
def reset():
    session.clear()
    return jsonify({"success": True})

@app.route('/api/status')
def status():
    last_post_count = max(0, request.args.get("lastPostCount", 0, type=int))
    # Что сейчас лежит в памяти - для сайдбара (данные кэшируются на 1.5 с)
    loaded_models, loaded_models_error = fetch_loaded_models()
    
    response = jsonify({
        "running": session.running,
        "finished": session.finished,
        "topic": session.topic,
        "new_posts": session.posts[last_post_count:],
        "total_posts": len(session.posts),
        "current_round": session.current_round,
        "current_participant": session.current_participant,
        "current_action": session.current_action,
        "search_query": session.search_query,
        "waiting_for_human": session.waiting_for_human,
        "current_participant_is_moderator": session.current_participant_is_moderator(),
        "loaded_models": loaded_models if not loaded_models_error else [],
        "loaded_models_error": loaded_models_error or "",
        "gpu_memory": fetch_gpu_memory(),
    })
    # Без этого браузер отдаёт статус из своего кэша, и панель с памятью GPU
    # «зависает» с устаревшими цифрами, пока не изменится счётчик постов
    response.headers["Cache-Control"] = "no-store"
    return response

# ============================================================
# WEBSOCKET СОБЫТИЯ
# ============================================================

# WebSocket-слой оставлен для внешних клиентов: сама страница обновляется
# через polling (/api/status?lastPostCount=...), а не через эти события.
@socketio.on('connect')
def handle_connect():
    print("🔌 Клиент подключился через WebSocket")
    emit('state_update', {
        "running": session.running,
        "finished": session.finished,
        "topic": session.topic,
        "posts": session.posts,
        "total_posts": len(session.posts),
        "current_round": session.current_round,
        "current_participant": session.current_participant,
        "current_action": session.current_action,
        "search_query": session.search_query,
        "waiting_for_human": session.waiting_for_human,
        "current_participant_is_moderator": session.current_participant_is_moderator(),
    })

@socketio.on('disconnect')
def handle_disconnect():
    print("🔌 Клиент отключился от WebSocket")

@socketio.on('request_status')
def handle_request_status():
    emit('status_update', {
        "running": session.running,
        "finished": session.finished,
        "current_round": session.current_round,
        "current_participant": session.current_participant,
        "current_action": session.current_action,
        "search_query": session.search_query,
        "waiting_for_human": session.waiting_for_human,
        "current_participant_is_moderator": session.current_participant_is_moderator(),
    })

@app.route('/api/moderator/message', methods=['POST'])
def moderator_message():
    data = request.get_json(silent=True) or {}
    session.moderator_message = data.get("message", "") or ""
    return jsonify({"success": True})

@app.route('/api/moderator/topic', methods=['POST'])
def moderator_topic():
    """Смена темы на ходу режиссёра: следующие реплики строятся уже по ней."""
    data = request.get_json(silent=True) or {}
    new_topic = str(data.get("topic", "") or "").strip()
    if not new_topic:
        return jsonify({"success": False, "error": "Тема не указана"})
    
    session.topic = new_topic
    print(f"🎬 Режиссёр сменил тему: {new_topic}")
    return jsonify({"success": True, "topic": new_topic})

@app.route('/api/moderator/finish', methods=['POST'])
def moderator_finish():
    session.moderator_finished = True
    session.waiting_for_human = False
    return jsonify({"success": True})

@app.route('/api/moderator/instructions', methods=['GET'])
def get_moderator_instructions():
    """Возвращает текущие static_instructions, moderator_messages и индивидуальные инструкции участников"""
    moderator_messages = session.moderator_messages()
    
    # Собираем индивидуальные инструкции участников
    participant_instructions = []
    for participant in session.runtime_participants:
        if participant.get("model") != "human":  # Только AI участники
            name = participant.get("display_name", "")
            instruction = session.instructions.get(name, "")
            
            participant_instructions.append({
                "name": name,
                "instruction": instruction,
                "is_judge": participant.get("is_judge", False)
            })
    
    return jsonify({
        "static_instructions": session.static_instructions or DEFAULT_STATIC_INSTRUCTIONS,
        "moderator_messages": moderator_messages,
        "participant_instructions": participant_instructions,
        # Правила судьи редактируются так же, как правила участников
        "judge_rules": session.judge_rules or DEFAULT_JUDGE_RULES,
        # Ими заполняется пустое поле личного промпта судьи в редакторе
        "default_judge_prompt": DEFAULT_JUDGE_INSTRUCTION,
        "default_static_instructions": DEFAULT_STATIC_INSTRUCTIONS,
    })

@app.route('/api/moderator/instructions', methods=['POST'])
def update_moderator_instructions():
    """Обновляет static_instructions, moderator_messages и индивидуальные инструкции участников"""
    data = request.get_json(silent=True) or {}
    
    # Обновляем static_instructions если переданы
    if isinstance(data.get("static_instructions"), list):
        session.static_instructions = [str(instr) for instr in data["static_instructions"]]
        print(f"📝 Обновлены статичные инструкции: {len(session.static_instructions)} пунктов")
        for i, instr in enumerate(session.static_instructions, 1):
            print(f"   {i}. {instr}")
    
    # Обновляем moderator_messages если переданы
    if isinstance(data.get("moderator_messages"), list):
        new_messages = data["moderator_messages"]
        
        # Удаляем старые moderator_messages из истории
        session.conversation_history = [
            post for post in session.conversation_history 
            if not post.get("is_moderator", False)
        ]
        
        # Добавляем новые moderator_messages
        for msg in new_messages:
            text = str(msg).strip() if msg is not None else ""
            if text:  # Только непустые сообщения
                session.conversation_history.append({
                    "display_name": "Руководство",
                    "content": text,
                    "is_moderator": True
                })
        
        print(f"📝 Обновлены руководства: {len(new_messages)} пунктов")
    
    # Обновляем правила судьи если переданы
    if isinstance(data.get("judge_rules"), list):
        session.judge_rules = [str(instr) for instr in data["judge_rules"]]
        save_theatre_settings()
        print(f"⚖️  Обновлены правила судьи: {len(session.judge_rules)} пунктов")
    
    # Обновляем индивидуальные инструкции участников если переданы
    if isinstance(data.get("participant_instructions"), list):
        for p_instr in data["participant_instructions"]:
            if not isinstance(p_instr, dict):
                continue
            name = str(p_instr.get("name", "") or "")
            instruction = str(p_instr.get("instruction", "") or "")
            if name:
                session.instructions[name] = instruction
                if instruction.strip():
                    print(f"📝 Обновлена индивидуальная инструкция для {name}: {instruction[:50]}...")
                else:
                    print(f"📝 Удалена индивидуальная инструкция для {name}")
    
    return jsonify({"success": True})

@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    if UNLOAD_AFTER_DEBATE:
        unique_models = set(p.get("model", "") for p in session.runtime_participants
                            if p.get("model") and p.get("model") != "human")
        for model in unique_models:
            unload_model(model)
    
    def do_shutdown():
        time.sleep(0.5)
        os._exit(0)
    
    threading.Thread(target=do_shutdown, daemon=True).start()
    return jsonify({"success": True})

# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 50)
    print("🎭 AI Театр - Спектакль нейросетей")
    print("=" * 50)
    print(f"Персонажей:  {len(PARTICIPANTS)}")
    for i, p in enumerate(PARTICIPANTS, 1):
        role = ""
        if p.get("is_moderator"):
            role = " (режиссёр)"
        elif p.get("is_judge"):
            role = " (судья)"
        print(f"  Персонаж {i}: модель {p['model']}{role}")
    
    # Проверяем, что модели реально скачаны: иначе спектакль упадёт уже на сцене
    report_models_status([p["model"] for p in PARTICIPANTS])
    print(f"  ℹ️  Поддержка инструментов определяется автоматически при первом запросе")
    print(f"Размышления: {'ВКЛ' if ENABLE_THINKING else 'ВЫКЛ'}")
    print(f"Поиск в инет: {'ВКЛ' if ENABLE_SEARCH else 'ВЫКЛ'}")
    if ENABLE_SEARCH:
        print(f"Мин. поисков: {MIN_SEARCHES}")
    print(f"Грим и костюмы: {'ВКЛ' if ENABLE_AVATAR_GENERATION else 'ВЫКЛ'}")
    print("=" * 50)
    print()
    print("🌐 Открываю браузер...")
    print("📍 Адрес: http://localhost:5000")
    print()
    print("Нажмите Ctrl+C для остановки сервера")
    print("=" * 50)
    
    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5000')).start()
    # allow_unsafe_werkzeug: без него flask-socketio падает с RuntimeError
    # "The Werkzeug web server is not designed to run in production", если stdin
    # не подключён к терминалу (перенаправленный вывод, запуск из IDE/службы).
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)

if __name__ == "__main__":
    main()
