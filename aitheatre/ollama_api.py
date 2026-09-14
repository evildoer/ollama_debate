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

# Кэш для хранения информации о поддержке tools моделями
MODELS_TOOLS_SUPPORT = {}  # {"model_name": True/False}

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


def ask_model_with_tools(model: str, messages: list, supports_tools: bool = True, tool_choice: str = None, options: dict = None, think=None) -> tuple:
    """
    Отправляет запрос к модели. Автоматически определяет поддержку tools.
    
    Parameters:
        tool_choice: "auto" (по умолчанию), "any" (обязан вызвать инструмент), 
                     или None (не использовать)
        think: True/False - явно включить/выключить размышления (см. resolve_think),
               None - не трогать режим модели
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
        MODELS_TOOLS_SUPPORT[model] = cloud.send_tools()
        return cloud.chat(model, messages, options=options, tool_choice=tool_choice)

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
        if settings.ENABLE_SEARCH and supports_tools and MODELS_TOOLS_SUPPORT.get(model, False):
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
            settings.OLLAMA_URL,
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
    if not deps.SEARCH_AVAILABLE:
        return "Поиск недоступен"
    
    print(f"  🔍 Поиск: '{query}'")
    
    results, last_error = search.ddgs_search("Поиск", "text", query, max_results)
    
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

def ask_model(model: str, messages: list, participant_name: str, options: dict = None,
              think=None, show_session=None) -> tuple:
    """
    Один ход модели: запрос к Ollama плюс поиск в интернете, если модель умеет
    вызывать инструменты.

    show_session — спектакль, который этот ход обслуживает. Нужен ровно для
    двух вещей: чтобы не искать новые факты после занавеса и чтобы сайдбар
    видел «Ищет: ...». Передаётся параметром, а не импортируется из show.py:
    иначе модуль общения с Ollama пришлось бы замкнуть сам на себя.
    """
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
        if show_session is not None and show_session.moderator_finished and content and content.strip():
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
            if search_count < settings.MIN_SEARCHES and forced_attempts < max_forced_attempts and MODELS_TOOLS_SUPPORT.get(model, False):
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
                
                if show_session is not None:
                    # Сайдбар показывает «Ищет: ...» именно из этих полей
                    show_session.current_action = "searching"
                    show_session.search_query = query
                search_queries.append(query)
                search_count += 1
                
                result = search_web(query, max_results)
                
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
                messages.append({
                    "role": "tool",
                    "tool_name": func_name,
                    "content": "[лимит поисков исчерпан]",
                    "name": func_name
                })
        
        if not has_search:
            break
    
    if (not content or not content.strip()) and not (
            show_session is not None and show_session.moderator_finished):
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
            # Через общий вход, а не своим запросом: так облачная модель тоже
            # получит последний шанс ответить (у неё размышлений в формате Ollama нет,
            # но пустой ответ бывает и по своим причинам)
            content, _tool_calls = ask_model_with_tools(model, messages, tool_choice=None,
                                                        options=options, think=False)
            if content and content.strip():
                return content, search_count, search_queries
        except Exception as e:
            print(f"  ⚠️  Ошибка финального запроса: {e}")
    
    return content or "[Модель не дала ответ]", search_count, search_queries

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
