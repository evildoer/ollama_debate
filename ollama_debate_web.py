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
import time
import threading
import webbrowser
import os
import hashlib
import ssl
import re
import random
from pathlib import Path
from urllib.parse import quote
from flask import Flask, render_template_string, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit

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

# Оптимизация GPU
OPTIONS = {
    "num_ctx": 16384,
    "num_predict": 1024,
    "num_thread": 0,
    "num_gpu": 999,
}

# Кэш для хранения информации о поддержке tools моделями
MODELS_TOOLS_SUPPORT = {}  # {"model_name": True/False}

# Кэш для хранения URL аватаров по ключам (чтобы не искать повторно)
AVATAR_URL_CACHE = {}  # {"avatar_keywords": "image_url"}

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

def get_random_name(gender: str = "male") -> str:
    """Возвращает случайное русское имя по полу"""
    if gender == "female":
        return random.choice(FEMALE_NAMES)
    return random.choice(MALE_NAMES)

def get_random_emoji() -> str:
    """Возвращает случайную эмодзи для аватара"""
    return random.choice(AVATAR_EMOJIS)

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

# ============================================================
# ПОИСК И СКАЧИВАНИЕ ИЗОБРАЖЕНИЙ
# ============================================================

def search_images(query: str, max_results: int = 5) -> str:
    if not SEARCH_AVAILABLE:
        return json.dumps([])
    
    # Пробуем несколько раз с задержкой
    for attempt in range(3):
        try:
            ddgs_kwargs = {}
            if PROXY:
                ddgs_kwargs["proxy"] = PROXY
            
            with DDGS(**ddgs_kwargs) as ddgs:
                results = list(ddgs.images(
                    query,
                    safesearch="off",
                    max_results=max_results
                ))
            
            image_urls = [r.get('image', '') for r in results if r.get('image')]
            if image_urls:
                return json.dumps(image_urls)
        except Exception as e:
            if attempt < 2:
                print(f"  ⚠️  Попытка {attempt + 1} не удалась: {e}, пробую ещё раз...")
                time.sleep(2)
            else:
                print(f"  ⚠️  Ошибка поиска изображений после 3 попыток: {e}")
    
    # Fallback: если прокси включён и все попытки не удались, пробуем без прокси
    if PROXY:
        print(f"  🔄 Пробую поиск изображений без прокси...")
        try:
            with DDGS() as ddgs:
                results = list(ddgs.images(
                    query,
                    safesearch="off",
                    max_results=max_results
                ))
            
            image_urls = [r.get('image', '') for r in results if r.get('image')]
            if image_urls:
                print(f"  ✅ Поиск изображений без прокси успешен!")
                return json.dumps(image_urls)
        except Exception as e:
            print(f"  ⚠️  Поиск изображений без прокси тоже не удался: {e}")
    
    return json.dumps([])

def download_image_with_checksum(url: str, base_name: str) -> str:
    setup_avatar_dir()
    
    # Кодируем URL для поддержки не-ASCII символов (кириллица и т.д.)
    encoded_url = quote(url, safe=':/?&=#%@!~')
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
            }
            
            req = urllib.request.Request(encoded_url, headers=headers)
            
            # Отключаем проверку SSL
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            with urllib.request.urlopen(req, timeout=60, context=ssl_context) as response:
                image_data = response.read()
            
            checksum = compute_data_checksum(image_data)
            
            for existing_file in AVATAR_DIR.glob(f"{base_name}*.jpg"):
                existing_checksum = compute_file_checksum(existing_file)
                if existing_checksum == checksum:
                    return str(existing_file)
            
            filename = f"{base_name}_{checksum}.jpg"
            filepath = AVATAR_DIR / filename
            filepath.write_bytes(image_data)
            
            return str(filepath)
            
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"  ⚠️  Попытка {attempt + 1} скачать не удалась: {e}, пробую ещё раз...")
                time.sleep(3)
            else:
                print(f"  ⚠️  Не удалось скачать изображение после 3 попыток: {e}")
                return ""
    
    return ""

