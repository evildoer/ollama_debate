"""Веб-сцена: Flask, Socket.IO, маршруты и запуск сервера.

Тонкий слой: маршруты читают состояние у сессии (show.session) и передают
работу дальше. Никакой логики спектакля здесь нет — только перевод запросов
браузера в вызовы модулей и обратно в JSON.
"""

import logging
import os
import threading
import time
import webbrowser

from flask import Flask, jsonify, render_template_string, request, send_from_directory
from flask_socketio import SocketIO, emit

from . import avatars
from . import ollama_api
from . import page
from . import settings
from . import show

# ============================================================
# FLASK APP + SOCKET.IO
# ============================================================

# Папки для браузера ищутся от корня проекта, а не от папки этого модуля:
# static/ и favicon.ico лежат рядом с ollama_debate_web.py, а не в aitheatre/
app = Flask(__name__,
            static_folder=str(settings.PROJECT_ROOT / "static"),
            static_url_path="/static")
app.config['SECRET_KEY'] = 'debate-secret-key-change-in-production'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


def publish_post(post: dict):
    """Новая реплика уходит в ленту сразу, не дожидаясь следующего опроса.

    Спектакль (show.py) про Socket.IO ничего не знает — он получает эту
    функцию колбэком.
    """
    socketio.emit('new_post', post)

# Отключаем логирование GET запросов к /api/status чтобы не засорять консоль
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)

# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    return render_template_string(page.HTML_TEMPLATE)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(settings.PROJECT_ROOT, 'favicon.ico',
                               mimetype='image/x-icon')

@app.route('/avatars/<path:filename>')
def serve_avatar(filename):
    return send_from_directory(settings.AVATAR_DIR, filename)

@app.route('/api/static_instructions')
def get_static_instructions():
    """Возвращает дефолтные статичные инструкции"""
    return jsonify({"static_instructions": settings.DEFAULT_STATIC_INSTRUCTIONS})

@app.route('/api/models')
def get_models():
    """Скачанные модели Ollama: ими заполняется список выбора модели в составе."""
    models, error = ollama_api.fetch_ollama_models(force=True)
    names = sorted(models.keys()) if models else []
    return jsonify({
        "models": names,
        # Кто из моделей умеет размышлять: интерфейс не даст включить это там, где нельзя
        "thinking_models": [n for n in names if ollama_api.model_supports_thinking(n)],
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
        error = show.apply_cast_patch(incoming)
        if error:
            print(f"⛔ Состав не изменён: {error}")
            return jsonify({"success": False, "error": error})
        print(f"🎭 Состав обновлён: {[p.get('display_name') for p in show.session.runtime_participants]}")
        return jsonify({"success": True, "participants": show.session.runtime_participants})

    models = [p.get("model", "") for p in show.session.runtime_participants]
    # Пустое поле участника значит «взять из OPTIONS». Чтобы это не приходилось
    # держать в голове, отдаём интерфейсу действующие значения и их источник.
    cast = []
    for p in show.session.runtime_participants:
        item = dict(p)
        if p.get("model") != "human":
            merged = ollama_api._merge_options(p)
            item["effective_options"] = {
                k: merged[k] for k in settings.PER_PARTICIPANT_OPTION_KEYS if merged.get(k) is not None
            }
            item["own_options"] = [k for k in settings.PER_PARTICIPANT_OPTION_KEYS if p.get(k) is not None]
            # Пока поле пустое, работают параметры из самого Modelfile модели
            item["model_defaults"] = ollama_api.fetch_model_parameters(p.get("model", ""))
        cast.append(item)
    return jsonify({
        "participants": cast,
        "option_keys": list(settings.PER_PARTICIPANT_OPTION_KEYS),
        # «Характер»: список и его числа держит сервер, чтобы пульт и случайный
        # розыгрыш на новый спектакль опирались на один и тот же набор
        "characters": {
            key: {"label": value["label"], "hint": value.get("hint", ""),
                  "group": value.get("group", "balanced"),
                  "think": value.get("think", "auto"),
                  "params": value.get("params") or {}}
            for key, value in settings.CHARACTER_PRESETS.items()
        },
        "randomize_characters": settings.RANDOMIZE_CHARACTERS,
        "running": show.session.running,
        "finished": show.session.finished,
        "topic": show.session.topic,
        # Сразу сообщаем интерфейсу, если нужных моделей нет в Ollama
        "models_status": ollama_api.check_models_available(models),
        # И если они не влезают в видеопамять при текущем num_ctx
        "vram_status": ollama_api.check_vram_fit(models),
    })

@app.route('/api/avatar/<keywords>')
def get_avatar(keywords):
    base_name = avatars.sanitize_avatar_name(keywords)
    for avatar_file in settings.AVATAR_DIR.glob(f"{base_name}*.jpg"):
        return jsonify({"avatar_url": f"/avatars/{avatar_file.name}"})
    return jsonify({"avatar_url": None})

@app.route('/api/avatar/<keywords>', methods=['POST'])
def refresh_avatar(keywords):
    print(f"🔍 Запрос аватара для: {keywords}")
    try:
        data = request.get_json(force=True, silent=True) or {}
        participant_idx = data.get('participant_idx')
        print(f"📋 Индекс участника: {participant_idx}")
        
        cast = show.session.runtime_participants
        if not isinstance(participant_idx, int) or not 0 <= participant_idx < len(cast):
            return jsonify({"error": "participant_idx вне состава"}), 400
        
        # generate_avatar_for_participant() работает только с именем и ключевыми словами
        participant = {
            "display_name": keywords,
            "avatar_keywords": keywords
        }
        
        # Кнопка «Найти аватар» должна искать заново, а не отдавать закэшированную ссылку
        avatars.AVATAR_URL_CACHE.pop(keywords, None)
        
        avatar_url = avatars.generate_avatar_for_participant(participant)
        
        # Аватар живёт в составе: его видит и следующий спектакль, и идущий
        cast[participant_idx]["avatar_url"] = avatar_url or None
        show.session.sync_cast_media()
        
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
    if show.session.running:
        return jsonify({"success": False, "error": "Уже запущено"})

    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "") or "").strip() or (show.session.topic or "").strip()
    if not topic:
        return jsonify({"success": False,
                        "error": "Тема не указана — напишите её в блоке «01 · Сюжет»"})
    show.session.topic = topic

    cast = show.session.runtime_participants
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
    models_status = ollama_api.check_models_available(
        [p.get("model", "") for p in cast], force=True
    )
    if not models_status["ok"]:
        problem = ollama_api.models_problem_message(models_status)
        print(f"⛔ Спектакль не начат: {problem}")
        # Сбрасываем running чтобы можно было попробовать снова
        show.session.running = False
        return jsonify({"success": False, "error": problem})

    print(f"🎭 Запускаем спектакль с {len(cast)} участниками")
    show.session.start_show(topic)
    print(f"🎭 Тема: {topic}")
    print(f"🎭 Состав: {[(p.get('display_name'), p.get('model')) for p in cast]}")

    thread = threading.Thread(target=show.run_debate_thread, args=(topic,),
                              kwargs={"on_post": publish_post})
    thread.daemon = True
    thread.start()
    # Тему возвращаем: интерфейс показывает в шапке именно то, с чем играем
    return jsonify({"success": True, "session_id": show.session.session_id, "topic": show.session.topic})

