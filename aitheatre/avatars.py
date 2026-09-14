"""Грим и костюмы: поиск и скачивание картинок для аватаров.

Кириллица в именах файлов сохраняется — по ней потом ищется уже скачанный
аватар. Сам поиск (вместе с прокси) живёт в search.py: искать в интернете
приходится не только картинки.
"""

import hashlib
import json
import re
import ssl
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

from . import deps
from . import search
from . import settings

# Кэш для хранения URL аватаров по ключам (чтобы не искать повторно)
AVATAR_URL_CACHE = {}  # {"avatar_keywords": "image_url"}

def setup_avatar_dir():
    if not settings.AVATAR_DIR.exists():
        settings.AVATAR_DIR.mkdir(parents=True, exist_ok=True)

def compute_file_checksum(filepath: Path) -> str:
    return hashlib.md5(filepath.read_bytes()).hexdigest()[:8]

def compute_data_checksum(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()[:8]

def sanitize_avatar_name(keywords: str) -> str:
    """Имя файла аватара: кириллица сохраняется, остальные символы заменяются на '_'."""
    return re.sub(r'[^a-zа-яё0-9_]', '_', keywords.lower().replace(' ', '_'), flags=re.IGNORECASE)

# ============================================================
# ПОИСК И СКАЧИВАНИЕ ИЗОБРАЖЕНИЙ
# ============================================================

def search_images(query: str, max_results: int = 5) -> str:
    if not deps.SEARCH_AVAILABLE:
        return json.dumps([])
    
    results, _ = search.ddgs_search("Поиск изображений", "images", query, max_results)
    image_urls = [r.get('image', '') for r in results if r.get('image')]
    return json.dumps(image_urls)

def _save_avatar_image(image_data: bytes, base_name: str) -> str:
    """Сохраняет картинку в /avatars, переиспользуя файл с той же контрольной суммой."""
    checksum = compute_data_checksum(image_data)
    
    for existing_file in settings.AVATAR_DIR.glob(f"{base_name}*.jpg"):
        if compute_file_checksum(existing_file) == checksum:
            return str(existing_file)
    
    filepath = settings.AVATAR_DIR / f"{base_name}_{checksum}.jpg"
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
    for proxy in search.proxy_modes():
        mode = f"через прокси {proxy}" if proxy else "без прокси"
        if proxy is None and settings.PROXY:
            print(f"  🔄 Прокси не помог, пробую скачать без прокси...")
        
        opener = _build_image_opener(proxy)
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(encoded_url, headers=headers)
                with opener.open(req, timeout=10) as response:
                    image_data = response.read()
                
                filepath = _save_avatar_image(image_data, base_name)
                if proxy is None and settings.PROXY:
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
    if not settings.ENABLE_AVATAR_GENERATION or not deps.SEARCH_AVAILABLE:
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
