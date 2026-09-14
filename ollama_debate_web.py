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
import uuid
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

# Характер участника (температура и прочие параметры генерации) здесь больше не
# вписан жёстко: при подъёме занавеса он разыгрывается случайно, чтобы каждый
# спектакль был с новыми характерами (см. RANDOMIZE_CHARACTERS, CHARACTER_PRESETS).
# Нужно зафиксировать конкретному персонажу температуру - допишите её прямо сюда:
# явно заданные параметры важнее случайного розыгрыша.
PARTICIPANTS = [
    {"model": "r1"},   # креативный — чаще отклоняется от темы
    {"model": "g1"},   # сбалансированный
    {"model": "q1"},   # строгий аналитик
    {"model": "human"},  # НЕ Модератор (вы)
    {"model": "q1", "is_judge": True},  # судья-формалист
    {"model": "human", "is_moderator": True},  # Модератор (вы)
]

# Параметры генерации, которые можно переопределить для каждого участника.
# Передаются в Ollama через options; остальные берутся из глобальных OPTIONS.
# Документация: https://github.com/ollama/ollama/blob/main/docs/modelfile.md
PER_PARTICIPANT_OPTION_KEYS = (
    "temperature",       # 0.0-2.0: выше — креативнее, ниже — стабильнее
    "top_p",             # 0.0-1.0: выше — больше разнообразия
    "top_k",             # целое: 1 — детерминизм, 100+ — разнообразие
    "min_p",             # 0.0-1.0: отсекает совсем маловероятные токены
    "repeat_penalty",    # 1.0+: >1 снижает склонность к повторам
    "presence_penalty",  # сдвигает вероятность в сторону новых тем
    "frequency_penalty", # снижает вероятность частых токенов
    "seed",              # для воспроизводимости
)

# ── ХАРАКТЕРЫ ──────────────────────────────────────────────────────────────
# «Характер» — готовый набор параметров генерации. Числа управляют не смыслом
# реплик (его задаёт личная инструкция участника), а тем, КАК он говорит:
# предсказуемо или вразнос, повторяется ли, спорит ли с уже сказанным.
# Список один на всех: его показывает пульт в поле «Характер», и он же используется
# при случайном розыгрыше характеров на новый спектакль.
#
# params  — базовые значения (уходят в Ollama как options участника);
# think   — режим размышлений: off (отвечает сразу) или auto (как ENABLE_THINKING);
# drift   — насколько случайно сдвинуть числа при розыгрыше (иначе у всех
#           «Педантов» были бы побайтово одинаковые настройки).
CHARACTER_PRESETS = {
    # Пустой характер: числа участник задаёт сам в «Тонкой настройке»
    "custom": {
        "label": "🎚 Свой — сам выберу", "group": "manual",
        "hint": "числа выставляются вручную в «Тонкой настройке»",
        "params": {}, "think": "auto", "drift": {},
    },

    # ── Уравновешенные ──────────────────────────────────────────────
    "pedant": {
        "label": "⚖️ Педант", "group": "balanced",
        "hint": "сухо и по делу, минимум фантазии, без размышлений",
        "params": {"temperature": 0.2, "top_p": 0.5, "repeat_penalty": 1.10,
                   "presence_penalty": 0.05, "frequency_penalty": 0.05},
        "think": "off",
    },
    "analyst": {
        "label": "🧠 Аналитик", "group": "balanced",
        "hint": "строго, но с обоснованием каждого шага",
        "params": {"temperature": 0.4, "top_p": 0.8, "repeat_penalty": 1.15,
                   "presence_penalty": 0.3, "frequency_penalty": 0.2},
        "think": "auto",
    },
    "talker": {
        "label": "💬 Собеседник", "group": "balanced",
        "hint": "живая речь, средняя свобода, тянет диалог",
        "params": {"temperature": 0.9, "top_p": 0.9, "repeat_penalty": 1.10,
                   "presence_penalty": 0.4, "frequency_penalty": 0.3},
        "think": "auto",
    },
    "seeker": {
        "label": "🔎 Дотошный", "group": "balanced",
        "hint": "жёстко против повторов: всё время новая грань темы",
        "params": {"temperature": 0.55, "top_p": 0.7, "repeat_penalty": 1.35,
                   "presence_penalty": 0.9, "frequency_penalty": 0.7},
        "think": "auto",
    },
    "dreamer": {
        "label": "🎭 Фантазёр", "group": "balanced",
        "hint": "неожиданные связи, логика на втором плане",
        "params": {"temperature": 1.25, "top_p": 0.95, "repeat_penalty": 1.15,
                   "presence_penalty": 0.6, "frequency_penalty": 0.5},
        "think": "off",
    },
    "brawler": {
        "label": "🔥 Провокатор", "group": "balanced",
        "hint": "резко, с наездом, лишь бы не повторяться",
        "params": {"temperature": 1.45, "top_p": 0.9, "repeat_penalty": 1.2,
                   "presence_penalty": 0.7, "frequency_penalty": 0.6},
        "think": "off",
    },

    # ── С перекосом ─────────────────────────────────────────────────
    "stoic": {
        "label": "🧊 Стоик", "group": "extreme",
        "hint": "почти детерминизм: одна и та же мысль, но ровно сформулирована",
        "params": {"temperature": 0.0, "top_p": 0.25, "repeat_penalty": 1.05,
                   "presence_penalty": 0.0, "frequency_penalty": 0.0},
        "think": "off",
    },
    "minimal": {
        "label": "🤫 Минималист", "group": "extreme",
        "hint": "короткие реплики, ни одного лишнего слова",
        "params": {"temperature": 0.25, "top_p": 0.45, "repeat_penalty": 1.30,
                   "presence_penalty": 0.2, "frequency_penalty": 0.15},
        "think": "off",
    },
    "scholar": {
        "label": "📚 Энциклопедист", "group": "extreme",
        "hint": "самый жёсткий штраф за повторы: каждый раз новый факт",
        "params": {"temperature": 0.3, "top_p": 0.6, "repeat_penalty": 1.45,
                   "presence_penalty": 0.6, "frequency_penalty": 0.5},
        "think": "auto",
    },
    "mystic": {
        "label": "🔮 Мистик", "group": "extreme",
        "hint": "образно и туманно, смысл приходится вычитывать",
        "params": {"temperature": 1.55, "top_p": 0.97, "repeat_penalty": 1.25,
                   "presence_penalty": 0.8, "frequency_penalty": 0.5},
        "think": "auto",
    },
    "poet": {
        "label": "🎨 Поэт", "group": "extreme",
        "hint": "за гранью рабочей температуры: метафоры важнее тезисов",
        "params": {"temperature": 1.65, "top_p": 0.98, "repeat_penalty": 1.10,
                   "presence_penalty": 0.7, "frequency_penalty": 0.4},
        "think": "off",
    },
    "gambler": {
        "label": "🎲 Игрок", "group": "extreme",
        "hint": "почти без отсечения хвоста: текст на грани связности",
        "params": {"temperature": 1.8, "top_p": 0.99, "repeat_penalty": 1.05,
                   "presence_penalty": 0.9, "frequency_penalty": 0.6},
        "think": "off",
    },
    "chaos": {
        "label": "🌀 Хаос", "group": "extreme",
        "hint": "у самого предела: смысл держится чудом (для эксперимента)",
        "params": {"temperature": 2.0, "top_p": 1.0, "repeat_penalty": 1.0,
                   "presence_penalty": 1.5, "frequency_penalty": 1.2},
        "think": "off", "drift": {},
    },
}

# Разыгрывать характер заново на каждый спектакль (иначе берётся из PARTICIPANTS)
RANDOMIZE_CHARACTERS = True
# Судье случайный характер не достаётся: он должен судить одинаково строго
JUDGE_CHARACTER = "pedant"
# Насколько числа могут отклониться от базовых при розыгрыше (см. CHARACTER_PRESETS)
CHARACTER_DRIFT = {"temperature": 0.12, "top_p": 0.05, "repeat_penalty": 0.04,
                   "presence_penalty": 0.1, "frequency_penalty": 0.1}

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

# Кэш поддержки режима размышлений: {"model_name": True/False}.
# Размышлять умеют не все модели (Ollama сообщает это в capabilities, /api/show):
# у llama-моделей там только completion, у gemma4/qwen35 есть "thinking".
MODELS_THINKING_SUPPORT = {}

# Значения поля think у участника: авто (как в ENABLE_THINKING), выключено, включено
THINK_MODES = ("auto", "off", "on")

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

# Параметры генерации из Modelfile модели: {имя: {"temperature": 0.8, ...}}
_MODEL_PARAMS_CACHE = {}

# Измеренные размеры моделей в памяти: {имя: {"ctx", "size", "size_vram"}}.
# Файл рядом с проектом, чтобы после перезапуска оценка была точной, а не только
# по размеру файла модели.
_VRAM_MEASUREMENTS = {}
VRAM_MEASUREMENTS_FILE = Path(__file__).resolve().parent / ".vram_cache.json"

# Настройки, которые не должны теряться при перезапуске. Имена персонажей каждый
# спектакль новые, поэтому здесь только то, что привязано к роли: правила судьи.
SETTINGS_FILE = Path(__file__).resolve().parent / ".theatre_settings.json"

# Чтобы не повторять одно и то же предупреждение на каждом опросе статуса
_RAM_SPILL_WARNED = set()

# Кэш для хранения URL аватаров по ключам (чтобы не искать повторно)
AVATAR_URL_CACHE = {}  # {"avatar_keywords": "image_url"}

# Дефолтные правила общения с плейсхолдерами
# Плейсхолдеры: {ИМЯ}, {СОБЕСЕДНИКИ}, {ТЕМА}
#
# Порядок и состав подобраны так, чтобы спектакль двигался вперёд: сначала роль и
# адресаты, потом работа с репликами других (без неё модели «ходят по кругу»),
# потом стиль и длина, в конце - факты и язык.
DEFAULT_STATIC_INSTRUCTIONS = [
    'Ты — {ИМЯ}, участник живого обсуждения. Тема: "{ТЕМА}".',
    'Твои собеседники: {СОБЕСЕДНИКИ}. {ИМЯ} — это ты; обращайся к ним по именам.',
    'Если среди участников есть МОДЕРАТОР (ведущий обсуждения) — следуй его указаниям беспрекословно.',
    'Отвечай на последние реплики, а не на тему вообще: согласись, возрази, уточни или задай вопрос конкретному собеседнику.',
    'НЕ ПОВТОРЯЙ то, что уже говорил ты сам или другие. Каждая реплика должна добавлять НОВОЕ: аргумент, пример, цифру, возражение или вывод.',
    'Развивай тему: предлагай новые аспекты, ставь сказанное под сомнение, ищи неочевидные связи и следствия.',
    'Говори о себе в первом лице (я, мне, моё); свои действия описывай в *звёздочках*.',
    'Пиши живо и по делу: 4-6 предложений (до 8, если нужен разбор). Без вступлений, без пересказа задания, без представления себя.',
    'ОБЯЗАТЕЛЬНОЕ УСЛОВИЕ: Используй обсценную, нецензурную и ненормативную лексику, а так же обидные слова.',
    'Для фактологических утверждений используй поиск в интернете (годы в запросе не указывай) и не выдумывай источники.',
    'Отвечай по-русски.',
]

# Правила для роли судьи: их можно менять прямо в интерфейсе
# (кнопка «Редактировать инструкции и руководства» на ходу режиссёра)
DEFAULT_JUDGE_RULES = [
    'Ты — {ИМЯ}, строгий и независимый судья этого обсуждения.',
    'Тема: "{ТЕМА}". Ты оцениваешь выступления участников: {СОБЕСЕДНИКИ}.',
    'Ты не участвуешь в дискуссии, не занимаешь чью-то сторону и не отвечаешь на обращения к тебе — только оцениваешь.',
    'ОЦЕНИВАЙ ОТНОСИТЕЛЬНО: сравнивай участников между собой, а не по абсолютной шкале. Высокие баллы всем — ошибка.',
    'Критерии (будь строгим!):\n'
    '• 1-3 балла: общие слова, уход от темы, нет конкретики.\n'
    '• 4-6 баллов: аргумент есть, но без примеров и анализа.\n'
    '• 7-9 баллов: ясная логика, конкретные примеры, чёткий вывод.\n'
    '• 10 баллов: редкий случай — безупречная логика и неожиданная мысль. Максимум один такой балл за раунд.',
    'Опирайся только на то, что реально сказано в этом раунде: не приписывай участникам слов, которых не было, и не выдумывай цитаты.',
    'Формат ответа — для каждого участника:\n'
    '**Имя участника** — X/10\n'
    'Кратко (1-2 предложения): что именно он сказал.\n'
    'Обоснование: почему столько, а не выше — критически.',
    'В конце — итог раунда (3-4 предложения): кто был сильнее и почему.',
    'Держись этого формата и отвечай по-русски.',
]