@app.route('/api/reset', methods=['POST'])
def reset():
    """«Новый спектакль»: новый состав, правила роли остаются."""
    show.session.new_show()
    print(f"🎭 Новый состав: {[p.get('display_name') for p in show.session.runtime_participants]}")
    return jsonify({"success": True, "participants": show.session.runtime_participants})

@app.route('/api/status')
def status():
    last_post_count = max(0, request.args.get("lastPostCount", 0, type=int))
    # Что сейчас лежит в памяти - для сайдбара (данные кэшируются на 1.5 с)
    loaded_models, loaded_models_error = ollama_api.fetch_loaded_models()
    
    response = jsonify({
        "running": show.session.running,
        "finished": show.session.finished,
        "session_id": show.session.session_id,
        "topic": show.session.topic,
        "new_posts": show.session.posts[last_post_count:],
        "total_posts": len(show.session.posts),
        "current_round": show.session.current_round,
        "current_participant": show.session.current_participant,
        "current_action": show.session.current_action,
        "search_query": show.session.search_query,
        "waiting_for_human": show.session.waiting_for_human,
        "current_participant_is_moderator": show.session.current_participant_is_moderator(),
        # Роль нужна интерфейсу, чтобы писать «Ход: Ирина · судья», а не просто имя
        "current_participant_role": show.session.current_participant_role(),
        "loaded_models": loaded_models if not loaded_models_error else [],
        "loaded_models_error": loaded_models_error or "",
        "gpu_memory": ollama_api.fetch_gpu_memory(),
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
        "running": show.session.running,
        "finished": show.session.finished,
        "topic": show.session.topic,
        "posts": show.session.posts,
        "total_posts": len(show.session.posts),
        "current_round": show.session.current_round,
        "current_participant": show.session.current_participant,
        "current_action": show.session.current_action,
        "search_query": show.session.search_query,
        "waiting_for_human": show.session.waiting_for_human,
        "current_participant_is_moderator": show.session.current_participant_is_moderator(),
        # Роль нужна интерфейсу, чтобы писать «Ход: Ирина · судья», а не просто имя
        "current_participant_role": show.session.current_participant_role(),
    })

@socketio.on('disconnect')
def handle_disconnect():
    print("🔌 Клиент отключился от WebSocket")