def generate_avatar_for_participant(participant: dict) -> str:
    if not ENABLE_AVATAR_GENERATION or not SEARCH_AVAILABLE:
        return ""
    
    display_name = participant["display_name"]
    avatar_keywords = participant.get("avatar_keywords", display_name)
    
    # Проверяем кэш URL аватаров - если уже искали этот запрос, используем сохранённый URL
    if avatar_keywords in AVATAR_URL_CACHE:
        cached_url = AVATAR_URL_CACHE[avatar_keywords]
        print(f"🎨 Подготовка грима для: '{avatar_keywords}' (из кэша)")
        
        # Создаём безопасное имя файла, сохраняя кириллицу
        base_name = re.sub(r'[^a-zа-яё0-9_]', '_', avatar_keywords.lower().replace(' ', '_'), flags=re.IGNORECASE)
        
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
        base_name = re.sub(r'[^a-zа-яё0-9_]', '_', avatar_keywords.lower().replace(' ', '_'), flags=re.IGNORECASE)
        
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
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)

# Единый источник правды о ходе спектакля - глобальный экземпляр DebateSession,
# объявленный ниже после определения класса.
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_URL = f"{OLLAMA_BASE_URL}/api/chat"
OLLAMA_GENERATE_URL = f"{OLLAMA_BASE_URL}/api/generate"
OLLAMA_SHOW_URL = f"{OLLAMA_BASE_URL}/api/show"

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
                return msg.get("content", ""), msg.get("tool_calls", [])
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
    
    try:
        ddgs_kwargs = {}
        if PROXY:
            ddgs_kwargs["proxy"] = PROXY
        
        with DDGS(**ddgs_kwargs) as ddgs:
            results = list(ddgs.text(query, safesearch="off", max_results=max_results))
        
        if not results:
            return f"По запросу '{query}' ничего не найдено."
        
        output = f"Результаты поиска:\n\n"
        for i, result in enumerate(results, 1):
            title = result.get('title', 'Без названия')
            body = result.get('body', 'Без описания')
            href = result.get('href', '')
            output += f"{i}. {title}\n   {body}\n   {href}\n\n"
        
        return output.strip()
    except Exception as e:
        print(f"  ⚠️  Ошибка поиска: {e}")
        
        # Fallback: если прокси включён, пробуем без прокси
        if PROXY:
            print(f"  🔄 Пробую поиск без прокси...")
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, safesearch="off", max_results=max_results))
                
                if not results:
                    return f"По запросу '{query}' ничего не найдено."
                
                output = f"Результаты поиска:\n\n"
                for i, result in enumerate(results, 1):
                    title = result.get('title', 'Без названия')
                    body = result.get('body', 'Без описания')
                    href = result.get('href', '')
                    output += f"{i}. {title}\n   {body}\n   {href}\n\n"
                
                print(f"  ✅ Поиск без прокси успешен!")
                return output.strip()
            except Exception as e2:
                print(f"  ⚠️  Поиск без прокси тоже не удался: {e2}")
        
        return f"Ошибка поиска: {e}"