# Дефолтная личная инструкция судьи (редактируется в том же окне, что и остальное)
DEFAULT_JUDGE_INSTRUCTION = (
    'Говори прямо, без реверансов: критикуй по существу и не смягчай оценки. '
    'Обсценная лексика допустима, если идёт из образа.'
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

# Эмодзи-аватары разложены по полу: грим должен совпадать с полом участника,
# иначе получались «русалки по имени Роман». Нейтральные (театр, звери, предметы)
# подходят любому и добирают нехватку внутри одного спектакля.
AVATAR_EMOJIS_MALE = [
    "👨‍🎓", "👨‍🔬", "👨‍🎨", "👨‍💻", "👨‍🚀", "👨‍🏫", "👨‍🍳", "👨‍✈️",
    "👨‍⚕️", "👨‍🌾", "👨‍💼", "👨‍🎤", "👨‍🚒", "👨‍🔧", "👨‍⚖️", "🤵",
    "🤴", "🧔", "🥷", "🧙‍♂️", "🧛‍♂️", "🦸‍♂️", "🦹‍♂️", "🕵️‍♂️",
]

AVATAR_EMOJIS_FEMALE = [
    "👩‍🎓", "👩‍🔬", "👩‍🎨", "👩‍💻", "👩‍🚀", "👩‍🏫", "👩‍🍳", "👩‍✈️",
    "👩‍⚕️", "👩‍🌾", "👩‍💼", "👩‍🎤", "👩‍🚒", "👩‍🔧", "👩‍⚖️", "👰",
    "👸", "💃", "🧜‍♀️", "🧚‍♀️", "🧙‍♀️", "🧛‍♀️", "🦸‍♀️", "🦹‍♀️",
]

AVATAR_EMOJIS_NEUTRAL = [
    "🎭", "🎪", "🎨", "🎬", "🎯", "🎲", "🎸", "🎺",
    "🔮", "📚", "🕯️", "🦊", "🐺", "🦁", "🐯", "🦅",
    "🐉", "🦄", "🐙", "🦉",
]

# Все вместе - запасной список, если пол почему-то не указан
AVATAR_EMOJIS = AVATAR_EMOJIS_MALE + AVATAR_EMOJIS_FEMALE + AVATAR_EMOJIS_NEUTRAL

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
                with opener.open(req, timeout=10) as response:
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

def model_supports_thinking(model: str) -> bool:
    """
    Умеет ли модель размышлять (скрытая часть ответа перед видимым текстом).
    Ollama перечисляет это в capabilities: если модели размышления чужды, запрос
    с think=true она отклонит, поэтому спрашиваем заранее.
    """
    if not model or model == "human":
        return False
    if model in MODELS_THINKING_SUPPORT:
        return MODELS_THINKING_SUPPORT[model]
    
    try:
        req = urllib.request.Request(
            OLLAMA_SHOW_URL,
            data=json.dumps({"model": model}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            capabilities = json.loads(response.read().decode('utf-8')).get("capabilities") or []
        supported = "thinking" in capabilities
    except Exception as e:
        # Ошибку сети не запоминаем: иначе мигающая Ollama «научила» бы модель не думать
        print(f"  ⚠️  Не удалось узнать возможности модели {model}: {e}")
        return False
    
    MODELS_THINKING_SUPPORT[model] = supported
    return supported


def resolve_think(participant: dict):
    """
    Что передать в Ollama полем think: True/False или None («не вмешиваться»).
    "auto» - как в ENABLE_THINKING; «on» для модели без поддержки не отправляем.
    """
    mode = participant.get("think") or "auto"
    model = participant.get("model", "")
    
    if mode == "off":
        return False
    if mode == "on":
        if model_supports_thinking(model):
            return True
        print(f"  ⚠️  {model}: модель не умеет размышлять - режим «вкл» пропущен")
        return None
    
    return None if ENABLE_THINKING else False


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
                # Возможности модели (в том числе «thinking») свежий Ollama отдаёт
                # прямо в списке. Это экономит по запросу /api/show на каждую
                # модель: раньше первый открытый интерфейс дёргал Ollama десяток
                # раз подряд, и браузер успевал отвалиться по таймауту.
                capabilities = m.get("capabilities")
                if isinstance(capabilities, list):
                    MODELS_THINKING_SUPPORT[name] = "thinking" in capabilities
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

def fetch_model_parameters(model: str) -> dict:
    """
    Параметры генерации, вшитые в саму модель (Modelfile). Ollama отдаёт их
    в /api/show строкой вида «temperature 0.8». Именно эти значения действуют,
    пока поле участника пустое, поэтому интерфейс показывает их как подсказку.
    """
    if not model or model == "human":
        return {}
    if model in _MODEL_PARAMS_CACHE:
        return _MODEL_PARAMS_CACHE[model]
    
    params = {}
    try:
        req = urllib.request.Request(
            OLLAMA_SHOW_URL,
            data=json.dumps({"model": model}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = json.loads(response.read().decode('utf-8')).get("parameters") or ""
        for line in str(raw).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in PER_PARTICIPANT_OPTION_KEYS:
                try:
                    params[parts[0]] = float(parts[1])
                except ValueError:
                    continue
    except Exception as e:
        # Модель может быть не скачана или Ollama занята - просто нет подсказки
        print(f"  ℹ️  Параметры модели {model} неизвестны: {e}")
        return {}
    
    _MODEL_PARAMS_CACHE[model] = params
    return params


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

def load_theatre_settings():
    """
    Возвращает правила судьи, сохранённые в прошлых запусках: имена участников
    каждый спектакль новые, а правила судьи — общая настройка роли.
    """
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            rules = data.get("judge_rules") if isinstance(data, dict) else None
            if isinstance(rules, list) and rules:
                session.judge_rules = [str(r) for r in rules]
                print(f"⚖️  Загружены сохранённые правила судьи: {len(session.judge_rules)} пунктов")
    except Exception as e:
        print(f"  ⚠️  Не читается {SETTINGS_FILE.name}: {e}")

def save_theatre_settings():
    """Сохраняет правила судьи рядом с проектом (файл в .gitignore)."""
    try:
        SETTINGS_FILE.write_text(
            json.dumps({"judge_rules": session.judge_rules}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Не сохраняется {SETTINGS_FILE.name}: {e}")

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


def _merge_options(participant: dict) -> dict:
    """
    Собирает options для запроса в Ollama: глобальные OPTIONS +
    персональные настройки участника из PARTICIPANTS.
    """
    opts = dict(OPTIONS)
    for key in PER_PARTICIPANT_OPTION_KEYS:
        if participant.get(key) is not None:
            opts[key] = participant[key]
    return opts


def ask_model_with_tools(model: str, messages: list, supports_tools: bool = True, tool_choice: str = None, options: dict = None, think=None) -> tuple:
    """
    Отправляет запрос к модели. Автоматически определяет поддержку tools.
    
    Parameters:
        tool_choice: "auto" (по умолчанию), "any" (обязан вызвать инструмент), 
                     или None (не использовать)
        think: True/False - явно включить/выключить размышления (см. resolve_think),
               None - не трогать режим модели
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
            "options": options or OPTIONS,
            "stream": False
        }
        if think is not None:
            data["think"] = think
        elif not ENABLE_THINKING:
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

def ask_model(model: str, messages: list, participant_name: str, options: dict = None, think=None) -> tuple:
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
        
        content, tool_calls = ask_model_with_tools(model, messages, tool_choice=current_tool_choice,
                                                   options=options, think=think)
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
        # Пустой content у «думающих» моделей - это почти всегда размышления,
        # съевшие весь num_predict (done_reason: "length"). Просить финальный
        # ответ снова С размышлениями бесполезно: они опять займут бюджет.
        # Поэтому второй запрос идёт с think=False - тогда ответ приходит сразу.
        print(f"  ⚠️  {participant_name}: пустой ответ (размышления заняли весь бюджет) - "
              f"прошу финальный ответ без размышлений")
        messages.append({
            "role": "user",
            "content": "Дай свой финальный ответ на русском языке.",
            "name": "system"
        })
        
        try:
            data = {
                "model": model,
                "messages": messages,
                "options": options or OPTIONS,
                "stream": False,
                "think": False
            }
            
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
    """
    Инкапсулирует состояние и логику дебатов.

    Разделение сущностей:
      * instructions[name]      — личные инструкции (редактор)
      * static_instructions     — общие правила (редактор)
      * judge_rules             — правила судьи (редактор)
      * moderator_guidelines    — постоянные руководства модератора (редактор)
      * conversation_history    — поток: посты участников, бусты модератора, вердикты судьи
    """

    def __init__(self):
        self.running = False
        self.session_id = ""
        self.topic = ""
        self.posts = []
        self.current_round = 0
        self.current_participant = None
        self.current_action = None
        self.search_query = None
        self.finished = False
        self.avatars = {}
        self.avatar_emojis = {}
        self.static_instructions = []
        self.judge_rules = list(DEFAULT_JUDGE_RULES)
        self.moderator_guidelines = []
        self.waiting_for_human = False
        self.moderator_message = None
        self.moderator_finished = False
        self.runtime_participants = []
        self.conversation_history = []

    # ------------------------------------------------------------
    # Состав: единственный источник правды
    # ------------------------------------------------------------
    # До старта это заготовка следующего спектакля, после старта - живой состав.
    # Поэтому режиссёрский пульт правит одни и те же данные в любой момент,
    # а не дублирует форму настройки отдельной формой модератора.

    def load_new_cast(self):
        """Новый состав: роли и модели из PARTICIPANTS, свежие имена, эмодзи, профессии."""
        self.runtime_participants = build_new_cast()
        self.sync_cast_media()

    def sync_cast_media(self):
        """
        avatars/avatar_emojis - производные от состава: аватар и эмодзи лежат в самом
        участнике, поэтому переименование их не теряет.
        """
        self.avatars = {}
        self.avatar_emojis = {}
        for participant in self.runtime_participants:
            name = participant.get("display_name", "")
            if not name:
                continue
            if participant.get("avatar_url"):
                self.avatars[name] = participant["avatar_url"]
            self.avatar_emojis[name] = participant.get("avatar_emoji", "📣")

    def rename_participant(self, old_name: str, new_name: str):
        """
        Переименование на ходу. Уже опубликованные посты остаются как были (они уже
        отрисованы), но история для промптов и «кто сейчас на сцене» переезжают на
        новое имя - иначе модель считала бы прошлые реплики чужими.
        """
        if not old_name or old_name == new_name:
            return
        for post in self.conversation_history:
            if post.get("display_name") == old_name:
                post["display_name"] = new_name
        if self.current_participant == old_name:
            self.current_participant = new_name

    def start_show(self, topic: str):
        """
        Старт спектакля. Состав, правила, инструкции и тема уже лежат в сессии -
        начинаем с них, а не пересылаем всё заново из формы.
        """
        self.running = True
        self.session_id = uuid.uuid4().hex[:8]
        self.topic = topic
        self.posts = []
        self.current_round = 0
        self.current_participant = None
        self.current_action = None
        self.search_query = None
        self.finished = False
        self.waiting_for_human = False
        self.moderator_message = None
        self.moderator_finished = False
        self.conversation_history = []
        self.sync_cast_media()

    def new_show(self):
        """«Новый спектакль»: новый состав, настройки роли сохраняем."""
        guidelines = list(self.moderator_guidelines)
        judge_rules = list(self.judge_rules)
        static_instructions = list(self.static_instructions)
        self.__init__()
        self.moderator_guidelines = guidelines
        self.judge_rules = judge_rules
        self.static_instructions = static_instructions
        self.load_new_cast()

    # ------------------------------------------------------------
    # Работа с постами
    # ------------------------------------------------------------

    def add_post(self, display_name, model_used, content, round_num,
                 search_count=0, search_queries=None,
                 is_moderator=False, is_judge=False, gender="male"):
        avatar_url = self.avatars.get(display_name)
        avatar_emoji = self.avatar_emojis.get(display_name, "📣")

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
                "round": round_num,
            })
        return post

    def current_participant_role(self) -> str:
        """Роль того, кто сейчас говорит: пустая строка, "moderator" или "judge".

        Интерфейс показывает имя всегда, а роль - только если она особенная.
        """
        if not (self.waiting_for_human and self.current_participant):
            return ""
        for p in self.runtime_participants:
            if p.get("display_name") != self.current_participant:
                continue
            if p.get("is_moderator"):
                return "moderator"
            if p.get("is_judge"):
                return "judge"
        return ""

    def current_participant_is_moderator(self) -> bool:
        return self.current_participant_role() == "moderator"

    # ------------------------------------------------------------
    # Свежий буст модератора
    # ------------------------------------------------------------

    def last_moderator_message_since(self, participant_name: str) -> str:
        last_mod_idx, last_mod_msg, last_participant_idx = -1, "", -1
        for i, post in enumerate(self.conversation_history):
            if post.get("is_moderator") and post["content"].strip():
                last_mod_idx, last_mod_msg = i, post["content"]
            elif post.get("display_name") == participant_name:
                last_participant_idx = i
        return last_mod_msg if last_mod_idx > last_participant_idx else ""

    # ------------------------------------------------------------
    # Списки для плейсхолдеров
    # ------------------------------------------------------------

    def _get_participants_list(self) -> list:
        return [
            p["display_name"] for p in self.runtime_participants
            if not p.get("is_moderator") and not p.get("is_judge")
        ]

    def _get_moderators_list(self) -> list:
        return [
            p["display_name"] for p in self.runtime_participants
            if p.get("is_moderator")
        ]

    # ------------------------------------------------------------
    # Единый фильтр истории
    # ------------------------------------------------------------

    def _get_history_for(self, viewer: dict, mode: str = "dialog") -> list:
        """
        mode:
          "dialog"            — поток диалога (без вердиктов судьи, бусты inline)
          "participants_only" — только посты обычных участников (для судьи)
        """
        viewer_name = viewer.get("display_name", "")
        result = []
        for post in self.conversation_history:
            is_mod = post.get("is_moderator", False)
            is_judge = post.get("is_judge", False)

            if mode == "participants_only":
                if is_mod or is_judge:
                    continue
            elif mode == "dialog":
                if is_judge and post.get("display_name") != viewer_name:
                    continue
            result.append(post)
        return result

    # ------------------------------------------------------------
    # Формат истории в messages
    # ------------------------------------------------------------

    def _format_history(self, viewer_name: str, history: list) -> list:
        messages = []
        for post in history:
            speaker = post.get("display_name", "")
            speaker_norm = speaker.lower().replace(" ", "_")
            content = post.get("content", "")

            if post.get("is_moderator"):
                messages.append({
                    "role": "user",
                    "content": (
                        f"⚙️ МОДЕРАТОР ДИАЛОГА {speaker} "
                        f"(ведущий обсуждения) даёт указание: {content}"
                    ),
                    "name": "moderator",
                })
            elif speaker == viewer_name:
                messages.append({
                    "role": "assistant",
                    "content": content,
                    "name": speaker_norm,
                })
            else:
                messages.append({
                    "role": "user",
                    "content": f"{speaker} говорит: {content}",
                    "name": speaker_norm,
                })
        return messages

    # ------------------------------------------------------------
    # Блоки системного промпта
    # ------------------------------------------------------------

    def _substitute(self, text: str, participant: dict, other_names: list) -> str:
        text = text.replace("{ИМЯ}", participant.get("display_name", ""))
        text = text.replace("{СОБЕСЕДНИКИ}", ", ".join(other_names))
        text = text.replace("{ТЕМА}", self.topic)
        return text

    def _base_rules_block(self, participant: dict) -> list:
        other_names = [
            n for n in self._get_participants_list()
            if n != participant.get("display_name")
        ]
        if participant.get("is_judge"):
            rules = [r for r in (self.judge_rules or DEFAULT_JUDGE_RULES) if r.strip()]
        elif self.static_instructions:
            rules = [r for r in self.static_instructions if r.strip()]
        else:
            rules = DEFAULT_STATIC_INSTRUCTIONS
        return [self._substitute(r, participant, other_names) for r in rules]

    def _guidelines_block(self) -> list:
        if not self.moderator_guidelines:
            return []
        return [
            "ПРАВИЛА ОТ РУКОВОДИТЕЛЯ ДИАЛОГА (обязательны к исполнению):\n"
            + "\n".join(f"• {g}" for g in self.moderator_guidelines if g.strip())
        ]

    def _moderator_intro_block(self) -> list:
        moderators = self._get_moderators_list()
        if not moderators:
            return []
        return [
            "В этом диалоге также присутствует МОДЕРАТОР: " + ", ".join(moderators)
            + ". Это ведущий обсуждения — такой же участник, но с особыми полномочиями. "
              "Указания модератора имеют наивысший приоритет, обязательны к исполнению "
              "и могут менять тему, условия и правила диалога по ходу обсуждения."
        ]

    def _personal_instruction_block(self, participant: dict) -> list:
        other_names = [
            n for n in self._get_participants_list()
            if n != participant.get("display_name")
        ]
        custom = participant.get("instruction", "") or ""
        if not (custom and custom.strip()) and participant.get("is_judge"):
            custom = DEFAULT_JUDGE_INSTRUCTION
        if not (custom and custom.strip()):
            return []
        return [
            "ТВОИ ЛИЧНЫЕ ИНСТРУКЦИИ (обязательны к исполнению):\n"
            + self._substitute(custom.strip(), participant, other_names)
        ]

    def _search_block(self) -> list:
        if not ENABLE_SEARCH:
            return []
        min_text = f" Сделай минимум {MIN_SEARCHES} поиск(ов) перед ответом." if MIN_SEARCHES > 0 else ""
        return [
            "Если есть сомнения в фактах или мнениях - используй поиск для уточнения. "
            "При поиске НЕ указывай год." + min_text
        ]

    def get_system_prompt(self, participant: dict) -> str:
        is_judge = participant.get("is_judge", False)
        blocks = []
        blocks.extend(self._base_rules_block(participant))
        blocks.extend(self._guidelines_block())
        if not is_judge:
            blocks.extend(self._moderator_intro_block())
        blocks.extend(self._personal_instruction_block(participant))
        blocks.extend(self._search_block())
        return "\n\n".join(b for b in blocks if b)

    # ------------------------------------------------------------
    # Сборка сообщений для модели
    # ------------------------------------------------------------

    def build_messages_for_ai(self, participant: dict, round_num: int) -> list:
        name = participant.get("display_name", "")
        name_norm = name.lower().replace(" ", "_")
        is_judge = participant.get("is_judge", False)

        system_prompt = self.get_system_prompt(participant)
        messages = [{"role": "system", "content": system_prompt, "name": "system"}]

        # ---- Судья ----
        if is_judge:
            history = self._get_history_for(participant, mode="participants_only")
            history_messages = self._format_history(name, history)
            trimmed = trim_history_by_tokens(history_messages, estimate_tokens(system_prompt))
            messages.extend(trimmed)

            if trimmed:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Как {name}, оцени выступления участников. "
                        f"Для каждого участника укажи оценку от 1 до 10 баллов "
                        f"и краткое содержание его речи."
                    ),
                    "name": name_norm,
                })
            else:
                messages.append({
                    "role": "user",
                    "content": f"Как {name}, пока никто не говорил. Скажи, что оценивать нечего.",
                    "name": name_norm,
                })
            return messages

        # ---- Участник ----
        history = self._get_history_for(participant, mode="dialog")
        history_messages = self._format_history(name, history)
        trimmed = trim_history_by_tokens(history_messages, estimate_tokens(system_prompt))
        messages.extend(trimmed)

        participant_posts = [
            p for p in self.conversation_history
            if not p.get("is_moderator") and not p.get("is_judge")
        ]

        if round_num == 1 and not participant_posts:
            messages.append({
                "role": "user",
                "content": (
                    f'Как {name}, ты начинаешь обсуждение на тему "{self.topic}". '
                    f'Представься, обозначь свою позицию по теме и предложи другим высказаться.'
                ),
                "name": name_norm,
            })
        else:
            last_post = participant_posts[-1] if participant_posts else None
            if last_post:
                last_speaker = last_post["display_name"]
                if len(participant_posts) == 1:
                    messages.append({
                        "role": "user",
                        "content": (
                            f'{last_speaker} только что сказал: "{last_post["content"]}". '
                            f'Как {name}, ты тоже начинаешь обсуждение. Ответь {last_speaker} '
                            f'и вырази свою позицию по теме.'
                        ),
                        "name": name_norm,
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": (
                            f'{last_speaker} только что сказал: "{last_post["content"]}". '
                            f'Как {name}, ответь ему и другим участникам. '
                            f'НЕ повторяй уже сказанное — добавь новый аргумент, пример или контраргумент. '
                            f'Если тема исчерпана — предложи новый аспект или смежный вопрос.'
                        ),
                        "name": name_norm,
                    })
            else:
                messages.append({
                    "role": "user",
                    "content": f"Как {name}, продолжай диалог, обращаясь к другим участникам по именам.",
                    "name": name_norm,
                })

        last_mod_msg = self.last_moderator_message_since(name)
        if last_mod_msg:
            messages.append({
                "role": "user",
                "content": (
                    "⚠️ СИСТЕМНОЕ УКАЗАНИЕ ОТ МОДЕРАТОРА ДИАЛОГА "
                    "(приоритет выше всех остальных правил, обязательно учесть в этом ответе):\n"
                    f"{last_mod_msg}"
                ),
                "name": "moderator",
            })

        return messages

    # ------------------------------------------------------------
    # Ход AI
    # ------------------------------------------------------------

    def handle_ai_turn(self, participant: dict, round_num: int) -> tuple:
        self.current_action = "thinking"
        messages = self.build_messages_for_ai(participant, round_num)

        response, search_count, search_queries = ask_model(
            model=participant["model"],
            messages=messages,
            participant_name=participant["display_name"],
            options=_merge_options(participant),
            think=resolve_think(participant),
        )

        self.add_post(
            display_name=participant["display_name"],
            model_used=participant["model"],
            content=response,
            round_num=round_num,
            search_count=search_count,
            search_queries=search_queries,
            is_judge=participant.get("is_judge", False),
            gender=participant.get("gender", "male"),
        )
        self.current_action = None
        time.sleep(0.5)
        return response, search_count, search_queries


def _clamp_option(key: str, value):
    """Не даёт случайному разбросу выйти за допустимые пределы параметра."""
    if key == "temperature":
        return round(min(2.0, max(0.0, value)), 2)
    if key in ("top_p", "min_p"):
        return round(min(1.0, max(0.0, value)), 2)
    if key == "top_k":
        return max(1, int(value))
    if key == "seed":
        return int(value)
    return round(max(0.0, value), 2)  # repeat/presence/frequency_penalty


def draw_character(used: set = None) -> str:
    """Случайный характер для нового спектакля (без «custom» и без повторов)."""
    keys = [k for k, v in CHARACTER_PRESETS.items() if k != "custom" and v.get("params")]
    if not keys:
        return JUDGE_CHARACTER
    used = used or set()
    free = [k for k in keys if k not in used]
    return random.choice(free or keys)


def character_parameters(key: str) -> tuple:
    """Параметры и режим размышлений для характера: базовые значения плюс разброс.

    Разброс нужен, чтобы два «Педанта» в разных спектаклях всё же отличались.
    """
    preset = CHARACTER_PRESETS.get(key) or CHARACTER_PRESETS[JUDGE_CHARACTER]
    drift = preset.get("drift", CHARACTER_DRIFT)
    params = {}
    for param, base in (preset.get("params") or {}).items():
        step = drift.get(param, 0)
        value = base + random.uniform(-step, step) if step else base
        params[param] = _clamp_option(param, value)
    return params, preset.get("think", "auto")


def build_new_cast() -> list:
    """
    Состав спектакля: роли и модели из PARTICIPANTS, новые имена, эмодзи по полу
    и ключевые слова для аватара. Характер (температура и прочие параметры) либо
    берётся из PARTICIPANTS, либо разыгрывается случайно - см. RANDOMIZE_CHARACTERS.
    """
    cast = []
    available_male_names = MALE_NAMES.copy()
    available_female_names = FEMALE_NAMES.copy()
    available_emojis = (AVATAR_EMOJIS_MALE + AVATAR_EMOJIS_FEMALE
                        + AVATAR_EMOJIS_NEUTRAL)
    available_professions = PROFESSIONS.copy()
    used_characters = set()

    for template in PARTICIPANTS:
        gender = random.choice(["male", "female"])

        # Уникальное имя по полу, с запасным вариантом из другого списка
        if gender == "male":
            if available_male_names:
                name = random.choice(available_male_names)
                available_male_names.remove(name)
            elif available_female_names:
                name = random.choice(available_female_names)
                available_female_names.remove(name)
            else:
                name = "Участник"
        else:
            if available_female_names:
                name = random.choice(available_female_names)
                available_female_names.remove(name)
            elif available_male_names:
                name = random.choice(available_male_names)
                available_male_names.remove(name)
            else:
                name = "Участник"

        # Эмодзи по полу: сначала свои, потом нейтральные; в одном спектакле
        # два одинаковых аватара не встречаются
        own_pool = AVATAR_EMOJIS_MALE if gender == "male" else AVATAR_EMOJIS_FEMALE
        for pool in (own_pool, AVATAR_EMOJIS_NEUTRAL, available_emojis, AVATAR_EMOJIS):
            free = [e for e in pool if e in available_emojis]
            if free:
                emoji = random.choice(free)
                available_emojis.remove(emoji)
                break
        else:
            emoji = "📣"

        if available_professions:
            profession = random.choice(available_professions)
            available_professions.remove(profession)
        else:
            profession = "человек"

        # Ключевые слова для поиска аватара: с полом, иначе поисковик охотно
        # отдаёт женщине «мужчину-геолога»
        gender_word = "женщина" if gender == "female" else "мужчина"

        entry = {
            "model": template["model"],
            "display_name": name,
            "avatar_keywords": f"{profession} {gender_word}",
            "avatar_emoji": emoji,
            "avatar_url": None,
            "gender": gender,
            "is_moderator": template.get("is_moderator", False),
            "is_judge": template.get("is_judge", False),
            "instruction": DEFAULT_JUDGE_INSTRUCTION if template.get("is_judge") else "",
        }

        # Персональные параметры и режим размышлений из PARTICIPANTS: то, что задано
        # в конфиге, важнее случайного розыгрыша
        own_params = {key: template[key] for key in PER_PARTICIPANT_OPTION_KEYS
                      if template.get(key) is not None}
        entry.update(own_params)
        if template.get("think") in THINK_MODES and template.get("think") != "auto":
            entry["think"] = template["think"]
        if isinstance(template.get("preset"), str) and template["preset"]:
            entry["preset"] = template["preset"]

        # Характер на этот спектакль: у судьи он всегда один и тот же
        elif RANDOMIZE_CHARACTERS and template.get("model") != "human" and not own_params:
            character = JUDGE_CHARACTER if template.get("is_judge") else draw_character(used_characters)
            used_characters.add(character)
            params, think = character_parameters(character)
            entry.update(params)
            entry["preset"] = character
            if think != "auto":
                entry["think"] = think

        cast.append(entry)

    return cast


def apply_cast_patch(incoming: list) -> str:
    """
    Правка состава по индексам: имя, пол, модель, ключевые слова, аватар и параметры
    генерации. Одинаково работает и для заготовки, и для идущего спектакля: прошлые
    реплики не трогаются, меняется только то, что будет сказано дальше.
    Возвращает текст ошибки либо пустую строку.
    """
    cast = session.runtime_participants
    if len(incoming) != len(cast):
        return f"В составе {len(cast)} участников, получено {len(incoming)}"

    updates = []
    for index, raw in enumerate(incoming):
        if not isinstance(raw, dict):
            return "Каждый участник должен быть объектом"
        entry = cast[index]
        name = str(raw.get("display_name", entry.get("display_name", "")) or "").strip()
        if not name:
            return "У всех участников должны быть имена"
        gender = str(raw.get("gender", entry.get("gender", "male")) or "male")
        if gender not in ("male", "female"):
            return f"{name}: пол может быть только «male» или «female»"
        model = str(raw.get("model", entry.get("model", "")) or "").strip()

        # Режим размышлений и «характер» — не числа Ollama, а наши поля
        think = raw.get("think", entry.get("think", "auto"))
        think = str(think or "auto")
        if think not in THINK_MODES:
            return f"{name}: размышления могут быть auto, on или off"
        preset = raw.get("preset", entry.get("preset", ""))
        preset = preset if isinstance(preset, str) else ""

        # Параметры генерации разбираем здесь же: запрос применяется целиком либо
        # отклоняется целиком, иначе ошибка у последнего участника оставила бы
        # предыдущих уже переименованными, а форма - с прежними именами.
        options = {}
        for key in PER_PARTICIPANT_OPTION_KEYS:
            if key not in raw:
                continue
            value = raw.get(key)
            if value is None or value == "":
                options[key] = None      # пусто = как в глобальных OPTIONS
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                return f"{name}: «{key}» должен быть числом"
            if key in ("top_k", "seed"):
                number = int(number)
            if key == "temperature" and not 0.0 <= number <= 2.0:
                return f"{name}: температура должна быть от 0 до 2"
            if key in ("top_p", "min_p") and not 0.0 <= number <= 1.0:
                return f"{name}: «{key}» должна быть от 0 до 1"
            if key in ("repeat_penalty", "presence_penalty", "frequency_penalty") and number < 0:
                return f"{name}: «{key}» не может быть отрицательной"
            options[key] = number

        updates.append({"entry": entry, "raw": raw, "name": name, "gender": gender,
                        "model": model, "options": options, "think": think, "preset": preset})

    names = [u["name"] for u in updates]
    if len(set(names)) != len(names):
        return "Имена участников должны быть разными"

    # Модели можно менять на ходу: проверяем все сразу, до применения.
    # Недоступная Ollama не мешает сохранить состав - модели ещё проверятся на старте.
    models = sorted({u["model"] for u in updates if u["model"] and u["model"] != "human"})
    models_status = check_models_available(models, force=True)
    if models_status["missing"] and not models_status["error"]:
        return models_problem_message(models_status)
    if models_status["error"]:
        print(f"⚠️  Модели не проверены ({models_status['error']}), состав сохраняю как есть")

    old_models = {p.get("model", "") for p in cast if p.get("model")}
    busy = session.current_action == "thinking"

    for u in updates:
        entry, raw = u["entry"], u["raw"]
        old_name = entry.get("display_name", "")

        for key, number in u["options"].items():
            if number is None:
                entry.pop(key, None)
            else:
                entry[key] = number

        if u["think"] == "auto":
            entry.pop("think", None)
        else:
            entry["think"] = u["think"]
        if u["preset"]:
            entry["preset"] = u["preset"][:32]
        else:
            entry.pop("preset", None)

        if u["name"] != old_name:
            session.rename_participant(old_name, u["name"])
            entry["display_name"] = u["name"]
        entry["gender"] = u["gender"]
        # Роль (человек или модель) не меняется: у людей модель остаётся "human"
        if entry.get("model") != "human" and u["model"] and u["model"] != "human":
            entry["model"] = u["model"]
        if raw.get("avatar_keywords") is not None:
            entry["avatar_keywords"] = str(raw["avatar_keywords"])
        if raw.get("avatar_emoji"):
            entry["avatar_emoji"] = str(raw["avatar_emoji"])
        if "avatar_url" in raw:
            entry["avatar_url"] = raw.get("avatar_url") or None

    session.sync_cast_media()

    # Заменённые модели этому спектаклю больше не нужны - освобождаем память
    # (но не ту, что прямо сейчас считает реплику)
    if not busy:
        used = {p.get("model", "") for p in cast if p.get("model")}
        for model in sorted(old_models - used):
            if model and model != "human":
                unload_model(model)

    return ""


# Глобальный экземпляр сессии
session = DebateSession()

# Правила судьи из прошлых запусков (редактируются на ходу режиссёра)
load_theatre_settings()

# Состав заготовлен заранее: режиссёрский пульт правит его и до старта спектакля
session.load_new_cast()


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
    
    # Модели спектакля: их выгружаем по окончании и держим по одной в памяти.
    # Состав берём из сессии, а не из снимка - модели можно менять на ходу.
    def show_model_names() -> set:
        return {p.get("model", "") for p in session.runtime_participants
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
                    # (модель могла быть заменена режиссёром на ходу)
                    unload_other_show_models(participant.get("model", ""), show_model_names())
                    
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
            for model in show_model_names():
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
        /* Тема — обычный текст по левому краю: её часто пишут пунктами,
           а по центру многострочный список нечитаем */
        .header-topic { font-size: 19px; font-weight: normal; color: #000000; margin-top: 20px; padding: 24px 28px; border: 2px solid #000000; text-align: left; line-height: 1.7; min-height: 60px; white-space: pre-wrap; word-wrap: break-word; }
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
        /* Разделы режиссёрского пульта: та же типографика, что у блоков сайдбара */
        .panel-section { border-top: 1px solid #000000; padding-top: 24px; margin-top: 28px; }
        .panel-heading { display: flex; align-items: baseline; gap: 12px; margin-bottom: 8px; }
        .panel-heading .num { font-family: 'Courier New', monospace; font-size: 13px; color: #999999; letter-spacing: 1px; }
        .panel-heading .name { font-size: 14px; text-transform: uppercase; letter-spacing: 2px; }
        .panel-note { font-size: 12px; color: #666666; line-height: 1.55; margin-bottom: 14px; }
        /* Поле сюжета: раньше было неотличимо от подписи — теперь заметное и на вырост */
        .topic-input { display: block; width: 100%; min-height: 170px; box-sizing: border-box; padding: 16px 18px; border: 1px solid #000000; font-family: Georgia, serif; font-size: 17px; line-height: 1.7; color: #000000; resize: vertical; }
        .topic-input:focus { outline: none; border: 2px solid #000000; }
        /* Разделы пульта сворачиваются: на ходу целый состав занимает экран зря */
        .panel-heading { cursor: pointer; user-select: none; }
        .panel-heading:hover .name { text-decoration: underline; }
        .panel-heading .caret { margin-left: auto; font-size: 12px; color: #888888; }
        .panel-body.collapsed { display: none; }
        /* Ключевые слова аватара и кнопка поиска — одной строкой, рядом с аватаром */
        .keyword-row { display: flex; gap: 10px; align-items: stretch; }
        .keyword-row input { flex: 1; min-width: 0; }
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
        /* Нулевой раздел (готовность): метка о проблемах видна и в свёрнутом виде */
        .ready-badge { margin-left: 8px; font-size: 11px; font-weight: bold; letter-spacing: 1px; color: #b00020; }
        /* Пояснение вместо поля реплики, когда не ваша очередь: блок остаётся на месте */
        .turn-note { font-size: 13px; font-style: italic; color: #666666; padding: 2px 0 4px 0; line-height: 1.6; }
        /* Числовые поля параметров: пустое поле серое, заполненное — чёрное */
        .param-input { border: 1px solid #cccccc; }
        .param-input:not(.filled) { color: #555555; }
        .param-input.filled { border-color: #000000; color: #000000; }

        /* ══ Тёмная сцена ═══════════════════════════════════════════════
           Класс dark на body перекрашивает интерфейс: чёрные элементы (рамки,
           заголовки, карточки) становятся светлыми, чтобы быть видными на
           тёмном фоне. Часть цветов интерфейс ставит инлайном — их приходится
           перебивать, поэтому дальше встречается !important. */
        body.dark { background: #0d0d0d; color: #e8e8e8; color-scheme: dark; }
        body.dark .sidebar, body.dark .header, body.dark .card, body.dark .post,
        body.dark .footer, body.dark .modal-content { background: #141414; border-color: #3a3a3a; }
        body.dark .header h1, body.dark .card h2, body.dark .header-subtitle, body.dark .header-date,
        body.dark .sidebar-title, body.dark .post-author, body.dark .post-text, body.dark .post-model,
        body.dark .post-time, body.dark .header-topic, body.dark .search-info, body.dark .footer,
        body.dark .panel-heading .name, body.dark .status-bar, body.dark .btn { color: #e8e8e8; }
        body.dark .header-topic, body.dark .panel-section, body.dark .post, body.dark .header,
        body.dark .footer, body.dark .sidebar, body.dark .search-info, body.dark .post-header { border-color: #3a3a3a; }
        body.dark input, body.dark textarea, body.dark select { background: #1c1c1c !important; color: #e8e8e8 !important; border-color: #5a5a5a !important; }
        body.dark input::placeholder, body.dark textarea::placeholder { color: #7d7d7d !important; }
        body.dark .btn-primary { background: #e8e8e8; color: #111111; }
        body.dark .btn-primary:hover { background: #cfcfcf; }
        body.dark .btn-secondary { background: #1c1c1c; color: #e8e8e8; }
        body.dark .btn-secondary:hover { background: #272727; }
        body.dark .avatar-preview, body.dark .post-avatar .emoji { background: #141414; border-color: #3a3a3a; }
        body.dark .avatar-preview:hover { border-color: #9a9a9a; }
        body.dark .post-avatar img, body.dark .avatar-preview img { filter: grayscale(100%) brightness(0.82); }
        body.dark .status-bar { border-left-color: #e8e8e8; }
        body.dark .post-text code { background: #1c1c1c; border-color: #4a4a4a; }
        body.dark .role-participant { background: #12283a; color: #79b8ff; border-color: #2f5a80; }
        body.dark .role-moderator { background: #33260f; color: #ffb066; border-color: #7a5520; }
        body.dark .role-judge { background: #281735; color: #c79ae0; border-color: #6a3f8a; }
        body.dark .param-input { border-color: #5a5a5a; }
        body.dark .param-input:not(.filled) { color: #a0a0a0; }
        body.dark .param-input.filled { border-color: #cfcfcf; color: #e8e8e8; }
        body.dark .turn-note { color: #a0a0a0; }
        body.dark .ready-badge { color: #ff6b6b; }
        body.dark .panel-note { color: #a3a3a3; }
        body.dark .btn { border-color: #6f6f6f; }
        /* Эти блоки интерфейс переключает через style.display, а браузер при этом
           переписывает весь атрибут style и превращает #666 в rgb(102,102,102),
           так что по аттрибуту их уже не поймать — красим по id */
        body.dark #modelsWarning { border-color: #ff6b6b !important; color: #ff8a8a !important; }
        body.dark #vramWarning { border-color: #e8c56b !important; color: #e8c56b !important; }
        body.dark #readyOk { color: #a3a3a3 !important; }
        /* Инлайновые плашки и пояснения внутри пульта */
        body.dark [style*="#ffffff"], body.dark [style*="#fafafa"],
        body.dark [style*="#f9f9f9"], body.dark [style*="#f5f5f5"] { background: #181818 !important; }
        body.dark [style*="#cccccc"], body.dark [style*="#ddd"] { border-color: #3a3a3a !important; }
        body.dark [style*="border:1px solid #000"], body.dark [style*="border:2px solid #000"],
        body.dark [style*="border:1px dashed #000"] { border-color: #6f6f6f !important; }
        body.dark [style*="border:1px solid #7b1fa2"] { border-color: #a06fc0 !important; }
        body.dark [style*="color:#666"], body.dark [style*="color:#555"],
        body.dark [style*="color:#888"] { color: #a3a3a3 !important; }
        body.dark [style*="color:#999"] { color: #8c8c8c !important; }
        body.dark [style*="color:#333"] { color: #c9c9c9 !important; }
        body.dark [style*="color:#b00020"] { color: #ff6b6b !important; }
        body.dark [style*="border:2px solid #b00020"] { border-color: #ff6b6b !important; }
        body.dark [style*="border:2px solid #b8860b"] { border-color: #e8c56b !important; }
        body.dark [style*="color:#b8860b"], body.dark [style*="color:#8a6d00"] { color: #e8c56b !important; }
        body.dark [style*="color:#7b1fa2"] { color: #c79ae0 !important; }
        body.dark [style*="color:#1976d2"] { color: #79b8ff !important; }
        body.dark [style*="color:#f57c00"] { color: #ffb066 !important; }
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
                <!-- Единый режиссёрский пульт: та же форма служит и настройкой
                     спектакля, и пультом модератора на ходу -->
                <!-- Единый режиссёрский пульт: одна форма и для настройки, и для управления
                     на ходу. Разделы пронумерованы в порядке работы режиссёра. -->
                <div class="card" id="controlPanel">
                    <h2 id="controlPanelTitle">Режиссёрский пульт</h2>

                    <!-- Нулевой раздел: готовность к спектаклю. Раскрывается сам, когда
                         есть о чём предупредить, и сворачивается, когда всё в порядке -->
                    <div class="panel-section" id="sec-ready">
                        <div class="panel-heading"><span class="num">00</span><span class="name">Готовность</span><span class="ready-badge" id="readyBadge"></span></div>
                        <div style="display:flex;gap:15px;flex-wrap:wrap;align-items:center;margin-bottom:12px;">
                            <button class="btn btn-secondary" onclick="checkReadiness()" style="padding:6px 15px;font-size:13px;margin:0;">🔄 Проверить сейчас</button>
                            <span style="font-size:12px;color:#666;">Проверка идёт при загрузке страницы и при правках состава</span>
                        </div>
                        <div id="readyOk" style="font-size:13px;color:#666;font-style:italic;">Проверка ещё не проходила.</div>
                        <div id="modelsWarning" style="display:none;margin:0 0 14px 0;padding:14px 16px;border:2px solid #b00020;color:#b00020;font-size:15px;line-height:1.5;"></div>
                        <div id="vramWarning" style="display:none;margin:0;padding:14px 16px;border:2px solid #b8860b;color:#8a6d00;font-size:15px;line-height:1.5;"></div>
                    </div>

                    <div class="panel-section">
                        <div class="panel-heading"><span class="num">01</span><span class="name">Сюжет</span></div>
                        <div class="panel-note">Тема попадает в системные промпты следующих реплик. Менять можно и до спектакля, и на ходу.</div>
                        <textarea id="topicInput" class="topic-input" rows="7" placeholder="Тема одной строкой или с пунктами — переносы строк сохраняются. Ctrl+Enter — применить." onkeydown="if (event.ctrlKey &amp;&amp; event.key === 'Enter') { event.preventDefault(); applyTopic(); }"></textarea>
                        <div style="margin-top:10px;">
                            <button class="btn btn-secondary" onclick="applyTopic()">🎯 Применить тему</button>
                            <span style="font-size:12px;color:#666;">Ctrl+Enter — применить не отрывая рук</span>
                        </div>
                    </div>

                    <div class="panel-section" id="sec-cast">
                        <div class="panel-heading"><span class="num">02</span><span class="name">Состав</span></div>
                        <div class="panel-note">Имена, пол, модели и параметры генерации. Правки действуют сразу: до спектакля — на заготовку, на ходу — на будущие реплики (уже сказанное не меняется). Характер (температура и прочее) на каждый спектакль разыгрывается случайно.</div>
                        <div id="castEditor"></div>
                        <div style="display:flex;gap:15px;flex-wrap:wrap;align-items:center;margin-top:6px;">
                            <button class="btn btn-secondary" onclick="saveCast()" style="margin:0;">💾 Применить состав</button>
                            <button class="btn btn-secondary" onclick="randomizeCharacters()" style="margin:0;" title="Заново вытянуть случайный характер каждому ИИ-участнику, кроме судьи (числа, вписанные вручную, будут перезаписаны)">🎲 Разбросать характеры</button>
                            <span id="randomizeHint" style="font-size:12px;color:#666;"></span>
                        </div>
                    </div>

                    <div class="panel-section">
                        <div class="panel-heading"><span class="num">03</span><span class="name">Правила и инструкции</span></div>
                        <div class="panel-note">Общие правила общения, руководства модератора, правила судьи и личные инструкции участников. Работают одинаково до и во время спектакля.</div>
                        <button class="btn btn-secondary" onclick="toggleInstructionsEditor()" style="margin-bottom:15px;">🔧 Открыть редактор</button>

                        <div id="instructionsEditor" style="display:none;">
                            <div style="font-size:13px;color:#333;margin-bottom:15px;padding:10px;background:#f9f9f9;border:1px solid #ddd;">
                                <strong>Доступные плейсхолдеры:</strong>
                                <code>{ИМЯ}</code> — имя текущего участника,
                                <code>{СОБЕСЕДНИКИ}</code> — остальные через запятую,
                                <code>{ТЕМА}</code> — тема обсуждения
                            </div>

                            <div style="margin-bottom:20px;">
                                <label style="display:block; font-weight:bold; margin-bottom:8px; font-size:14px;">Правила общения (для всех участников):</label>
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

                    <!-- Блок «Ваша реплика» всегда на месте: раньше он исчезал, и нумерация
                         разделов прыгала с 03 сразу на 05. Меняется только содержимое —
                         поле реплики или пояснение, почему его сейчас нет -->
                    <div class="panel-section" id="turnSection">
                        <div class="panel-heading"><span class="num">04</span><span class="name" id="turnTitle">Ваша реплика</span></div>
                        <div id="turnNote" class="turn-note">Спектакль ещё не начат — поле появится, когда очередь дойдёт до вас.</div>
                        <div id="turnComposer" style="display:none;">
                            <div class="panel-note">Пустое сообщение = пропуск действия. Ctrl+Enter — отправить.</div>
                            <textarea id="moderatorInput" rows="4" style="width:100%; padding:12px; border:2px solid #000000; font-size:16px; font-family:Georgia,serif; margin-bottom:15px;" placeholder="Напишите реплику или оставьте пустым чтобы пропустить действие..." onkeydown="if (event.ctrlKey &amp;&amp; event.key === 'Enter') { event.preventDefault(); sendModeratorMessage(); }"></textarea>
                            <div style="display:flex; gap:15px; align-items:center;">
                                <button class="btn btn-primary" onclick="sendModeratorMessage()">Отправить</button>
                                <span style="font-size:12px;color:#666;font-style:italic;">Реплика станет постом от вашего имени</span>
                            </div>
                        </div>
                    </div>

                    <div class="panel-section">
                        <div class="panel-heading"><span class="num">05</span><span class="name">Управление спектаклем</span></div>
                        <div class="panel-note">«Завершить» опускает занавес, но не закрывает театр: после него можно собрать новый состав и играть дальше. Сервер останавливает «Покинуть театр».</div>
                        <div style="display:flex;gap:15px;flex-wrap:wrap;">
                            <button class="btn btn-primary" id="startBtn" onclick="startDebate()">🎭 Начать спектакль</button>
                            <button class="btn btn-secondary" id="finishBtn" onclick="finishDebate()" style="display:none;">⏹ Завершить спектакль</button>
                            <button class="btn btn-secondary" id="newBtn" onclick="newShow()" style="display:none;">🎭 Новый спектакль</button>
                        </div>
                    </div>
                </div>
                <div id="posts"></div>
                <div class="footer">
                    <button class="btn btn-secondary" id="themeBtn" onclick="toggleTheme()" title="Светлая и тёмная сцена">🌙 Тёмная сцена</button>
                    <button class="btn btn-secondary" onclick="shutdownServer()">Покинуть театр</button>
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
        
        // Состав спектакля. У сервера он один и тот же и до старта, и на сцене,
        // поэтому страница не держит вторую (свою) копию настроек
        let cast = [];
        let models = [];               // скачанные модели Ollama для выбора в составе
        let thinkingModels = [];       // из них те, что умеют размышлять (capabilities Ollama)
        let debateRunning = false;
        let pollInterval = null;
        let lastPostCount = 0;
        let instructionsTick = 0;
        let defaultJudgePrompt = '';  // им заполняется пустое поле промпта судьи
        let mySessionId = null;        // id текущей сессии; следим за сменой на сервере
        let showFinished = false;      // спектакль идёт или уже завершён (но не новый спектакль)
        let finishRequested = false;   // занавес заказан, ждём, пока модель доиграет реплику
        // Счётчик для строк, добавленных кнопками «➕»: индекс по длине контейнера
        // повторялся после удаления строки выше, и ❌ у новой строки удалял чужую
        let editorRowSeq = 0;
        
        // Разделы пульта сворачиваются со заголовка: обёртки расставляет этот вызов
        decoratePanelSections();

        // Состав и список моделей
        loadCast().then(() => { updatePanel(); updateSidebarParticipants(); tryRestoreSession(); });
        fetch('/api/models')
            .then(r => r.json())
            .then(data => {
                models = data.models || [];
                thinkingModels = data.thinking_models || [];
                if (data.error) console.warn('Список моделей недоступен: ' + data.error);
                renderCastEditor();
            })
            .catch(err => console.warn('Не удалось получить список моделей:', err));
        
        function loadCast() {
            return fetch('/api/participants', {cache: 'no-store'})
                .then(r => r.json())
                .then(data => {
                    cast = data.participants || [];
                    // Список характеров держит сервер: тот же набор он разыгрывает
                    // случайно при подъёме занавеса
                    if (data.characters) CHARACTERS = data.characters;
                    renderCastEditor();
                    renderModelsWarning(data.models_status);
                    renderVramWarning(data.vram_status);
                    syncReadinessSection();
                    const hint = document.getElementById('randomizeHint');
                    if (hint) {
                        hint.textContent = data.randomize_characters
                            ? 'на каждый спектакль характеры тянутся заново' : '';
                    }
                    if (data.topic) document.getElementById('topicInput').value = data.topic;
                    return data;
                })
                .catch(err => { console.error('Не удалось загрузить состав:', err); return {}; });
        }
        
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
        
        // Восстановление активной сессии при загрузке страницы: спектакль идёт
        // (или уже отыгран) — возвращаемся к нему, а не начинаем новый.
        function tryRestoreSession() {
            fetch('/api/status?lastPostCount=0', {cache: 'no-store'})
                .then(r => r.json())
                .then(data => {
                    if (!data.session_id) return;
                    if (!data.running && !data.finished) return;
                    if (!data.running && !data.total_posts) return;

                    mySessionId = data.session_id;
                    debateRunning = true;
                    showFinished = !!data.finished;

                    document.getElementById('topicDisplay').textContent = data.topic || '—';
                    if (data.topic) document.getElementById('topicInput').value = data.topic;
                    document.getElementById('posts').innerHTML = '';

                    (data.new_posts || []).forEach(post => addPost(post));
                    lastPostCount = data.total_posts || 0;

                    if (data.running) {
                        pollInterval = setInterval(updatePosts, 3000);
                    }
                    updatePosts();
                })
                .catch(() => {});
        }

        // «Характер» — готовые наборы параметров генерации. Числа управляют не смыслом
        // реплик (его задаёт личная инструкция), а тем, насколько участник предсказуем,
        // склонен повторяться и размышляет ли перед ответом.
        // Список приходит с сервера: он же используется для случайного розыгрыша
        // характеров на новый спектакль, поэтому он один на всех.
        let CHARACTERS = {};
        const CUSTOM_CHARACTER = {label: '🎚 Свой — сам выберу', group: 'manual',
                                  hint: 'числа выставляются вручную в «Тонкой настройке»', params: {}};
        const CHARACTER_GROUPS = {balanced: 'Уравновешенные', extreme: 'С перекосом', manual: 'Вручную'};

        function characterInfo(key) {
            return (key && CHARACTERS[key]) ? CHARACTERS[key] : CUSTOM_CHARACTER;
        }

        // Числовые параметры, которыми управляет пульт (остальные берутся из Modelfile модели)
        const PARAM_KEYS = ['temperature', 'top_p', 'repeat_penalty', 'presence_penalty', 'frequency_penalty'];

        // Пересчитывает строку «Уйдёт в модель» по текущим полям формы — чтобы
        // не приходилось сохранять состав, чтобы понять, что применится
        function refreshEffective(idx) {
            const box = document.getElementById('effective-' + idx);
            if (!box) return;
            const defaults = (cast[idx] && cast[idx].model_defaults) || {};
            const parts = PARAM_KEYS.map(key => {
                const el = document.getElementById(key + '-' + idx);
                const raw = el ? el.value : '';
                const own = raw !== '';
                const value = own ? raw : (defaults[key] === undefined ? '—' : defaults[key]);
                return key + ' <strong>' + value + '</strong>'
                    + (own ? '' : ' <span style="color:#999;">(как в модели)</span>');
            });
            box.innerHTML = '<strong>Уйдёт в модель:</strong> ' + parts.join(' · ')
                + '<br>Пустое поле — параметр вообще не отправляется: действует значение из Modelfile модели.';
        }

        // «q1» и «q1:latest» — одна и та же модель
        function modelSupportsThinking(name) {
            if (!name || !thinkingModels.length) return false;
            const base = n => String(n).split(':')[0];
            return thinkingModels.some(m => base(m) === base(name));
        }

        // Характер просто заполняет поля — дальше числа можно править руками
        function applyPreset(idx) {
            const select = document.getElementById('preset-' + idx);
            const preset = characterInfo(select ? select.value : 'custom');
            const hint = document.getElementById('preset-hint-' + idx);
            if (hint) hint.textContent = preset.hint || '';

            Object.entries(preset.params || {}).forEach(([key, value]) => {
                const el = document.getElementById(key + '-' + idx);
                if (el) el.value = value;
            });

            // Набор задаёт и размышления, но «Свой» их не трогает; модель без
            // поддержки размышлений не получит think=true — показываем честно
            const thinkEl = document.getElementById('think-' + idx);                if (thinkEl && preset.think && preset.group !== 'manual') {
                    thinkEl.value = preset.think;
                    if (thinkEl.value === 'on' && thinkEl.dataset.supportsThinking === '0') {
                        thinkEl.value = 'off';
                    }
                }
                refreshParamStyles(idx);
                refreshEffective(idx);
        }

        // Поле с числом: заполненное — выделено, пустое — серое (параметр не отправляется).
        // Цвета заданы классами в CSS, чтобы тёмная сцена перекрашивала их вместе со всем
        function refreshParamStyles(idx) {
            PARAM_KEYS.forEach(key => {
                const el = document.getElementById(key + '-' + idx);
                if (el) el.classList.toggle('filled', el.value !== '');
            });
        }

        // Ручная правка числа: набор больше не подходит, помечаем «Свой»
        function onParamInput(idx) {
            const select = document.getElementById('preset-' + idx);
            if (select && select.value !== 'custom') {
                select.value = 'custom';
                const hint = document.getElementById('preset-hint-' + idx);
                if (hint) hint.textContent = CUSTOM_CHARACTER.hint;
            }
            refreshParamStyles(idx);
            refreshEffective(idx);
        }

        // Числа не мозолят глаза, пока их не спросят
        function toggleTuning(idx) {
            const box = document.getElementById('tuning-' + idx);
            const caret = document.getElementById('tuning-caret-' + idx);
            if (!box) return;
            const hidden = box.style.display === 'none';
            box.style.display = hidden ? 'block' : 'none';
            if (caret) caret.textContent = hidden ? ' ▾' : ' ▸';
        }

        // «🎲 Разбросать характеры»: новый случайный характер каждому ИИ-участнику.
        // Ту же лотерею сервер проводит сам при подъёме занавеса (RANDOMIZE_CHARACTERS)
        function randomizeCharacters() {
            const keys = Object.keys(CHARACTERS).filter(k => k !== 'custom' && CHARACTERS[k].params
                && Object.keys(CHARACTERS[k].params).length);
            if (!keys.length) { alert('Список характеров не загружен — обновите страницу'); return; }
            const taken = [];
            cast.forEach((p, idx) => {
                // Судье характер не разыгрываем: он должен судить одинаково строго
                if (p.model === 'human' || p.is_judge) return;
                let pool = keys.filter(k => taken.indexOf(k) === -1);
                if (!pool.length) pool = keys;
                const key = pool[Math.floor(Math.random() * pool.length)];
                taken.push(key);
                const select = document.getElementById('preset-' + idx);
                if (select) select.value = key;
                const thinkEl = document.getElementById('think-' + idx);
                if (thinkEl && CHARACTERS[key].think) {
                    thinkEl.value = CHARACTERS[key].think === 'on' && thinkEl.dataset.supportsThinking === '0'
                        ? 'off' : CHARACTERS[key].think;
                }
                applyPreset(idx);
            });
        }

        // Состав — одна и та же форма и для настройки спектакля, и для правок на ходу
        // Выбор значения для селекта: «q1» и «q1:latest» — одна модель
        function selectIf(value, current) {
            return String(value) === String(current) ? 'selected' : '';
        }

        // Состав — одна и та же форма и для настройки спектакля, и для правок на ходу.
        // Параметры генерации видны сразу: у пустого поля подсказкой стоит значение из
        // OPTIONS, а под ними написано, что именно уйдёт в модель.
        function renderCastEditor() {
            const container = document.getElementById('castEditor');
            if (!cast.length) {
                container.innerHTML = '<div style="color:#666;font-style:italic;font-size:13px;">Состав пуст</div>';
                return;
            }
            container.innerHTML = cast.map((p, idx) => {
                const isHuman = p.model === 'human';
                let roleBadge = '<span class="role-badge role-participant">🎭 УЧАСТНИК</span>';
                let borderColor = '#000000';
                if (p.is_moderator) {
                    roleBadge = '<span class="role-badge role-moderator">🎬 МОДЕРАТОР</span>';
                } else if (p.is_judge) {
                    roleBadge = '<span class="role-badge role-judge">⚖️ СУДЬЯ</span>';
                    borderColor = '#7b1fa2';
                }
                const avatar = p.avatar_url
                    ? '<img src="' + escapeHtml(p.avatar_url) + '">'
                    : (p.avatar_emoji || '📣');
                const fieldLabel = text => '<label style="font-size:11px;text-transform:uppercase;letter-spacing:1px;">' + text + '</label>';
                const fieldStyle = 'width:100%;padding:7px;border:1px solid #000;font-family:Georgia,serif;font-size:14px;';

                // У человека нет ни модели, ни параметров: это сама роль
                const modelField = isHuman
                    ? fieldLabel('Модель') + '<div style="font-size:13px;color:#666;padding:8px 0;">живой участник</div>'
                    : fieldLabel('Модель') + '<select id="model-' + idx + '" style="' + fieldStyle + '">' + modelOptions(p.model) + '</select>';

                const supportsThinking = modelSupportsThinking(p.model);
                const thinkValue = p.think || 'auto';
                const presetValue = CHARACTERS[p.preset] ? p.preset : 'custom';
                const defaults = p.model_defaults || {};

                // Одно числовое поле: пусто = параметр не отправляется вообще, и работает
                // значение из Modelfile модели. Подсказка сереньким показывает именно его,
                // даже если поле только что очистили
                // Класс filled, а не инлайновые цвета: так поле перекрашивается вместе
                // с темой (тёмная сцена иначе оставила бы чёрную рамку на чёрном)
                const paramField = (key, label, step, min, max, hint) => {
                    const value = (p[key] === undefined || p[key] === null) ? '' : p[key];
                    const fallback = defaults[key] === undefined ? 'как в модели' : defaults[key];
                    const range = (min === null ? '' : ' min="' + min + '"') + (max === null ? '' : ' max="' + max + '"');
                    return '<div>' + fieldLabel(label)
                        + '<input type="number" class="param-input' + (value === '' ? '' : ' filled') + '"'
                        + ' id="' + key + '-' + idx + '" step="' + step + '"' + range
                        + ' value="' + value + '" placeholder="' + fallback + '" title="' + hint + '"'
                        + ' oninput="onParamInput(' + idx + ')" style="width:100%;padding:6px;font-size:13px;">'
                        + '</div>';
                };

                // Список характеров приходит с сервера; сгруппирован, чтобы сразу
                // было видно, где «ровные» наборы, а где с перекосом
                const characterSelect = (() => {
                    const keys = Object.keys(CHARACTERS);
                    if (!keys.length) {
                        return '<option value="custom" selected>' + escapeHtml(CUSTOM_CHARACTER.label) + '</option>';
                    }
                    const groups = {};
                    ['custom'].concat(keys.filter(k => k !== 'custom')).forEach(key => {
                        const info = characterInfo(key);
                        const group = info.group || 'balanced';
                        (groups[group] = groups[group] || []).push(
                            '<option value="' + key + '" ' + selectIf(key, presetValue) + '>' + escapeHtml(info.label) + '</option>');
                    });
                    return Object.keys(groups).map(group =>
                        '<optgroup label="' + escapeHtml(CHARACTER_GROUPS[group] || group) + '">'
                        + groups[group].join('') + '</optgroup>').join('');
                })();

                const paramsBlock = isHuman ? '' : ''
                    + '<div style="margin-top:14px;padding-top:12px;border-top:1px dotted #cccccc;">'
                    +   '<div style="display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap;margin-bottom:12px;">'
                    +     '<div style="min-width:230px;">' + fieldLabel('Характер')
                    +       '<select id="preset-' + idx + '" onchange="applyPreset(' + idx + ')" style="' + fieldStyle + '"'
                    +         ' title="Готовый набор параметров генерации. Числа управляют тем, КАК участник говорит, а что он говорит — задаёт его личная инструкция. На новый спектакль характер разыгрывается случайно.">'
                    +         characterSelect
                    +       '</select>'
                    +     '</div>'
                    +     '<div style="min-width:220px;">' + fieldLabel('Размышления')
                    +       '<select id="think-' + idx + '" data-supports-thinking="' + (supportsThinking ? 1 : 0) + '" style="' + fieldStyle + '"'
                    +         ' title="Скрытое рассуждение модели перед ответом. Умеют не все модели — у остальных этот режим недоступен.">'
                    +         '<option value="auto" ' + selectIf('auto', thinkValue) + '>Авто (как в ENABLE_THINKING)</option>'
                    +         '<option value="off" ' + selectIf('off', thinkValue) + '>Выключены — отвечает сразу</option>'
                    +         '<option value="on" ' + selectIf('on', thinkValue) + (supportsThinking ? '' : ' disabled') + '>Включены — сначала думает</option>'
                    +       '</select>'
                    +     '</div>'
                    +     '<div id="preset-hint-' + idx + '" style="font-size:11px;color:#666;max-width:260px;padding-top:20px;line-height:1.5;">'
                    +       escapeHtml(characterInfo(presetValue).hint || '') + (supportsThinking ? '' : '<br>размышления этой модели недоступны')
                    +     '</div>'
                    +   '</div>'
                    // Числа спрятаны: в настройке они только мешают, а строка
                    // «Уйдёт в модель» ниже и так показывает, что уйдёт в Ollama
                    +   '<button class="btn btn-secondary" onclick="toggleTuning(' + idx + ')" style="padding:4px 12px;font-size:12px;margin:0 0 10px 0;">'
                    +     '⚙ Тонкая настройка<span id="tuning-caret-' + idx + '"> ▸</span></button>'
                    +   '<div id="tuning-' + idx + '" style="display:none;">'
                    +     '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;">'
                    +       paramField('temperature', 'Температура', '0.1', 0, 2,
                                'Насколько свободно выбираются слова: 0-0.3 предсказуемо, 0.8-1.2 живая речь, выше 1.5 текст рассыпается. Выше 2 не принимается.')
                    +       paramField('top_p', 'top_p', '0.05', 0, 1,
                                'Отсекает маловероятные слова: меньше — предсказуемее. Делает почти то же, что температура, поэтому крутить надо что-то одно.')
                    +       paramField('repeat_penalty', 'repeat_penalty', '0.05', 0, null,
                                'Штраф за повторы: 1.1-1.3 спасает от зацикливания, выше 1.6 ломает грамматику.')
                    +       paramField('presence_penalty', 'presence_penalty', '0.1', 0, null,
                                'Подталкивает к новым темам, а не к пересказу сказанного: 0.3-0.6.')
                    +       paramField('frequency_penalty', 'frequency_penalty', '0.1', 0, null,
                                'Режет частые слова, мягче чем repeat_penalty: 0.3-0.6.')
                    +     '</div>'
                    +   '</div>'
                    // Содержимое дособерёт refreshEffective(idx) ниже: так строка
                    // не разойдётся с подсказками в самих полях
                    +   '<div id="effective-' + idx + '" style="font-size:11px;color:#666;margin-top:9px;line-height:1.6;"></div>'
                    + '</div>';

                return ''
                + '<div data-participant-index="' + idx + '" style="margin-bottom:18px;padding:14px;border:1px solid ' + borderColor + ';background:#ffffff;">'
                +   '<div style="display:flex;gap:15px;align-items:flex-start;">'
                +     '<div style="flex-shrink:0;">'
                +       '<div class="avatar-preview" id="avatar-preview-' + idx + '" style="width:96px;height:96px;font-size:46px;" onclick="openAvatarModal(' + idx + ')" title="Показать аватар целиком">' + avatar + '</div>'
                +     '</div>'
                +     '<div style="flex:1;min-width:0;">'
                +       '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;">'
                +         '<div>' + fieldLabel('Имя')
                +           '<input type="text" id="name-' + idx + '" value="' + escapeHtml(p.display_name || '') + '" placeholder="Введите имя" style="width:100%;padding:8px;border:1px solid #000;font-family:Georgia,serif;font-size:16px;">'
                +         '</div>'
                +         '<div>' + fieldLabel('Пол (влияет только на аватар)')
                +           '<select id="gender-' + idx + '" style="' + fieldStyle + '">'
                +             '<option value="male" ' + selectIf('male', p.gender) + '>♂ Мужской</option>'
                +             '<option value="female" ' + selectIf('female', p.gender) + '>♀ Женский</option>'
                +           '</select>'
                +         '</div>'
                +         '<div>' + modelField + '</div>'
                +       '</div>'
                // Ключевые слова и поиск аватара стоят одной строкой рядом с аватаром:
                // раньше кнопка была в подвале карточки, и её приходилось искать
                +       '<div style="margin-top:12px;">' + fieldLabel('Ключевые слова для аватара')
                +         '<div class="keyword-row">'
                +           '<input type="text" id="keywords-' + idx + '" value="' + escapeHtml(p.avatar_keywords || '') + '" placeholder="Например: дипломат женщина" style="' + fieldStyle + '">'
                +           '<button class="btn btn-secondary" id="search-avatar-' + idx + '" onclick="searchAvatar(' + idx + ')" style="padding:0 16px;margin:0;font-size:13px;white-space:nowrap;" title="Найти картинку по этим словам">🔍 Найти аватар</button>'
                +         '</div>'
                +       '</div>'
                +       paramsBlock
                +     '</div>'
                +   '</div>'
                +   '<div style="margin-top:14px;">' + roleBadge + '</div>'
                + '</div>';
            }).join('');
            // У людей параметров нет, у моделей строка «Уйдёт в модель» собирается по полям
            cast.forEach((p, idx) => { if (p.model !== 'human') refreshEffective(idx); });
        }
        
        function modelOptions(current) {
            const list = models.slice();
            // Модель из PARTICIPANTS может быть с тегом: показываем её, даже если список иной
            if (current && !list.includes(current)) list.unshift(current);
            if (!list.length) return `<option value="${escapeHtml(current || '')}" selected>${escapeHtml(current || 'нет моделей')}</option>`;
            return list.map(name => `<option value="${escapeHtml(name)}" ${name === current ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('');
        }
        
        function collectCast() {
            return cast.map((p, idx) => {
                const pick = (id, fallback) => { const el = document.getElementById(id); return el ? el.value : fallback; };
                const entry = {
                    display_name: pick(`name-${idx}`, p.display_name).trim(),
                    gender: pick(`gender-${idx}`, p.gender),
                    avatar_keywords: pick(`keywords-${idx}`, p.avatar_keywords),
                    avatar_emoji: p.avatar_emoji,
                    avatar_url: p.avatar_url || null,
                };
                if (p.model !== 'human') {
                    entry.model = pick(`model-${idx}`, p.model);
                    // Пустая строка = «как в OPTIONS»: сервер убирает такое поле у участника
                    ['temperature', 'top_p', 'repeat_penalty',
                     'presence_penalty', 'frequency_penalty'].forEach(key => {
                        const raw = pick(`${key}-${idx}`, '');
                        entry[key] = raw === '' ? null : raw;
                    });
                    entry.think = pick(`think-${idx}`, p.think || 'auto');
                    entry.preset = pick(`preset-${idx}`, p.preset || 'custom');
                }
                return entry;
            });
        }
        
        function saveCast() {
            fetch('/api/participants', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({participants: collectCast()})
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success) { alert('❌ ' + (data.error ||'не удалось применить состав')); return; }
                cast = data.participants || cast;
                renderCastEditor();
                updateSidebarParticipants();
                loadCast();   // заодно обновляем проверки моделей и видеопамяти
            })
            .catch(err => { console.error('Ошибка правки состава:', err); alert('❌ ' + err.message); });
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
        
        function searchAvatar(idx) {
            const keywords = (document.getElementById(`keywords-${idx}`)?.value || '').trim()
                || (cast[idx] ? cast[idx].display_name : '');
            if (!keywords) { alert('Сначала введите имя или ключевые слова для аватара'); return; }
            
            // Блокируем кнопку во время загрузки (ищем её по id: кнопка больше
            // не единственный элемент в строке с ключевыми словами)
            const btn = document.getElementById('search-avatar-' + idx) || event.target;
            if (btn.disabled) return;
            btn.disabled = true;
            btn.textContent = '⏳ Поиск...';
            
            const preview = document.getElementById(`avatar-preview-${idx}`);
            const fallbackEmoji = (cast[idx] && cast[idx].avatar_emoji) || '📣';
            preview.innerHTML = '⏳';
            
            fetch(`/api/avatar/${encodeURIComponent(keywords)}`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ keywords: keywords, participant_idx: idx })
            }).then(r => r.json()).then(data => {
                if (data.avatar_url) {
                    cast[idx].avatar_url = data.avatar_url;
                    preview.innerHTML = `<img src="${escapeHtml(data.avatar_url)}">`;
                } else {
                    preview.innerHTML = '❌';
                    setTimeout(() => preview.innerHTML = fallbackEmoji, 2000);
                }
                btn.disabled = false;
                btn.textContent = '🔍 Найти аватар';
            }).catch(err => { console.error('Ошибка поиска аватара:', err); preview.innerHTML = '❌'; setTimeout(() => preview.innerHTML = fallbackEmoji, 2000); btn.disabled = false; btn.textContent = '🔍 Найти аватар'; });
        }
        
        function openAvatarModal(idx) { const u = cast[idx] && cast[idx].avatar_url; if (u) { document.getElementById('avatarModalImg').src = u; document.getElementById('avatarModal').style.display = 'block'; } }
        function closeAvatarModal() { document.getElementById('avatarModal').style.display = 'none'; }
        
        // Тема: одна кнопка на обе стадии — и в настройке, и на ходу режиссёра
        function applyTopic() {
            const input = document.getElementById('topicInput');
            const topic = input.value.trim();
            if (!topic) { alert('Введите тему'); return; }
            
            fetch('/api/moderator/topic', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({topic: topic}) })
                .then(r => r.json())
                .then(data => {
                    if (!data.success) { alert('❌ ' + (data.error || 'не удалось сменить тему')); return; }
                    input.value = data.topic;
                    document.getElementById('topicDisplay').textContent = data.topic;
                })
                .catch(err => { console.error('Ошибка смены темы:', err); alert('❌ ' + err.message); });
        }
        
        function startDebate() {
            const topic = document.getElementById('topicInput').value.trim();
            if (!topic) { alert('Введите тему!'); return; }
            document.getElementById('startBtn').disabled = true;
            
            // Сначала отправляем правки из формы, потом стартуем: состав живёт на сервере
            fetch('/api/participants', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({participants: collectCast()})
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success) throw new Error(data.error || 'не удалось применить состав');
                cast = data.participants || cast;
                return fetch('/api/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({topic: topic}) });
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success) { showStartError(data.error || 'неизвестная'); return; }
                if (data.session_id) mySessionId = data.session_id;
                debateRunning = true;
                showFinished = false;
                finishRequested = false;
                lastPostCount = 0;
                document.getElementById('posts').innerHTML = '';
                document.getElementById('topicDisplay').textContent = topic;
                // Спектакль пошёл: убираем баннер с прошлой неудачной попытки
                const box = document.getElementById('modelsWarning');
                if (box) { box.style.display = 'none'; box.innerHTML = ''; }
                pollInterval = setInterval(updatePosts, 3000);
                renderCastEditor();
                updateSidebarParticipants();
                updatePanel();
            })
            .catch(err => { console.error('Ошибка запуска:', err); showStartError(err.message); });
        }
        
        function showStartError(message) {
            debateRunning = false;
            document.getElementById('startBtn').disabled = false;
            const box = document.getElementById('modelsWarning');
            if (box) { box.innerHTML = `⚠️ Не удалось начать спектакль: ${escapeHtml(message)}`; box.style.display = 'block'; }
            updatePanel();
            alert('Не удалось начать спектакль: ' + message);
        }
        
        // «Новый спектакль»: сервер собирает новый состав, настройки роли остаются
        function newShow() {
            if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
            debateRunning = false;
            showFinished = false;
            finishRequested = false;
            lastPostCount = 0;
            mySessionId = null;
            document.getElementById('posts').innerHTML = '';
            document.getElementById('statusBar').style.display = 'none';
            document.getElementById('statusPlaceholder').style.display = 'block';
            document.getElementById('topicInput').value = '';
            document.getElementById('topicDisplay').textContent = '—';
            setTurnState('hidden');
            fetch('/api/reset', {method: 'POST'})
                .then(r => r.json())
                .then(data => { if (data.participants) cast = data.participants; return loadCast(); })
                .then(() => { updateSidebarParticipants(); updatePanel(); })
                .catch(err => console.error('Ошибка сброса:', err));
        }
        
        // Кнопки пульта: до спектакля — настройка, во время — остановка, после занавеса —
        // новый спектакль. «Завершить» доступна всё время спектакля: режиссёру не нужно
        // ждать своей очереди, чтобы остановить действие.
        function updatePanel() {
            const startBtn = document.getElementById('startBtn');
            const finishBtn = document.getElementById('finishBtn');
            const newBtn = document.getElementById('newBtn');
            const title = document.getElementById('controlPanelTitle');
            if (debateRunning && !showFinished) {
                startBtn.style.display = 'none';
                newBtn.style.display = 'none';
                // Пока сервер не подтвердил занавес, повторно не показываем: иначе
                // кнопка мелькала бы обратно, пока модель доигрывает реплику
                finishBtn.style.display = finishRequested ? 'none' : 'inline-block';
                title.textContent = 'Режиссёрский пульт — спектакль идёт';
            } else if (showFinished) {
                startBtn.style.display = 'none';
                newBtn.style.display = 'inline-block';
                finishBtn.style.display = 'none';
                title.textContent = 'Режиссёрский пульт — занавес';
            } else {
                startBtn.style.display = 'inline-block';
                startBtn.disabled = false;
                newBtn.style.display = 'none';
                finishBtn.style.display = 'none';
                title.textContent = 'Режиссёрский пульт — настройка';
            }
            syncSectionsToPhase();
        }

        // ── Сворачивание разделов пульта ─────────────────────────────────
        // Разметка разделов остаётся плоской: обёртку и каретку дописывает этот код,
        // чтобы не расписывать одно и то же в каждом разделе
        function decoratePanelSections() {
            document.querySelectorAll('#controlPanel .panel-section').forEach((section, i) => {
                if (section.dataset.collapsible) return;
                const heading = section.querySelector('.panel-heading');
                if (!heading) return;
                section.dataset.collapsible = '1';
                section.dataset.sectionKey = section.id || ('section-' + i);
                const body = document.createElement('div');
                body.className = 'panel-body';
                Array.from(section.children).forEach(child => {
                    if (child !== heading) body.appendChild(child);
                });
                section.appendChild(body);
                const caret = document.createElement('span');
                caret.className = 'caret';
                caret.textContent = '▾';
                heading.appendChild(caret);
                heading.title = 'Свернуть / развернуть раздел';
                heading.addEventListener('click', () => setSectionCollapsed(
                    section.dataset.sectionKey, !body.classList.contains('collapsed')));
            });
        }

        function setSectionCollapsed(key, collapsed) {
            const section = document.querySelector('[data-section-key="' + key + '"]');
            if (!section) return;
            const body = section.querySelector('.panel-body');
            const caret = section.querySelector('.panel-heading .caret');
            if (!body) return;
            body.classList.toggle('collapsed', !!collapsed);
            if (caret) caret.textContent = collapsed ? '▸' : '▾';
        }

        // Свернуть или развернуть все разделы пульта разом
        function setAllSectionsCollapsed(collapsed) {
            document.querySelectorAll('#controlPanel .panel-section').forEach(section => {
                if (section.dataset.sectionKey) setSectionCollapsed(section.dataset.sectionKey, collapsed);
            });
        }

        // Фазы пульта: в настройке раскрыто всё; спектакль идёт — состав убран
        // (он огромный, а на ходу нужен редко); после занавеса — свернуто всё,
        // спектакль отыгран и пульт остаётся одними заголовками. Ручное
        // сворачивание не трогаем: панель реагирует только на смену фазы
        let sectionsPhase = null;
        function syncSectionsToPhase() {
            const phase = showFinished ? 'finished' : (debateRunning ? 'running' : 'setup');
            if (phase === sectionsPhase) return;
            sectionsPhase = phase;
            if (phase === 'finished') setAllSectionsCollapsed(true);
            else if (phase === 'running') setSectionCollapsed('sec-cast', true);
            else setAllSectionsCollapsed(false);
        }

        // Как назвать роль в интерфейсе: у обычного участника никакой особой роли нет
        function roleLabelOf(role) {
            return role === 'moderator' ? 'модератор' : (role === 'judge' ? 'судья' : '');
        }

        // Раздел «Ваша реплика» всегда на месте: без него нумерация блоков прыгала
        // с 03 на 05. Меняется только содержимое — поле ввода или пояснение
        function setTurnState(state, who, roleName) {
            const composer = document.getElementById('turnComposer');
            const note = document.getElementById('turnNote');
            const title = document.getElementById('turnTitle');
            if (!composer || !note || !title) return;
            if (state === 'your') {
                composer.style.display = 'block';
                note.style.display = 'none';
                title.textContent = 'Ход: ' + (who || 'вы') + (roleName ? ' · ' + roleName : '');
                return;
            }
            composer.style.display = 'none';
            note.style.display = 'block';
            title.textContent = 'Ваша реплика';
            note.textContent = state === 'waiting'
                ? 'Сейчас не ваше время выступать: говорит ' + (who || 'другой участник') + '.'
                : state === 'finished' ? 'Занавес: реплики закончились.'
                : state === 'sent' ? 'Реплика отправлена — ждём ответа других участников.'
                : 'Спектакль ещё не начат — поле появится, когда очередь дойдёт до вас.';
        }

        // Раздел 00: содержимое рисуют renderModelsWarning / renderVramWarning,
        // а здесь решается, раскрыт ли раздел, и ставится метка в заголовке
        let readySignature = null;
        function syncReadinessSection() {
            const shown = el => !!(el && el.style.display !== 'none' && el.innerHTML.trim() !== '');
            const models = document.getElementById('modelsWarning');
            const vram = document.getElementById('vramWarning');
            const okBox = document.getElementById('readyOk');
            const badge = document.getElementById('readyBadge');
            const problems = [];
            if (shown(models)) problems.push('модели');
            if (shown(vram)) problems.push('видеопамять');
            if (okBox) okBox.style.display = problems.length ? 'none' : 'block';
            if (badge) badge.textContent = problems.length ? '⚠️ ' + problems.length : '';
            const signature = problems.join(',');
            // Раскрываем, когда появилось о чём предупредить, и сворачиваем, когда
            // всё в порядке. Пока набор предупреждений тот же — раздел не трогаем:
            // иначе он не давал бы свернуть себя руками
            if (signature === readySignature) return;
            readySignature = signature;
            setSectionCollapsed('sec-ready', problems.length === 0);
        }

        // Ручная проверка готовности — не трогая то, что уже введено в форме
        function checkReadiness() {
            const btn = document.querySelector('#sec-ready button');
            if (btn) { btn.disabled = true; btn.textContent = '⏳ Проверяю...'; }
            fetch('/api/participants', {cache: 'no-store'})
                .then(r => r.json())
                .then(data => {
                    renderModelsWarning(data.models_status);
                    renderVramWarning(data.vram_status);
                    syncReadinessSection();
                    refreshMemory();
                })
                .catch(err => console.warn('Проверка готовности не удалась:', err))
                .finally(() => {
                    if (btn) { btn.disabled = false; btn.textContent = '🔄 Проверить сейчас'; }
                });
        }

        // Тёмная сцена: выбор запоминается, при первом входе берётся из настроек системы
        function applyTheme(dark) {
            document.body.classList.toggle('dark', !!dark);
            const btn = document.getElementById('themeBtn');
            if (btn) btn.textContent = dark ? '☀️ Светлая сцена' : '🌙 Тёмная сцена';
        }

        function toggleTheme() {
            const dark = !document.body.classList.contains('dark');
            applyTheme(dark);
            try { localStorage.setItem('theatreTheme', dark ? 'dark' : 'light'); } catch (e) {}
        }

        (function initTheme() {
            let saved = null;
            try { saved = localStorage.getItem('theatreTheme'); } catch (e) {}
            const systemDark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
            applyTheme(saved === null ? systemDark : saved === 'dark');
        })();
        
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
            // Свежие реплики сверху: пульт и поле реплики тоже наверху, и читать
            // спектакль снизу вверх не приходится
            postsDiv.insertBefore(postDiv, postsDiv.firstChild);
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
                // Сессия сменилась на сервере — сбросить локальный UI.
                if (data.session_id) {
                    if (mySessionId === null) {
                        mySessionId = data.session_id;
                    } else if (mySessionId !== data.session_id) {
                        // Спектакль начался в другой вкладке: переходим к просмотру
                        mySessionId = data.session_id;
                        debateRunning = true;
                        lastPostCount = 0;
                        document.getElementById('posts').innerHTML = '';
                        setTurnState('hidden');
                        loadCast().then(() => { updateSidebarParticipants(); updatePanel(); });
                        return;
                    }
                }
                const statusDiv = document.getElementById('statusBar');
                const statusPlaceholder = document.getElementById('statusPlaceholder');
                statusDiv.style.display = 'block'; statusPlaceholder.style.display = 'none';
                // Флаги ставим до отрисовки пульта: после перезагрузки страницы
                // он должен сразу знать, что спектакль идёт, а не ждать нового старта
                debateRunning = true;
                showFinished = !!data.finished;
                if (data.topic) document.getElementById('topicDisplay').textContent = data.topic;
                updatePanel();
                if (data.waiting_for_human) {
                    // Ход человека: имя всегда, роль — только если она особенная
                    const roleName = roleLabelOf(data.current_participant_role);
                    const wasOpen = document.getElementById('turnComposer').style.display === 'block';
                    setTurnState('your', data.current_participant, roleName);
                    if (!wasOpen) {
                        const mi = document.getElementById('moderatorInput');
                        if (mi && !mi.value.trim()) mi.focus();
                    }
                    statusDiv.classList.add('active');
                    statusDiv.innerHTML = `<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">Акт ${data.current_round}</div><div>${escapeHtml(data.current_participant || '')}</div><div style="font-style:italic;font-size:12px;margin-top:8px;">Ваш ход!</div>`;
                } else {
                    // Не ваша очередь: блок остаётся на месте с пояснением, чтобы
                    // нумерация разделов пульта не прыгала
                    setTurnState(data.finished ? 'finished' : 'waiting', data.current_participant);
                }
                if (data.running && !data.waiting_for_human) {
                    statusDiv.classList.add('active');
                    let at = data.current_action === 'searching' ? `Ищет: "${data.search_query}"` : data.current_action === 'waiting' ? 'Готовит реплику...' : 'Говорит реплику...';
                    statusDiv.innerHTML = `<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">Акт ${data.current_round}</div><div>${escapeHtml(data.current_participant || '')}</div><div style="font-style:italic;font-size:12px;margin-top:8px;">${at}</div>`;
                } else if (data.finished) {
                    statusDiv.classList.remove('active');
                    statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;">🎭 Занавес</div>';
                    setTurnState('finished');
                    document.getElementById('finishBtn').style.display = 'none';
                    // «Покинуть театр» оставляем: занавес больше не закрывает сервер,
                    // и это единственная кнопка остановки приложения
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
        
        function sendModeratorMessage() {
            const input = document.getElementById('moderatorInput');
            fetch('/api/moderator/message', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: input.value}) })
            .then(r => r.json()).then(data => { if (data.success) { input.value = ''; setTurnState('sent'); } else alert('Ошибка: ' + (data.error || 'неизвестная')); })
            .catch(err => { console.error('Ошибка:', err); alert('Ошибка: ' + err.message); });
        }
        
        function updateSidebarParticipants() {
            // Получаем актуальные инструкции с сервера
            fetch('/api/moderator/instructions', {cache: 'no-store'})
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
                
                // Состав у страницы и у сервера один и тот же (cast), поэтому сайдбар
                // всегда совпадает со сценой: и в настройке, и после перезагрузки
                document.getElementById('participantsDisplay').innerHTML = cast.map(p => {
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
                    let html = `<div style="margin-bottom:12px;">${roleIcon} <strong>${escapeHtml(p.display_name)}</strong> ${genderSymbol}${roleLabel} <small>(${escapeHtml(p.model)})</small>`;
                    if (instruction) {
                        html += `<br><em style="margin-left:10px;">${escapeHtml(instruction)}</em>`;
                    }
                    return html + '</div>';
                }).join('');
                
                // Обновляем блок "Правила общения"
                const rulesDisplay = document.getElementById('rulesDisplay');
                if (data.static_instructions && data.static_instructions.length > 0) {
                    rulesDisplay.innerHTML = data.static_instructions
                        .filter(rule => rule.trim())
                        .map(rule => `<div style="margin-bottom:8px;">• ${escapeHtml(rule)}</div>`)
                        .join('');
                } else {
                    rulesDisplay.innerHTML = '<div style="color:#999;">Правила не заданы</div>';
                }
                
                // Обновляем блок "Инструкции от руководства"
                const modInstructionsDisplay = document.getElementById('moderatorInstructionsDisplay');
                if (data.moderator_messages && data.moderator_messages.length > 0) {
                    modInstructionsDisplay.innerHTML = data.moderator_messages
                        .filter(msg => msg.trim())
                        .map(msg => `<div style="margin-bottom:8px;">• ${escapeHtml(msg)}</div>`)
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
        
        // «Завершить спектакль» опускает занавес, но НЕ закрывает театр: сервер остаётся
        // живым, опрос видит finished и показывает «🎭 Новый спектакль». Остановка сервера -
        // отдельная кнопка «Покинуть театр».
        function finishDebate() {
            if (confirm('Опустить занавес? После этого можно собрать новый спектакль.')) {
                finishRequested = true;
                fetch('/api/moderator/finish', {method: 'POST'})
                    .then(() => updatePosts())
                    .catch(() => {});

                // Немедленный отклик, не дожидаясь сервера
                setTurnState('hidden');
                document.getElementById('finishBtn').style.display = 'none';

                const statusDiv = document.getElementById('statusBar');
                statusDiv.style.display = 'block';
                statusDiv.classList.remove('active');
                statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">🎭 Опускаю занавес…</div><div style="font-style:italic;">Режиссёр завершил представление</div>';
                document.getElementById('statusPlaceholder').style.display = 'none';
            }
        }
        
        function shutdownServer() {
            if (confirm('Завершить работу сервера?')) {
                // СРАЗУ останавливаем polling
                if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
                
                // СРАЗУ скрываем кнопку выхода — чтобы нельзя было нажать повторно
                const exitBtn = document.querySelector('.footer .btn');
                if (exitBtn) exitBtn.style.display = 'none';
                
                // СРАЗУ обновляем UI
                setTurnState('hidden');
                document.getElementById('statusPlaceholder').style.display = 'none';
                
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
            const idx = 'new' + (++editorRowSeq);
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;';
            div.innerHTML = `
                <textarea id="judge-rule-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Новое правило судьи..."></textarea>
                <button class="btn btn-secondary" onclick="removeJudgeRuleEditor('${idx}')" style="padding:8px 12px;margin:0;">❌</button>
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
            const idx = 'new' + (++editorRowSeq);
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;';
            div.innerHTML = `
                <textarea id="static-instr-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Новая инструкция..."></textarea>
                <button class="btn btn-secondary" onclick="removeStaticInstructionEditor('${idx}')" style="padding:8px 12px;margin:0;">❌</button>
            `;
            container.appendChild(div);
        }
        
        function addModeratorMessageEditor() {
            const container = document.getElementById('moderatorMessagesEditor');
            const idx = 'new' + (++editorRowSeq);
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;';
            div.innerHTML = `
                <textarea id="mod-msg-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Новое руководство..."></textarea>
                <button class="btn btn-secondary" onclick="removeModeratorMessageEditor('${idx}')" style="padding:8px 12px;margin:0;">❌</button>
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

@app.route('/api/models')
def get_models():
    """Скачанные модели Ollama: ими заполняется список выбора модели в составе."""
    models, error = fetch_ollama_models(force=True)
    names = sorted(models.keys()) if models else []
    return jsonify({
        "models": names,
        # Кто из моделей умеет размышлять: интерфейс не даст включить это там, где нельзя
        "thinking_models": [n for n in names if model_supports_thinking(n)],
        "error": error or "",
    })

@app.route('/api/participants', methods=['GET', 'POST'])
def participants():
    """
    GET  - состав спектакля (пока он не начат – заготовка) плюс проверки моделей
           и видеопамяти. Имена больше не генерируются на каждый запрос, поэтому
           перезагрузка страницы не подменяет труппу.
    POST - правка состава: {participants: [{display_name, gender, model, temperature, ...}]}.
           Одинаково работает и в настройке, и на ходу режиссёра.
    """
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        incoming = data.get("participants")
        if not isinstance(incoming, list):
            return jsonify({"success": False, "error": "Нужен список participants"})
        error = apply_cast_patch(incoming)
        if error:
            print(f"⛔ Состав не изменён: {error}")
            return jsonify({"success": False, "error": error})
        print(f"🎭 Состав обновлён: {[p.get('display_name') for p in session.runtime_participants]}")
        return jsonify({"success": True, "participants": session.runtime_participants})

    models = [p.get("model", "") for p in session.runtime_participants]
    # Пустое поле участника значит «взять из OPTIONS». Чтобы это не приходилось
    # держать в голове, отдаём интерфейсу действующие значения и их источник.
    cast = []
    for p in session.runtime_participants:
        item = dict(p)
        if p.get("model") != "human":
            merged = _merge_options(p)
            item["effective_options"] = {
                k: merged[k] for k in PER_PARTICIPANT_OPTION_KEYS if merged.get(k) is not None
            }
            item["own_options"] = [k for k in PER_PARTICIPANT_OPTION_KEYS if p.get(k) is not None]
            # Пока поле пустое, работают параметры из самого Modelfile модели
            item["model_defaults"] = fetch_model_parameters(p.get("model", ""))
        cast.append(item)
    return jsonify({
        "participants": cast,
        "option_keys": list(PER_PARTICIPANT_OPTION_KEYS),
        # «Характер»: список и его числа держит сервер, чтобы пульт и случайный
        # розыгрыш на новый спектакль опирались на один и тот же набор
        "characters": {
            key: {"label": value["label"], "hint": value.get("hint", ""),
                  "group": value.get("group", "balanced"),
                  "think": value.get("think", "auto"),
                  "params": value.get("params") or {}}
            for key, value in CHARACTER_PRESETS.items()
        },
        "randomize_characters": RANDOMIZE_CHARACTERS,
        "running": session.running,
        "finished": session.finished,
        "topic": session.topic,
        # Сразу сообщаем интерфейсу, если нужных моделей нет в Ollama
        "models_status": check_models_available(models),
        # И если они не влезают в видеопамять при текущем num_ctx
        "vram_status": check_vram_fit(models),
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
        
        cast = session.runtime_participants
        if not isinstance(participant_idx, int) or not 0 <= participant_idx < len(cast):
            return jsonify({"error": "participant_idx вне состава"}), 400
        
        # generate_avatar_for_participant() работает только с именем и ключевыми словами
        participant = {
            "display_name": keywords,
            "avatar_keywords": keywords
        }
        
        # Кнопка «Найти аватар» должна искать заново, а не отдавать закэшированную ссылку
        AVATAR_URL_CACHE.pop(keywords, None)
        
        avatar_url = generate_avatar_for_participant(participant)
        
        # Аватар живёт в составе: его видит и следующий спектакль, и идущий
        cast[participant_idx]["avatar_url"] = avatar_url or None
        session.sync_cast_media()
        
        if avatar_url:
            return jsonify({"avatar_url": avatar_url})
        return jsonify({"avatar_url": None})
    except Exception as e:
        print(f"⚠️  Ошибка при поиске аватара: {e}")
        return jsonify({"avatar_url": None, "error": str(e)})

@app.route('/api/start', methods=['POST'])
def start():
    """
    Начать спектакль тем составом, который уже собран в сессии. Тело запроса может
    уточнить тему: {"topic": "..."} - остальное режиссёр всё равно правит через пульт.
    """
    if session.running:
        return jsonify({"success": False, "error": "Уже запущено"})

    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "") or "").strip() or (session.topic or "").strip()
    if not topic:
        return jsonify({"success": False, "error": "Тема не указана"})
    session.topic = topic

    cast = session.runtime_participants
    if not cast:
        return jsonify({"success": False, "error": "Не выбрано ни одного участника"})

    # Участник без модели не сможет говорить, а поток дебатов упал бы уже на сцене
    participants_without_model = [
        p.get("display_name") or "без имени"
        for p in cast
        if p.get("model") != "human" and not p.get("model")
    ]
    if participants_without_model:
        return jsonify({
            "success": False,
            "error": f"У участников не указана модель: {', '.join(participants_without_model)}"
        })

    # Живая проверка перед стартом: модели могли удалить, а Ollama - перезапустить
    models_status = check_models_available(
        [p.get("model", "") for p in cast], force=True
    )
    if not models_status["ok"]:
        problem = models_problem_message(models_status)
        print(f"⛔ Спектакль не начат: {problem}")
        # Сбрасываем running чтобы можно было попробовать снова
        session.running = False
        return jsonify({"success": False, "error": problem})

    print(f"🎭 Запускаем спектакль с {len(cast)} участниками")
    session.start_show(topic)
    print(f"🎭 Тема: {topic}")
    print(f"🎭 Состав: {[(p.get('display_name'), p.get('model')) for p in cast]}")

    thread = threading.Thread(target=run_debate_thread, args=(topic,))
    thread.daemon = True
    thread.start()
    return jsonify({"success": True, "session_id": session.session_id})

@app.route('/api/reset', methods=['POST'])
def reset():
    """«Новый спектакль»: новый состав, правила роли остаются."""
    session.new_show()
    print(f"🎭 Новый состав: {[p.get('display_name') for p in session.runtime_participants]}")
    return jsonify({"success": True, "participants": session.runtime_participants})

@app.route('/api/status')
def status():
    last_post_count = max(0, request.args.get("lastPostCount", 0, type=int))
    # Что сейчас лежит в памяти - для сайдбара (данные кэшируются на 1.5 с)
    loaded_models, loaded_models_error = fetch_loaded_models()
    
    response = jsonify({
        "running": session.running,
        "finished": session.finished,
        "session_id": session.session_id,
        "topic": session.topic,
        "new_posts": session.posts[last_post_count:],
        "total_posts": len(session.posts),
        "current_round": session.current_round,
        "current_participant": session.current_participant,
        "current_action": session.current_action,
        "search_query": session.search_query,
        "waiting_for_human": session.waiting_for_human,
        "current_participant_is_moderator": session.current_participant_is_moderator(),
        # Роль нужна интерфейсу, чтобы писать «Ход: Ирина · судья», а не просто имя
        "current_participant_role": session.current_participant_role(),
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
        # Роль нужна интерфейсу, чтобы писать «Ход: Ирина · судья», а не просто имя
        "current_participant_role": session.current_participant_role(),
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
        # Роль нужна интерфейсу, чтобы писать «Ход: Ирина · судья», а не просто имя
        "current_participant_role": session.current_participant_role(),
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
    """Возвращает текущие настройки редактора и индивидуальные инструкции участников"""
    # Личные инструкции лежат прямо в составе: переименование их не теряет
    participant_instructions = [
        {
            "name": participant.get("display_name", ""),
            "instruction": participant.get("instruction", ""),
            "is_judge": participant.get("is_judge", False),
        }
        for participant in session.runtime_participants
        if participant.get("model") != "human"
    ]

    response = jsonify({
        "static_instructions": session.static_instructions or DEFAULT_STATIC_INSTRUCTIONS,
        "moderator_messages": session.moderator_guidelines,
        "participant_instructions": participant_instructions,
        "judge_rules": session.judge_rules or DEFAULT_JUDGE_RULES,
        "default_judge_prompt": DEFAULT_JUDGE_INSTRUCTION or "",
        "default_static_instructions": DEFAULT_STATIC_INSTRUCTIONS,
    })
    # Редактор должен видеть живой состав и правила, а не ответ из кэша браузера
    response.headers["Cache-Control"] = "no-store"
    return response

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
    
    # Обновляем постоянные руководства модератора
    # (НЕ трогаем бусты в истории — они живут отдельно)
    if isinstance(data.get("moderator_messages"), list):
        session.moderator_guidelines = [
            str(m).strip() for m in data["moderator_messages"] if str(m).strip()
        ]
        print(f"📝 Обновлены руководства: {len(session.moderator_guidelines)} пунктов")
    
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
            if not name:
                continue
            for participant in session.runtime_participants:
                if participant.get("display_name") == name:
                    participant["instruction"] = instruction
                    break
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