@socketio.on('request_status')
def handle_request_status():
    emit('status_update', {
        "running": show.session.running,
        "finished": show.session.finished,
        "current_round": show.session.current_round,
        "current_participant": show.session.current_participant,
        "current_action": show.session.current_action,
        "search_query": show.session.search_query,
        "waiting_for_human": show.session.waiting_for_human,
        "current_participant_is_moderator": show.session.current_participant_is_moderator(),
        # Роль нужна интерфейсу, чтобы писать «Ход: Ирина · судья», а не просто имя
        "current_participant_role": show.session.current_participant_role(),
    })

@app.route('/api/moderator/message', methods=['POST'])
def moderator_message():
    data = request.get_json(silent=True) or {}
    show.session.moderator_message = data.get("message", "") or ""
    return jsonify({"success": True})

@app.route('/api/moderator/topic', methods=['POST'])
def moderator_topic():
    """Смена темы на ходу режиссёра: следующие реплики строятся уже по ней."""
    data = request.get_json(silent=True) or {}
    new_topic = str(data.get("topic", "") or "").strip()
    if not new_topic:
        return jsonify({"success": False, "error": "Тема не указана"})
    
    show.session.topic = new_topic
    print(f"🎬 Режиссёр сменил тему: {new_topic}")
    return jsonify({"success": True, "topic": new_topic})

@app.route('/api/moderator/finish', methods=['POST'])
def moderator_finish():
    show.session.moderator_finished = True
    show.session.waiting_for_human = False
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
        for participant in show.session.runtime_participants
        if participant.get("model") != "human"
    ]

    response = jsonify({
        "static_instructions": show.session.static_instructions or settings.DEFAULT_STATIC_INSTRUCTIONS,
        "moderator_messages": show.session.moderator_guidelines,
        "participant_instructions": participant_instructions,
        "judge_rules": show.session.judge_rules or settings.DEFAULT_JUDGE_RULES,
        "default_judge_prompt": settings.DEFAULT_JUDGE_INSTRUCTION or "",
        "default_static_instructions": settings.DEFAULT_STATIC_INSTRUCTIONS,
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
        show.session.static_instructions = [str(instr) for instr in data["static_instructions"]]
        print(f"📝 Обновлены статичные инструкции: {len(show.session.static_instructions)} пунктов")
        for i, instr in enumerate(show.session.static_instructions, 1):
            print(f"   {i}. {instr}")
    
    # Обновляем постоянные руководства модератора
    # (НЕ трогаем бусты в истории — они живут отдельно)
    if isinstance(data.get("moderator_messages"), list):
        show.session.moderator_guidelines = [
            str(m).strip() for m in data["moderator_messages"] if str(m).strip()
        ]
        print(f"📝 Обновлены руководства: {len(show.session.moderator_guidelines)} пунктов")
    
    # Обновляем правила судьи если переданы
    if isinstance(data.get("judge_rules"), list):
        show.session.judge_rules = [str(instr) for instr in data["judge_rules"]]
        show.save_theatre_settings()
        print(f"⚖️  Обновлены правила судьи: {len(show.session.judge_rules)} пунктов")
    
    # Обновляем индивидуальные инструкции участников если переданы
    if isinstance(data.get("participant_instructions"), list):
        for p_instr in data["participant_instructions"]:
            if not isinstance(p_instr, dict):
                continue
            name = str(p_instr.get("name", "") or "")
            instruction = str(p_instr.get("instruction", "") or "")
            if not name:
                continue
            for participant in show.session.runtime_participants:
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
    if settings.UNLOAD_AFTER_DEBATE:
        unique_models = set(p.get("model", "") for p in show.session.runtime_participants
                            if p.get("model") and p.get("model") != "human")
        for model in unique_models:
            ollama_api.unload_model(model)
    
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
    print(f"Персонажей:  {len(settings.PARTICIPANTS)}")
    for i, p in enumerate(settings.PARTICIPANTS, 1):
        role = ""
        if p.get("is_moderator"):
            role = " (режиссёр)"
        elif p.get("is_judge"):
            role = " (судья)"
        print(f"  Персонаж {i}: модель {p['model']}{role}")
    
    # Проверяем, что модели реально скачаны: иначе спектакль упадёт уже на сцене
    ollama_api.report_models_status([p["model"] for p in settings.PARTICIPANTS])
    print(f"  ℹ️  Поддержка инструментов определяется автоматически при первом запросе")
    print(f"Размышления: {'ВКЛ' if settings.ENABLE_THINKING else 'ВЫКЛ'}")
    print(f"Поиск в инет: {'ВКЛ' if settings.ENABLE_SEARCH else 'ВЫКЛ'}")
    if settings.ENABLE_SEARCH:
        print(f"Мин. поисков: {settings.MIN_SEARCHES}")
    print(f"Грим и костюмы: {'ВКЛ' if settings.ENABLE_AVATAR_GENERATION else 'ВЫКЛ'}")
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