def ask_model(model: str, messages: list, participant_name: str) -> tuple:
    search_queries = []
    search_count = 0
    max_searches = 3
    max_iterations = 8
    force_tool_use = False  # Флаг для принудительного использования инструмента через tool_choice
    forced_attempts = 0     # Счётчик попыток принудительного поиска
    max_forced_attempts = 2 # Максимум попыток принудительного поиска
    content = ""
    
    for iteration in range(max_iterations):
        # Если нужен принудительный поиск - передаём tool_choice="any"
        current_tool_choice = "any" if force_tool_use else None
        
        content, tool_calls = ask_model_with_tools(model, messages, tool_choice=current_tool_choice)
        
        # Сбрасываем флаг после использования
        if force_tool_use:
            force_tool_use = False
            forced_attempts += 1
        
        if not tool_calls and content and content.strip():
            # Принудительный поиск только если модель поддерживает tools
            if search_count < MIN_SEARCHES and forced_attempts < max_forced_attempts and MODELS_TOOLS_SUPPORT.get(model, False):
                print(f"  🔍 Принудительный поиск (попытка {forced_attempts + 1}/{max_forced_attempts})...")
                messages.append({"role": "assistant", "content": content, "name": participant_name.lower().replace(" ", "_")})
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
            "name": participant_name.lower().replace(" ", "_")
        })
        
        has_search = False
        for tc in tool_calls:
            func = tc.get("function", {})
            func_name = func.get("name", "")
            func_args = func.get("arguments", {})
            
            if func_name == "search_web" and search_count < max_searches:
                query = func_args.get("query", "")
                max_results = func_args.get("max_results", 5)
                
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
    
    if not content or not content.strip():
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
                search_count: int = 0, search_queries: list = None) -> dict:
    """Единая функция создания поста для любого участника (human или AI)"""
    if search_queries is None:
        search_queries = []
    
    return {
        "id": len(session.posts) + 1,
        "display_name": display_name,
        "model_used": model_used,
        "avatar_url": avatar_url,
        "avatar_emoji": avatar_emoji or "📣",
        "content": content,
        "content_html": markdown_to_html(content),
        "round": round_num,
        "timestamp": time.strftime("%H:%M"),
        "search_count": search_count,
        "search_queries": search_queries
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
        self.waiting_for_human = False
        self.moderator_message = None
        self.moderator_finished = False
        self.runtime_participants = []
        self.conversation_history = []
    
    def reset(self, topic: str, runtime_participants: list, avatars: dict, 
              instructions: dict, avatar_emojis: dict = None, static_instructions: list = None):
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
        self.waiting_for_human = False
        self.moderator_message = None
        self.moderator_finished = False
        self.runtime_participants = runtime_participants
        self.conversation_history = []
    
    def clear(self):
        """Полный сброс состояния (кнопка «Новый спектакль»)"""
        self.__init__()
    
    def add_post(self, display_name: str, model_used: str, content: str, round_num: int,
                 search_count: int = 0, search_queries: list = None, is_moderator: bool = False):
        """Добавляет пост в историю и список постов"""
        avatar_url = self.avatars.get(display_name)
        avatar_emoji = self.avatar_emojis.get(display_name, "📣")
        post = create_post(display_name, model_used, content, round_num, 
                          avatar_url, avatar_emoji, search_count, search_queries)
        self.posts.append(post)
        
        if content.strip():
            self.conversation_history.append({
                "display_name": display_name,
                "content": content,
                "is_moderator": is_moderator
            })
        
        return post
    
    def get_system_prompt(self, participant: dict, all_names: list) -> str:
        """Генерирует системный промпт для участника"""
        other_names = [name for name in all_names if name != participant["display_name"]]
        
        # Динамические части (зависят от переменных)
        dynamic_parts = [
            f'Ты — {participant["display_name"]}.',
            f"Ты участвуешь в диалоге вместе с: {', '.join(other_names)}.",
            f'Тема обсуждения: "{self.topic}".',
        ]
        
        # Настраиваемые статичные инструкции (из self.static_instructions)
        static_parts = self.static_instructions if self.static_instructions else [
            'ИГРАЙ ЭТУ РОЛЬ ОТ ПЕРВОГО ЛИЦА (Я, МНЕ, МОЁ).',
            'ОБРАЩАЙСЯ к ним по именам когда отвечаешь на их реплики.',
            'ГОВОРИ О СЕБЕ В ПЕРВОМ ЛИЦЕ, не в третьем!',
            'Учитывай всё что говорили другие персонажи и реагируй на их слова.',
            'Отвечай на русском языке.',
            'КРИТИЧЕСКИ ВАЖНО: Пиши МАКСИМУМ 4-5 предложений. Будь лаконичным.',
            'Используй поиск в интернете для фактологических утверждений.',
            'При поиске НЕ указывай годы.'
        ]
        
        # Объединяем все части
        all_parts = dynamic_parts + static_parts
        system_prompt = ' '.join(all_parts)
        
        # Добавляем индивидуальную инструкцию участника
        custom_instruction = self.instructions.get(participant["display_name"], "")
        if custom_instruction:
            system_prompt += f" {custom_instruction}"
        
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
        
        return system_prompt
    
    def build_messages_for_ai(self, participant: dict, round_num: int) -> list:
        """Строит список сообщений для AI модели"""
        # Исключаем модераторов из списка участников для промпта
        non_moderator_names = [
            p["display_name"] for p in self.runtime_participants 
            if not p.get("is_moderator", False)
        ]
        
        system_prompt = self.get_system_prompt(participant, non_moderator_names)
        
        messages = [
            {"role": "system", "content": system_prompt, "name": "system"},
        ]
        
        for post in self.conversation_history:
            speaker_name = post["display_name"]
            content = post["content"]
            is_moderator = post.get("is_moderator", False)
            
            if speaker_name == participant["display_name"]:
                messages.append({
                    "role": "assistant",
                    "content": content,
                    "name": speaker_name.lower().replace(" ", "_")
                })
            elif is_moderator:
                # Реплики модератора добавляем как системные сообщения (без упоминания "режиссёр")
                messages.append({
                    "role": "system",
                    "content": f"{speaker_name} говорит: {content}",
                    "name": speaker_name.lower().replace(" ", "_")
                })
            else:
                messages.append({
                    "role": "user",
                    "content": f"{speaker_name} говорит: {content}",
                    "name": speaker_name.lower().replace(" ", "_")
                })
        
        if round_num == 1 and len(self.conversation_history) == 0:
            messages.append({
                "role": "user", 
                "content": f'Как {participant["display_name"]}, начни диалог на тему "{self.topic}". Обращайся к другим участникам по именам.',
                "name": participant["display_name"].lower().replace(" ", "_")
            })
        else:
            last_post = self.conversation_history[-1] if self.conversation_history else None
            if last_post:
                last_speaker = last_post["display_name"]
                last_is_moderator = last_post.get("is_moderator", False)
                if last_is_moderator:
                    messages.append({
                        "role": "system",
                        "content": f'{last_speaker} только что сказал: "{last_post["content"]}". Как {participant["display_name"]}, ответь ему и другим участникам, обращаясь по именам.',
                        "name": participant["display_name"].lower().replace(" ", "_")
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": f'{last_speaker} только что сказал: "{last_post["content"]}". Как {participant["display_name"]}, ответь ему и другим участникам, обращаясь по именам.',
                        "name": participant["display_name"].lower().replace(" ", "_")
                    })
            else:
                messages.append({
                    "role": "user",
                    "content": f'Как {participant["display_name"]}, продолжай диалог, обращаясь к другим участникам по именам.',
                    "name": participant["display_name"].lower().replace(" ", "_")
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
            search_queries=search_queries
        )
        
        self.current_action = None
        time.sleep(0.5)
        
        return response, search_count, search_queries

# Глобальный экземпляр сессии
session = DebateSession()

def run_debate_thread(topic: str):
    runtime_participants = session.runtime_participants
    
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
            
            print(f"  🎭 Персонажи: {[p['display_name'] + ' (' + p['model'] + ')' for p in runtime_participants]}")
            
            for participant in runtime_participants:
                print(f"  🎭 На сцене: {participant['display_name']} (модель: {participant['model']})")
                session.current_participant = participant["display_name"]
                
                if participant["model"] == "human":
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
                                    display_name=participant["display_name"],
                                    model_used="human",
                                    content=current_message,
                                    round_num=round_num,
                                    search_count=0,
                                    search_queries=[],
                                    is_moderator=participant.get("is_moderator", False)
                                )
                                print(f"🎬 {participant['display_name']}: {current_message[:50]}")
                                socketio.emit('new_post', post)
                            else:
                                print(f"🎬 {participant['display_name']} пропустил действие")
                            
                            session.moderator_message = None
                            session.current_action = None
                            break
                    
                    if session.moderator_finished:
                        break
                else:
                    session.current_action = "thinking"
                    
                    session.handle_ai_turn(participant, round_num)
                    
                    post = session.posts[-1] if session.posts else None
                    if post:
                        socketio.emit('new_post', post)
                    
                    session.current_action = None
                    time.sleep(0.5)
            
            if session.moderator_finished:
                break
        
        session.finished = True
        
    finally:
        session.running = False
        session.current_participant = None
        session.current_action = None
        
        if UNLOAD_AFTER_DEBATE:
            unique_models = set(p["model"] for p in runtime_participants if p["model"] != "human")
            for model in unique_models:
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
        .header-topic { font-size: 24px; font-weight: bold; color: #000000; margin-top: 20px; padding: 20px; border: 2px solid #000000; text-align: center; line-height: 1.4; min-height: 60px; display: flex; align-items: center; justify-content: center; }
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
        .post-avatar img { width: 120px; height: 120px; object-fit: cover; border: 1px solid #000000; filter: grayscale(100%); }
        .post-avatar .emoji { width: 120px; height: 120px; background: #ffffff; border: 1px solid #000000; display: flex; align-items: center; justify-content: center; font-size: 60px; }
        .post-content { flex: 1; min-width: 0; }
        .post-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #000000; }
        .post-author { font-family: Georgia, serif; font-size: 36px; font-weight: normal; color: #000000; letter-spacing: 1px; }
        .post-model { font-size: 14px; color: #000000; font-family: Georgia, serif; margin-top: 5px; font-style: italic; }
        .post-time { font-size: 14px; color: #000000; font-style: italic; }
        .post-text { font-size: 20px; line-height: 1.8; color: #000000; word-wrap: break-word; overflow-wrap: break-word; text-align: justify; }
        .post-text p { margin-bottom: 15px; }
        .post-text p:last-child { margin-bottom: 0; }
        .post-text strong { font-weight: bold; }
        .post-text em { font-style: italic; }
        .post-text code { background: #ffffff; padding: 2px 6px; font-family: 'Courier New', monospace; border: 1px solid #000000; }
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
                    <button class="btn btn-primary" id="startBtn" onclick="startDebate()">🎭 Начать спектакль</button>
                    <button class="btn btn-secondary" id="newBtn" onclick="resetDebate()" style="display:none;">🎭 Новый спектакль</button>
                    <div style="margin-top:10px;font-size:12px;color:#666;">Ctrl+Enter для отправки</div>
                </div>
                <div class="card" id="staticInstructionsCard">
                    <h2>Общие инструкции</h2>
                    <p style="font-size:14px;color:#666;margin-bottom:15px;font-style:italic;">Эти инструкции будут добавлены в системный промпт для всех участников. Вы можете редактировать, добавлять или удалять пункты.</p>
                    <div id="staticInstructionsList"></div>
                    <button class="btn btn-secondary" onclick="addStaticInstruction()" style="margin-top:10px;">➕ Добавить инструкцию</button>
                </div>
                <div id="posts"></div>
                <div id="moderatorPanel" style="display:none; margin-top:30px; padding:20px; border:2px solid #000000;">
                    <h3 style="margin:0 0 15px 0; font-size:20px; text-transform:uppercase; letter-spacing:2px;">Ваша реплика, режиссёр</h3>
                    <textarea id="moderatorInput" rows="4" style="width:100%; padding:12px; border:2px solid #000000; font-size:16px; font-family:Georgia,serif; margin-bottom:15px;" placeholder="Напишите реплику или оставьте пустым чтобы пропустить действие..."></textarea>
                    <div style="display:flex; gap:15px;">
                        <button class="btn btn-primary" onclick="sendModeratorMessage()">Отправить</button>
                        <button class="btn btn-secondary" id="finishBtn" onclick="finishDebate()">Завершить спектакль</button>
                    </div>
                    <div style="margin-top:10px; font-size:12px; font-style:italic;">💡 Пустое сообщение = пропуск действия • Ctrl+Enter для отправки</div>
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
                <div class="sidebar-title">Персонажи</div>
                <div id="participantsDisplay" style="color:#000000;font-size:13px;line-height:1.6;"></div>
            </div>
        </div>
    </div>
    <div id="avatarModal" class="modal" onclick="closeAvatarModal()">
        <span class="modal-close">&times;</span>
        <img class="modal-content" id="avatarModalImg">
    </div>
    <script>
        let participants = [];
        let debateRunning = false;
        let pollInterval = null;
        let lastPostCount = 0;
        let staticInstructions = [];
        
        // Загружаем участников и статичные инструкции
        fetch('/api/participants')
            .then(r => r.json())
            .then(data => { participants = data.participants; renderParticipantsSetup(); });
        
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
        
        function renderParticipantsSetup() {
            const container = document.getElementById('participantsSetup');
            container.innerHTML = participants.map((p, idx) => `
                <div class="participant-card">
                    <div class="avatar-container">
                        <div class="avatar-preview" id="avatar-preview-${idx}" onclick="openAvatarModal(${idx})">
                            ${p.avatar_url ? `<img src="${p.avatar_url}">` : (p.avatar_emoji || (p.is_moderator ? '🎬' : '📣'))}
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
                    <div style="font-size:12px;color:#999;margin-top:10px;">Модель: ${p.model} ${p.is_moderator ? '(режиссёр)' : ''}</div>
                </div>
            `).join('');
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
                if (data.avatar_url) { 
                    participants[idx].avatar_url = data.avatar_url; 
                    preview.innerHTML = `<img src="${data.avatar_url}">`; 
                } else { 
                    preview.innerHTML = '❌'; 
                    setTimeout(() => preview.innerHTML = fallbackEmoji, 2000); 
                }
                btn.disabled = false;
                btn.textContent = '🔍 Найти аватар';
            }).catch(err => { 
                console.error('Ошибка поиска аватара:', err); 
                preview.innerHTML = '❌'; 
                setTimeout(() => preview.innerHTML = fallbackEmoji, 2000);
                btn.disabled = false;
                btn.textContent = '🔍 Найти аватар';
            });
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
            document.getElementById('participantsDisplay').innerHTML = participants.map(p => { const i = instructions[p.display_name]; let h = `<div style="margin-bottom:12px;"><strong>${p.display_name}</strong> <small>(${p.model})</small>`; if (i) h += `<br><em style="margin-left:10px;">${i}</em>`; return h + '</div>'; }).join('');
            const avatars = {}; participants.forEach(p => { if (p.avatar_url) avatars[p.display_name] = p.avatar_url; });
            // Собираем статичные инструкции из формы
            const currentStaticInstructions = [];
            staticInstructions.forEach((_, idx) => {
                const el = document.getElementById(`static-instr-${idx}`);
                if (el && el.value.trim()) currentStaticInstructions.push(el.value.trim());
            });
            
            fetch('/api/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ topic, instructions, participants, avatars, static_instructions: currentStaticInstructions }) })
            .then(r => r.json()).then(data => { if (data.success) pollInterval = setInterval(updatePosts, 3000); })
            .catch(err => { console.error('Ошибка запуска:', err); alert('Ошибка: ' + err.message); });
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
            const postDiv = document.createElement('div');
            postDiv.className = 'post';
            postDiv.innerHTML = `<div class="post-avatar">${avatarHtml}</div><div class="post-content"><div class="post-header"><div><div class="post-author">${post.display_name}</div><div class="post-model">модель: ${post.model_used}</div></div><div class="post-time">${post.timestamp} | Акт ${post.round}</div></div><div class="post-text">${post.content_html || post.content}</div>${searchInfo}</div>`;
            postsDiv.appendChild(postDiv);
        }
        
        function showAvatarFull(url) { document.getElementById('avatarModalImg').src = url; document.getElementById('avatarModal').style.display = 'block'; }
        
        function updatePosts() {
            fetch(`/api/status?lastPostCount=${lastPostCount}`).then(r => r.json()).then(data => {
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
                }
                if (data.new_posts && data.new_posts.length > 0) data.new_posts.forEach(post => addPost(post));
                if (typeof data.total_posts === 'number') lastPostCount = data.total_posts;
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
            });
        }
        
        function sendModeratorMessage() {
            const input = document.getElementById('moderatorInput');
            fetch('/api/moderator/message', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: input.value}) })
            .then(r => r.json()).then(data => { if (data.success) { input.value = ''; document.getElementById('moderatorPanel').style.display = 'none'; } else alert('Ошибка: ' + (data.error || 'неизвестная')); })
            .catch(err => { console.error('Ошибка:', err); alert('Ошибка: ' + err.message); });
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

@app.route('/avatars/<path:filename>')
def serve_avatar(filename):
    return send_from_directory(AVATAR_DIR, filename)

# Дефолтные статичные инструкции
DEFAULT_STATIC_INSTRUCTIONS = [
    'ИГРАЙ ЭТУ РОЛЬ ОТ ПЕРВОГО ЛИЦА (Я, МНЕ, МОЁ).',
    'ОБРАЩАЙСЯ к ним по именам когда отвечаешь на их реплики.',
    'ГОВОРИ О СЕБЕ В ПЕРВОМ ЛИЦЕ, не в третьем!',
    'Учитывай всё что говорили другие участники и реагируй на их слова.',
    'Отвечай на русском языке.',
    'КРИТИЧЕСКИ ВАЖНО: Пиши МАКСИМУМ 4-5 предложений. Будь лаконичным.',
    'Используй поиск в интернете для фактологических утверждений.',
    'При поиске НЕ указывай годы.'
]

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
        
        # Выбираем уникальное имя
        if gender == "male" and available_male_names:
            name = random.choice(available_male_names)
            available_male_names.remove(name)
        elif available_female_names:
            name = random.choice(available_female_names)
            available_female_names.remove(name)
        elif available_male_names:
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
            "is_moderator": p.get("is_moderator", False)
        })
    
    return jsonify({"participants": participants_data})

@app.route('/api/avatar/<keywords>')
def get_avatar(keywords):
    base_name = keywords.lower().replace(' ', '_')
    for avatar_file in AVATAR_DIR.glob(f"{base_name}*.jpg"):
        return jsonify({"avatar_url": f"/avatars/{avatar_file.name}"})
    return jsonify({"avatar_url": None})

@app.route('/api/avatar/<keywords>', methods=['POST'])
def refresh_avatar(keywords):
    try:
        data = request.get_json(force=True, silent=True) or {}
        participant_idx = data.get('participant_idx')
        
        if participant_idx is None:
            return jsonify({"error": "participant_idx не указан"}), 400
        
        if not PARTICIPANTS:
            return jsonify({"error": "Нет моделей"}), 400
        
        template = PARTICIPANTS[0]
        participant = {
            "model": template["model"],
            "display_name": keywords,
            "avatar_keywords": keywords
        }
        
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
    
    data = request.json
    topic = data.get("topic", "")
    instructions = data.get("instructions", {})
    participants_data = data.get("participants", [])
    avatars = data.get("avatars", {})
    static_instructions = data.get("static_instructions", [])
    
    # Собираем эмодзи из участников
    avatar_emojis = {}
    for p in participants_data:
        name = p.get("display_name", "")
        if name and p.get("avatar_emoji"):
            avatar_emojis[name] = p["avatar_emoji"]
    
    if not topic:
        return jsonify({"success": False, "error": "Тема не указана"})
    
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
    current_participant_is_moderator = False
    if session.waiting_for_human and session.current_participant:
        for p in session.runtime_participants:
            if p["display_name"] == session.current_participant and p.get("is_moderator", False):
                current_participant_is_moderator = True
                break
    
    last_post_count = max(0, request.args.get("lastPostCount", 0, type=int))
    
    return jsonify({
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
        "current_participant_is_moderator": current_participant_is_moderator,
    })

# ============================================================
# WEBSOCKET СОБЫТИЯ
# ============================================================

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
        "current_participant_is_moderator": False,
    })

@socketio.on('disconnect')
def handle_disconnect():
    print("🔌 Клиент отключился от WebSocket")

@socketio.on('request_status')
def handle_request_status():
    current_participant_is_moderator = False
    if session.waiting_for_human and session.current_participant:
        for p in session.runtime_participants:
            if p["display_name"] == session.current_participant and p.get("is_moderator", False):
                current_participant_is_moderator = True
                break
    
    emit('status_update', {
        "running": session.running,
        "finished": session.finished,
        "current_round": session.current_round,
        "current_participant": session.current_participant,
        "current_action": session.current_action,
        "search_query": session.search_query,
        "waiting_for_human": session.waiting_for_human,
        "current_participant_is_moderator": current_participant_is_moderator,
    })

@app.route('/api/moderator/message', methods=['POST'])
def moderator_message():
    data = request.json
    message = data.get("message", "")
    session.moderator_message = message
    return jsonify({"success": True})

@app.route('/api/moderator/finish', methods=['POST'])
def moderator_finish():
    session.moderator_finished = True
    session.waiting_for_human = False
    return jsonify({"success": True})

@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    if UNLOAD_AFTER_DEBATE:
        unique_models = set(p["model"] for p in session.runtime_participants if p["model"] != "human")
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

if __name__ == "__main__":
    print("=" * 50)
    print("🎭 AI Театр - Спектакль нейросетей")
    print("=" * 50)
    print(f"Персонажей:  {len(PARTICIPANTS)}")
    for i, p in enumerate(PARTICIPANTS, 1):
        is_mod = " (режиссёр)" if p.get("is_moderator") else ""
        print(f"  Персонаж {i}: модель {p['model']}{is_mod}")
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
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
