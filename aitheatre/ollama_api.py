"""Всё общение с Ollama и оценка видеопамяти.

Раньше это было разбросано по большому файлу; вместе оно держится потому, что
для оценки «влезет ли модель в VRAM» нужны метаданные модели (число блоков и
головок внимания) из того же /api/show, что и для остального. Здесь же кэши
ответов: список моделей, загруженные в память, размеры из nvidia-smi.

Оценка VRAM — не догадка: пока модель ни разу не загружалась, вес берётся по
размеру файла, а после первого запуска — из измерений в .vram_cache.json.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from . import cloud
from . import deps
from . import search
from . import settings
from . import text
from . import tooltext

# Кэш для хранения информации о поддержке tools моделями
MODELS_TOOLS_SUPPORT = {}  # {"model_name": True/False}

# Ответы, которые репликой не считаются: ошибка запроса и «модель промолчала».
# Различать нужно затем, чтобы не гнать модель за поиском после неудачного хода:
# она не ответила не потому, что мало знает, а «поиск» после ошибки стоит ещё
# одного хода ожидания — у «думающих» моделей это ещё один оборванный ход.
_NOT_ANSWERS = ("[ОШИБКА:", "[Модель не дала ответ]")


def is_answer(text: str) -> bool:
    """Реплика ли это: не пусто, не ошибка и не молчание модели."""
    if not text or not text.strip():
        return False
    return not text.lstrip().startswith(_NOT_ANSWERS)


def takes_tools_now(model: str) -> bool:
    """Умеет ли модель инструменты — до первого запроса, а не после него.

    Спрашивать надо раньше, потому что от этого зависит сам первый запрос:
    можно сразу потребовать поиск (см. SEARCH_BEFORE_REPLY), а не ждать ответа,
    чтобы потом попросить заново. Кэш моделей тот же, поэтому лишнего запроса
    к Ollama не будет: /api/show спрашивается один раз на модель.
    """
    if not settings.ENABLE_SEARCH:
        return False
    if cloud.is_cloud_model(model):
        # Про облачные модели Ollama ничего не знает — там решает настройка
        return cloud.send_tools() and cloud.model_takes_tools(model)
    if model not in MODELS_TOOLS_SUPPORT:
        MODELS_TOOLS_SUPPORT[model] = check_model_tools_support(model)
    return bool(MODELS_TOOLS_SUPPORT.get(model))

# Кэш поддержки режима размышлений: {"model_name": True/False}.
# Размышлять умеют не все модели (Ollama сообщает это в capabilities, /api/show):
# у llama-моделей там только completion, у gemma4/qwen35 есть "thinking".
MODELS_THINKING_SUPPORT = {}

# Кэш списка скачанных моделей Ollama: {"at": monotonic, "models": {имя: размер}, "error": str}
_OLLAMA_MODELS_CACHE = {"at": 0.0, "models": {}, "error": None}

# Кэш моделей, загруженных в память: {"at": monotonic, "models": [...], "error": str}
_LOADED_MODELS_CACHE = {"at": 0.0, "models": [], "error": None}

# Кэш объёма видеопамяти (nvidia-smi): {"at": monotonic, "info": {total, used}}
_GPU_MEMORY_CACHE = {"at": 0.0, "info": {}}

# Метаданные моделей (/api/show): {имя: model_info}
_MODEL_INFO_CACHE = {}

# Параметры генерации из Modelfile модели: {имя: {"temperature": 0.8, ...}}
_MODEL_PARAMS_CACHE = {}

# Измеренные размеры моделей в памяти: {имя: {"ctx", "size", "size_vram"}}.
# Файл рядом с проектом, чтобы после перезапуска оценка была точной, а не только
# по размеру файла модели.
_VRAM_MEASUREMENTS = {}

# Чтобы не повторять одно и то же предупреждение на каждом опросе статуса
_RAM_SPILL_WARNED = set()

def model_supports_thinking(model: str) -> bool:
    """
    Умеет ли модель размышлять (скрытая часть ответа перед видимым текстом).
    Ollama перечисляет это в capabilities: если модели размышления чужды, запрос
    с think=true она отклонит, поэтому спрашиваем заранее.
    """
    if not model or model == "human":
        return False
    if cloud.is_cloud_model(model):
        # Спрашивать про размышления некого: /api/show — это Ollama, у которой
        # такой модели нет. А поле think мы на облако не отправляем: там у него
        # другой вид (и включать его — отдельная работа)
        return False
    if model in MODELS_THINKING_SUPPORT:
        return MODELS_THINKING_SUPPORT[model]
    
    try:
        req = urllib.request.Request(
            settings.OLLAMA_SHOW_URL,
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
    
    return None if settings.ENABLE_THINKING else False


def check_model_tools_support(model: str) -> bool:
    """
    Проверяет поддержку tools моделью через API /api/show.
    Возвращает True если модель поддерживает tools, False иначе.
    """
    try:
        data = {"name": model}
        req = urllib.request.Request(
            settings.OLLAMA_SHOW_URL,
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
    
    if not force and time.monotonic() - _OLLAMA_MODELS_CACHE["at"] < settings.MODELS_CACHE_TTL:
        return _OLLAMA_MODELS_CACHE["models"], _OLLAMA_MODELS_CACHE["error"]
    
    models, error = {}, None
    try:
        with urllib.request.urlopen(settings.OLLAMA_TAGS_URL, timeout=timeout) as response:
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
        error = f"Ollama недоступен по адресу {settings.OLLAMA_BASE_URL} ({e})"
    
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
    
    if not force and time.monotonic() - _LOADED_MODELS_CACHE["at"] < settings.LOADED_MODELS_CACHE_TTL:
        return _LOADED_MODELS_CACHE["models"], _LOADED_MODELS_CACHE["error"]
    
    models, error = [], None
    try:
        with urllib.request.urlopen(settings.OLLAMA_PS_URL, timeout=timeout) as response:
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
    
    if time.monotonic() - _GPU_MEMORY_CACHE["at"] < settings.GPU_MEMORY_CACHE_TTL:
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

    У облачной модели своего Modelfile нет: её значения по умолчанию живут на
    шлюзе, и спрашивать о них Ollama бессмысленно (и она ответит ошибкой).
    """
    if not model or model == "human" or cloud.is_cloud_model(model):
        return {}
    if model in _MODEL_PARAMS_CACHE:
        return _MODEL_PARAMS_CACHE[model]
    
    params = {}
    try:
        req = urllib.request.Request(
            settings.OLLAMA_SHOW_URL,
            data=json.dumps({"model": model}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = json.loads(response.read().decode('utf-8')).get("parameters") or ""
        for line in str(raw).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in settings.PER_PARTICIPANT_OPTION_KEYS:
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
            settings.OLLAMA_SHOW_URL,
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
    return 2 * attention_blocks * kv_heads * head_dim * settings.KV_CACHE_BYTES_PER_ELEM

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
        if settings.VRAM_MEASUREMENTS_FILE.exists():
            data = json.loads(settings.VRAM_MEASUREMENTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _VRAM_MEASUREMENTS = {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        print(f"  ⚠️  Не читается {settings.VRAM_MEASUREMENTS_FILE.name}: {e}")

def save_vram_measurements():
    try:
        settings.VRAM_MEASUREMENTS_FILE.write_text(
            json.dumps(_VRAM_MEASUREMENTS, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Не сохраняется {settings.VRAM_MEASUREMENTS_FILE.name}: {e}")

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
    if room < per_token * settings.VRAM_SAFE_MIN_CTX:
        return None, 0
    
    safe = min(num_ctx, room // per_token)
    safe = (safe // settings.VRAM_SAFE_CTX_STEP) * settings.VRAM_SAFE_CTX_STEP
    if safe < settings.VRAM_SAFE_MIN_CTX:
        return None, 0
    
    return safe, base + per_token * safe

def check_vram_fit(models: list, num_ctx: int = None) -> dict:
    """
    Проверяет, влезут ли модели спектакля в видеопамять при текущем num_ctx.
    Ничего не грузит: только /api/show, /api/tags и уже измеренные размеры.
    """
    num_ctx = as_int(num_ctx) or as_int(settings.OPTIONS.get("num_ctx")) or 4096
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
    # Спектакль из одних облачных моделей видеопамяти не касается: незачем
    # ругаться и на видеокарту, которой может не быть, и на Ollama, которую для
    # такого спектакля вообще можно не запускать
    local_models = [m for m in (models or [])
                    if m and m != "human" and not cloud.is_cloud_model(m)]
    if not local_models:
        result["checked"] = True
        return result

    if not result["gpu_total"]:
        result["error"] = "объём видеопамяти неизвестен (nvidia-smi не отвечает)"
        return result

    available, models_error = fetch_ollama_models()
    if models_error:
        result["error"] = models_error
        return result
    
    result["checked"] = True
    result["budget"] = int(result["gpu_total"] * settings.VRAM_SAFETY_FACTOR)
    
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
    if not settings.UNLOAD_OTHER_MODELS:
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

    # Местные модели ищутся в Ollama, облачные — на шлюзе: искать облачную
    # в /api/tags значило бы объявить «модель не скачана» той, которую и не надо
    # скачивать
    local = [m for m in required if not cloud.is_cloud_model(m)]
    remote = [m for m in required if cloud.is_cloud_model(m)]
    missing = []

    if local:
        available, error = fetch_ollama_models(force=force)
        if error:
            return {"ok": False, "missing": list(local), "error": error}
        missing.extend(m for m in local if not model_is_installed(m, available))

    if remote:
        if not cloud.is_configured():
            return {"ok": False, "missing": list(remote),
                    "error": ("для облачных участников нужен ключ шлюза: впишите его в "
                              f"settings.CLOUD_API_KEY или положите в переменную "
                              f"{settings.CLOUD_KEY_ENV}")}
        # Список моделей шлюза кэшируется, поэтому проверка ничего не стоит
        names, cloud_error = cloud.fetch_models()
        if cloud_error:
            return {"ok": False, "missing": list(remote),
                    "error": f"облачные модели: {cloud_error}"}
        known = {cloud.cloud_model_id(n) for n in names}
        if known:
            # Пустой список — это «шлюз ничего не отдал», а не «все модели пропали»
            missing.extend(m for m in remote if m not in known)

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
    """Человекочитаемое объяснение, почему спектакль нельзя начать.

    Местные и облачные причины разделены: «проверьте, что Ollama запущена» —
    бесполезный совет тому, у кого не вписан ключ шлюза, а «скачайте модель» —
    тому, чья модель живёт в интернете.
    """
    missing = status.get("missing") or []
    local_missing = [m for m in missing if not cloud.is_cloud_model(m)]
    cloud_missing = [m for m in missing if cloud.is_cloud_model(m)]

    lines = []
    if status.get("error"):
        lines.append(status["error"])
        if not cloud_missing or local_missing:
            lines[-1] += ". Проверьте, что Ollama запущена."

    if local_missing:
        pulls = " ; ".join(f"ollama pull {m}" for m in local_missing)
        lines.append(f"В Ollama нет моделей: {', '.join(local_missing)}. Скачайте их: {pulls}")

    if cloud_missing:
        lines.append("На облачном шлюзе нет моделей: " + ", ".join(cloud_missing)
                     + ". Сверьтесь с каталогом моделей шлюза.")

    return " ".join(lines)


def _merge_options(participant: dict) -> dict:
    """
    Собирает options для запроса в Ollama: глобальные OPTIONS +
    персональные настройки участника из PARTICIPANTS.
    """
    opts = dict(settings.OPTIONS)
    for key in settings.PER_PARTICIPANT_OPTION_KEYS:
        if participant.get(key) is not None:
            opts[key] = participant[key]
    return opts


def ask_model_with_tools(model: str, messages: list, supports_tools: bool = True, tool_choice: str = None, options: dict = None, think=None, on_delta=None, on_thought=None, tools=None, report: dict = None) -> tuple:
    """
    Отправляет запрос к модели. Автоматически определяет поддержку tools.
    
    Parameters:
        tool_choice: "auto" (по умолчанию), "any" (обязан вызвать инструмент), 
                     или None (не использовать)
        think: True/False - явно включить/выключить размышления (см. resolve_think),
               None - не трогать режим модели
        tools: False - на этот запрос инструмент не отправлять (см. последнюю
               попытку хода в ask_model); None - как решат настройки модели
        report: пустой словарь, который облачный путь наполнит рассказом о ходе
               (чем ответ кончился, сколько ушло в размышления)
    """
    # Облачная модель играет не в Ollama: ход уходит на шлюз, в формате OpenAI.
    # Возвращаемая форма та же (текст, вызовы инструментов), поэтому весь цикл
    # поиска в интернете и разбор ошибок в ask_model остаются нетронутыми
    if cloud.is_cloud_model(model):
        # Инструменты облачные модели умеют, а спросить про это не у кого:
        # /api/show спрашивает Ollama, у которой такой модели нет. Метка — та же,
        # что и у местных, и она разрешает принудительный поиск в ask_model
        # (он смотрит в тот же кэш). Но только когда инструмент правда отправляется:
        # иначе мы бы требовали поиск у шлюза, которого он не получал, — и получали
        # ошибку на каждом ходу вместо обычной реплики
        MODELS_TOOLS_SUPPORT[model] = cloud.send_tools() and cloud.model_takes_tools(model)
        # on_delta и on_thought живут только здесь: к Ollama они не относятся
        # (см. ask_model)
        return cloud.chat(model, messages, options=options, tool_choice=tool_choice,
                          on_delta=on_delta, on_thought=on_thought, use_tools=tools,
                          report=report)

    # Проверяем кэш поддержки tools
    if model not in MODELS_TOOLS_SUPPORT:
        # Первый запрос - проверяем через /api/show
        if settings.ENABLE_SEARCH and supports_tools:
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
            "options": options or settings.OPTIONS,
            "stream": False
        }
        if think is not None:
            data["think"] = think
        elif not settings.ENABLE_THINKING:
            data["think"] = False
        
        # Добавляем tools только если модель их поддерживает
        if tools is not False and settings.ENABLE_SEARCH and supports_tools and MODELS_TOOLS_SUPPORT.get(model, False):
            data["tools"] = [{
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": (f"Ищет информацию в интернете — до "
                                    f"{max(1, _as_int(settings.SEARCH_MAX_RESULTS))} "
                                    f"результатов на запрос"),
                    "parameters": {
                        "type": "object",
                        "required": ["query"],
                        "properties": {
                            "query": {"type": "string", "description": "Поисковый запрос"},
                            "max_results": {"type": "integer"}
                        }
                    }
                }
            }]
            
            # Добавляем tool_choice если указан
            if tool_choice:
                data["tool_choice"] = tool_choice
        
        # Местный запрос тоже попадает в хронологию со своими метками времени:
        # у судьи на облаке и у судьи на Ollama ход должен читаться одинаково,
        # и у обоих видно, что запрос ушёл и сколько в нём уехало
        cloud.journal_ask_start(report,
                                tokens_in_est=cloud.messages_tokens(data.get("messages")),
                                tools=bool(data.get("tools")),
                                messages=len(data.get("messages") or []))
        req = urllib.request.Request(
            settings.OLLAMA_URL,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
            msg = result.get("message") or {}
            # Размышления местной модели — в ту же хронологию, и раньше итога
            # запроса: они случились по дороге к ответу
            if msg.get("thinking"):
                cloud.journal_thought(report, str(msg["thinking"]), replace=True)
            # Ollama считает токены сама (prompt_eval_count / eval_count): без этого
            # у местных моделей в журнале хода не было бы ни одного числа, а у облачных
            # они есть — и эта разница выгладела бы необъяснимой
            cloud.journal_ask(report, {
                "tokens_in": _as_int(result.get("prompt_eval_count")),
                "tokens_out": _as_int(result.get("eval_count")),
                "finish_reason": str(result.get("done_reason") or ""),
            })
            if "message" in result:
                return msg.get("content", "") or "", msg.get("tool_calls") or []
            return "", []
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else "Нет деталей ошибки"
        print(f"  ⚠️  HTTP ошибка {e.code}: {e.reason}")
        print(f"  📋 Детали: {error_body}")
        cloud.journal_ask(report, {"error": f"HTTP {e.code}: {e.reason}"})
        return f"[ОШИБКА: HTTP {e.code}]", []
    except Exception as e:
        print(f"  ⚠️  Ошибка запроса к модели: {e}")
        cloud.journal_ask(report, {"error": str(e)})
        return f"[ОШИБКА: {e}]", []

def search_web(query: str, max_results: int = None) -> str:
    """Поиск в интернете — столько результатов, сколько разрешено настройкой.

    Модель может попросить в вызове больше, чем стоит в SEARCH_MAX_RESULTS: сверх
    настроек не ходим. Больше находок — не больше пользы, а больше входных
    токенов, и за них платит режиссёр (найденные страницы едут к модели сверх
    истории — см. снимок хода в show.build_turn_report).
    """
    if not deps.SEARCH_AVAILABLE:
        return "Поиск недоступен"

    limit = max(1, _as_int(settings.SEARCH_MAX_RESULTS))
    asked = min(_as_int(max_results) or limit, limit)
    print(f"  🔍 Поиск: '{query}' (до {asked} результатов)")
    
    results, last_error = search.ddgs_search("Поиск", "text", query, asked)
    
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

def _as_int(value) -> int:
    """Число из ответа вендора — или 0, если его там нет: пустое место не 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def journal_search(report: dict, index: int, query: str, results: str,
                   limit: int, started: float = None) -> None:
    """Дописать в журнал хода событие о поиске — с запросом и всем принесённым.

    В ленте результаты поиска и раньше виднелись за «Источники», а вот самого
    запроса там не было — как и связи между «нашлось вот это» и «модель сказала
    вот так». Теперь это одна хронология: строки хода идут по порядку, и у каждой
    видно и вопрос, и ответ на него, и сколько каждый весил (см.
    show.build_turn_report).
    """
    if report is None:
        return
    results = results or ""
    cloud.journal_push(report, {
        "kind": "search", "n": int(index), "query": query,
        "limit": int(limit), "results": results,
        # Вес найденного — как и у всего остального в журнале: найденное уезжает
        # к модели сверх истории и платится входными токенами
        "tokens": text.estimate_tokens(results),
        "t": time.time() if started is None else float(started),
        "t_end": time.time(),
    })


def journal_note(report: dict, text: str, kind: str = "note") -> None:
    """Дописать в журнал хода событие-объяснение: что сделал код и почему."""
    if report is None or not str(text or "").strip():
        return
    cloud.journal_push(report, {"kind": kind, "text": str(text), "t": time.time()})


def silence_reason(report: dict = None, asked_for_search: bool = False) -> str:
    """Почему ход кончился молчанием — словами, для лога и для ленты.

    «Модель не дала ответ» ничего не говорит ни зрителю, ни судье, ни тому, кто
    разбирается, что крутить в настройках: у молчания три разных причины, и они
    лечатся по-разному. Предел вывода (finish_reason=length), съеденный
    размышлениями, лечится большим пределом или меньшей задумчивостью; модель,
    которая на каждом запросе просит ещё поиск, не успокоится, пока поиск не
    закончится; а бывает и так, что вендор посчитал токены и не прислал текста
    вовсе — это уже его дело, и здесь его не выдумать.

    Живёт здесь, а не в cloud.py: облачный путь только собирает числа
    (см. cloud._turn_notes), а говорить о них словами — дело хода.
    """
    report = report or {}
    finish = str(report.get("finish_reason") or "")
    thoughts = int(report.get("reasoning_tokens") or 0)
    written = int(report.get("tokens_out") or 0)

    if finish == "length" and thoughts:
        return f"весь предел вывода ушёл в размышления ({thoughts} токенов)"
    if finish == "length":
        return "ответ оборван по пределу вывода"
    if asked_for_search:
        return "вместо ответа модель просила ещё поиск"
    if thoughts:
        return f"модель только размышляла ({thoughts} токенов) и вслух ничего не сказала"
    if written:
        return f"шлюз насчитал {written} токенов вывода, но текста не прислал"
    return "шлюз не прислал текста"


def search_limits() -> tuple:
    """Числа поиска из настроек: (минимум, максимум, результатов на запрос, попытки).

    Живут в одном месте и читаются здесь: остальной код о поиске ничего не решает,
    а берёт готовое. Максимум — всегда хотя бы один: поиск, который разрешён
    «ноль раз», — это выключенный поиск, а для этого есть ENABLE_SEARCH (см.
    settings.SEARCH_MAX_RESULTS).
    """
    return (max(0, _as_int(settings.MIN_SEARCHES)),
            max(1, _as_int(settings.MAX_SEARCHES)),
            max(1, _as_int(settings.SEARCH_MAX_RESULTS)),
            max(0, _as_int(settings.MAX_SEARCH_ATTEMPTS)))


def ask_model(model: str, messages: list, participant_name: str, options: dict = None,
              think=None, show_session=None, on_delta=None, on_thought=None,
              report: dict = None) -> tuple:
    """
    Один ход модели: запрос к Ollama плюс поиск в интернете, если модель умеет
    вызывать инструменты.

    show_session — спектакль, который этот ход обслуживает. Нужен ровно для
    двух вещей: чтобы не искать новые факты после занавеса и чтобы сайдбар
    видел «Ищет: ...». Передаётся параметром, а не импортируется из show.py:
    иначе модуль общения с Ollama пришлось бы замкнуть сам на себя.

    on_delta и on_thought — получатели ответа по кускам (только облачные модели,
    только когда включён CLOUD_STREAM): лента показывает реплику, пока она
    пишется, и размышления модели — пока они думаются. На готовый ответ это
    не влияет — возвращается всё равно целый текст.

    report — журнал хода: сюда ляжет всё, что происходило по порядку (запросы
    с их входом и выводом, поиски с формулировкой и найденным, отказы). Свой
    словарь приходит от вызывающего (см. show.handle_ai_turn): по нему потом
    собирается отчёт и у реплики в ленте, и в ДАМПе спектакля.
    """
    search_queries = []
    search_count = 0
    # Сколько поисков требуем, сколько разрешаем и сколько результатов приносим —
    # из настроек, а не из чисел в коде: это те ручки, которые режиссёр крутит
    # сам (см. settings, группа «ПОИСК В ИНТЕРНЕТЕ»)
    min_searches, max_searches, max_results_setting, max_forced_attempts = search_limits()
    # Кругов хода должно хватать на всё, что разрешено настройками: требуемые
    # поиски, все разрешённые, попытки «поищи, пожалуйста» и финальный ответ.
    # Иначе потолок поисков молча упирался бы в потолок кругов: режиссёр поднял
    # MAX_SEARCHES, а ход кончился бы на том же месте, и почему — не понять
    max_iterations = max(8, min_searches + max_searches + max_forced_attempts + 2)
    force_tool_use = False  # Флаг для принудительного использования инструмента через tool_choice
    forced_attempts = 0     # Счётчик попыток принудительного поиска
    content = ""
    # Журнал хода — тот же, в который пишет и облачный путь (см. cloud.journal_ask):
    # у хода должен быть один рассказ, а не два, иначе «откуда это число» опять
    # придётся собирать по кускам
    report = report if report is not None else {}
    refused_search = False  # модель просила ещё поиск, а мы уже отказали

    # Размышления — такое же событие хронологии, как запросы и поиски: по ним
    # видно, чем модель занималась вместо ответа. Собираем их здесь, по дороге
    # в ленту: черновик показывает только хвост, а в ДАМПе нужно всё
    def take_thought(piece, replace=False):
        cloud.journal_thought(report, piece, replace)
        if on_thought is not None:
            on_thought(piece, replace)
    
    # Вычисляем нормализованное имя один раз
    participant_name_normalized = participant_name.lower().replace(" ", "_")
    
    for iteration in range(max_iterations):
        # Режиссёр завершил спектакль - не тратим время на новые поиски
        if show_session is not None and show_session.moderator_finished and content and content.strip():
            return content, search_count, search_queries
        
        # Если нужен принудительный поиск - передаём tool_choice="any".
        # И то же самое делаем на самом первом запросе хода: искать надо
        # до того, как сказано слово, а не после — иначе первая версия реплики
        # рождается только для того, чтобы быть отброшенной (её токены оплачены,
        # а в ленте она мелькала и исчезала; см. SEARCH_BEFORE_REPLY)
        require_search = force_tool_use or (
            iteration == 0 and settings.SEARCH_BEFORE_REPLY
            and search_count < min_searches and takes_tools_now(model))
        current_tool_choice = "any" if require_search else None
        
        content, tool_calls = ask_model_with_tools(model, messages, tool_choice=current_tool_choice,
                                                   options=options, think=think, on_delta=on_delta,
                                                   on_thought=take_thought, report=report)
        tool_calls = tool_calls or []

        # Модель может попросить поиск не протоколом, а словами: напечатать
        # «search:web_search{query: "..."}» прямо в реплике. Для нас это была
        # обычная реплика — сырой вызов уезжал в пост, а поиска не было вовсе
        # (и «Источников» в ленте тоже). Теперь просьба узнаётся и выполняется
        # (см. tooltext), а из реплики вызов убирается
        content, text_queries = tooltext.take_calls(content)
        if text_queries and not tool_calls:
            print(f"  🔍 {participant_name}: поиск попросили текстом — выполняю: "
                  + ", ".join(f"«{query}»" for query in text_queries))
            tool_calls = [{"id": "", "type": "function", "textual": True,
                           "function": {"name": "search_web",
                                        "arguments": {"query": query}}}
                          for query in text_queries]
        
        # Сбрасываем флаг после использования
        if force_tool_use:
            force_tool_use = False
            forced_attempts += 1
        
        if not tool_calls and is_answer(content):
            # Принудительный поиск — только у тех, кто правда принимает инструмент,
            # и спрашиваем об этом заново, а не по кэшу: облачная модель могла
            # отказаться от инструмента на этом самом ходу, и требовать у неё
            # поиск — значит гонять её впустую (в логе это выглядело так: шлюз
            # сказал «не принимаю инструмент», а приложение тут же попросило поиск,
            # и «думающая» модель ушла в новый оборванный ход ожидания)
            if (search_count < min_searches and forced_attempts < max_forced_attempts
                    and takes_tools_now(model)):
                print(f"  🔍 Принудительный поиск (попытка {forced_attempts + 1}/{max_forced_attempts})...")
                journal_note(report, f"модель ответила без поиска — прошу поиск "
                                     f"(попытка {forced_attempts + 1} из {max_forced_attempts})",
                             kind="force")
                messages.append({"role": "assistant", "content": content, "name": participant_name_normalized})
                messages.append({
                    "role": "user",
                    "content": "Используй инструмент поиска для получения актуальной информации.",
                    "name": "system"
                })
                force_tool_use = True  # В следующий раз требуем инструмент через API
                continue
            return content, search_count, search_queries
        
        if not tool_calls:
            break
        
        # Вызов, пришедший текстом, отправляем обратно текстом же: протокол
        # инструментов эта модель не удержала (потому вызов и оказался в реплике),
        # и ответ инструмента на выдуманный нами id она не поймёт
        if all(tc.get("textual") for tc in tool_calls):
            messages.append({
                "role": "assistant",
                "content": content or "",
                "name": participant_name_normalized
            })
        else:
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
                    max_results = int(func_args.get("max_results", max_results_setting))
                except (TypeError, ValueError):
                    max_results = max_results_setting
                
                if show_session is not None:
                    # Сайдбар показывает «Ищет: ...» именно из этих полей
                    show_session.current_action = "searching"
                    show_session.search_query = query
                search_queries.append(query)
                search_count += 1
                
                started = time.time()
                result = search_web(query, max_results)
                # В журнал — вместе с формулировкой запроса: в ленте «Источники»
                # показывают её отдельно, а в ДАМПе нужно, чтобы вопрос и то, что
                # по нему нашлось, лежали рядом — со временем и весом
                journal_search(report, search_count, query, result, max_searches,
                               started=started)
                
                if tc.get("textual"):
                    # Найденное — обычным сообщением: с этой моделью мы говорим
                    # на её языке, а не на языке протокола (см. выше)
                    messages.append({
                        "role": "user",
                        "content": f"Результаты поиска по запросу «{query}»:\n\n{result}",
                        "name": "system"
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_name": "search_web",
                        "content": result,
                        "name": "search_web"
                    })
                
                if show_session is not None:
                    show_session.current_action = "thinking"
                    show_session.search_query = None
                has_search = True
            else:
                if func_name == "search_web":
                    # Молчать об этом нельзя: со стороны это выглядит как модель,
                    # которая «думает» и ничего не говорит, — а на самом деле она
                    # в четвёртый раз просит поиск, и ей тихо отказали
                    refused_search = True
                    asked = str(func_args.get("query", "") or "")
                    print(f"  🔍 {participant_name}: просит ещё поиск "
                          f"(«{asked}»), но лимит ({max_searches}) исчерпан — "
                          f"досказать придётся словами")
                    journal_note(report, f"просит ещё поиск «{asked}», но лимит "
                                         f"{max_searches} исчерпан — досказать придётся словами",
                                 kind="refused")
                messages.append({
                    "role": "tool",
                    "tool_name": func_name,
                    "content": "[лимит поисков исчерпан]",
                    "name": func_name
                })
        
        if not has_search:
            break
    
    reason = silence_reason(report, asked_for_search=refused_search)

    if (not content or not content.strip()) and not (
            show_session is not None and show_session.moderator_finished):
        # Молчание хода объясняем словами: «пустой ответ» без причины не говорит
        # ничего ни зрителю, ни тому, кто потом разбирается в настройках
        # (см. silence_reason)
        print(f"  ⚠️  {participant_name}: пустой ответ — {reason}. "
              f"Прошу финальный ответ ещё раз, теперь без поиска")
        journal_note(report, f"пустой ответ: {reason} — прошу финальный ответ без поиска",
                     kind="silence")
        messages.append({
            "role": "user",
            # Модель, застрявшая на просьбах поискать, должна узнать, что искать
            # больше нечего: иначе последняя попытка только повторит первую
            "content": "Поиска больше не будет. Скажи свой финальный ответ "
                       "обычным текстом, на русском языке.",
            "name": "system"
        })
        
        try:
            # Через общий вход, а не своим запросом: так облачная модель тоже
            # получит последний шанс ответить (у неё размышлений в формате Ollama нет,
            # но пустой ответ бывает и по своим причинам).
            # Инструмент этой попытке не отправляется: у местной модели он был бы
            # ещё одним кругом, а у облачной — поводом попросить поиск в четвёртый
            # раз, как это и случилось у gpt-5-nano (см. tools в ask_model_with_tools)
            content, _tool_calls = ask_model_with_tools(model, messages, tool_choice=None,
                                                        options=options, think=False, tools=False,
                                                        on_delta=on_delta, on_thought=take_thought,
                                                        report=report)
            # И тут просьба о поиске может прийти словами: репликой её считать
            # нельзя, а выполнять уже нечего — ход кончается (см. tooltext)
            content = tooltext.take_calls(content)[0]
            if content and content.strip():
                journal_note(report, "финальная попытка удалась: поиск не понадобился",
                             kind="silence")
                return content, search_count, search_queries
        except Exception as e:
            print(f"  ⚠️  Ошибка финального запроса: {e}")
            journal_note(report, f"финальный запрос не прошёл: {e}", kind="silence")
    
    # Молчание без объяснения — потерянная улика: в ленте и в ДАМПе должно
    # остаться, ПОЧЕМУ модель не сказала ни слова
    return content or f"[Модель не дала ответ] — {reason}", search_count, search_queries

def unload_model(model: str):
    try:
        data = {"model": model, "keep_alive": 0}
        req = urllib.request.Request(
            settings.OLLAMA_GENERATE_URL,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=30)
    except:
        pass
