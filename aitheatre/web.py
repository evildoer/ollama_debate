"""Веб-сцена: Flask, Socket.IO, маршруты и запуск сервера.

Тонкий слой: маршруты читают состояние у сессии (show.session) и передают
работу дальше. Никакой логики спектакля здесь нет — только перевод запросов
браузера в вызовы модулей и обратно в JSON.
"""

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from flask import Flask, jsonify, render_template_string, request, send_from_directory
from flask_socketio import SocketIO, emit

from . import avatars
from . import cloud
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


def publish_draft(draft: dict):
    """Реплика, которая пишется прямо сейчас: черновик поста.

    Настоящего поста ещё нет — он придёт, когда ход кончится. Черновик живёт
    только на этой стороне, у ленты, и только по Socket.IO: опрос носит готовые
    посты, и черновик в него не попадает (он и не должен — он ещё не реплика).
    """
    socketio.emit('stream_post', draft)


# Как часто сервер сам говорит браузеру, что происходит. Сокет — основной
# канал: опрос раз в три секунды заменён этим, и он свежее (видно «Ищет: …»
# почти сразу). Страница продолжает спрашивать статус сама, но редко — это
# подстраховка на случай, когда сокет отвалился, а не рабочий ход
STATUS_PUSH_INTERVAL = 1.0


def status_payload(last_post_count: int = 0, with_posts: bool = True) -> dict:
    """Состояние спектакля — одно и то же для опроса и для сокета.

    Раньше этот словарь собирался в двух местах (в маршруте и в событиях),
    и они уже успели разойтись: поле, добавленное в одно, не появлялось
    в другом. Теперь источник один.

    with_posts=False — для рассылки по сокету: реплики там ходят событиями
    new_post, и повторять их в состоянии раз в секунду незачем.
    """
    # Что сейчас лежит в памяти - для сайдбара (данные кэшируются на 1.5 с)
    loaded_models, loaded_models_error = ollama_api.fetch_loaded_models()
    payload = {
        "running": show.session.running,
        "finished": show.session.finished,
        "session_id": show.session.session_id,
        "topic": show.session.topic,
        "new_posts": show.session.posts[max(0, last_post_count):] if with_posts else [],
        "total_posts": len(show.session.posts),
        # Пришли ли вместе с состоянием сами реплики. Нужно странице: в рассылке
        # по сокету их нет, и по счётчику она не должна решить, что уже всё
        # видела — иначе пост, пришедший событием, был бы потерян
        "posts_included": with_posts,
        "current_round": show.session.current_round,
        "current_participant": show.session.current_participant,
        "current_action": show.session.current_action,
        "search_query": show.session.search_query,
        # Часы хода: сколько уже думает говорящий и сколько ему осталось.
        # Именно секунды «сколько прошло / сколько осталось», а не момент
        # времени: часы у браузера и у сервера разные, и «до 05:41» у зрителя
        # значило бы гадание (см. show.start_turn_clock)
        "turn_elapsed": (None if show.session.turn_elapsed() is None
                         else round(show.session.turn_elapsed(), 1)),
        "turn_left": (None if show.session.turn_left() is None
                      else round(show.session.turn_left(), 1)),
        "turn_limit": show.session.turn_limit,
        "turn_extra": show.session.turn_extra,
        # Сколько стоил спектакль: сумма разниц остатка на ключе. Считает её
        # ход (см. show._note_money), а не страница: тарифов не знает никто,
        # а остаток знает только шлюз
        "spent": round(float(show.session.spent or 0.0), 2),
        "waiting_for_human": show.session.waiting_for_human,
        "current_participant_is_moderator": show.session.current_participant_is_moderator(),
        # Роль нужна интерфейсу, чтобы писать «Ход: Ирина · судья», а не просто имя
        "current_participant_role": show.session.current_participant_role(),
        "loaded_models": loaded_models if not loaded_models_error else [],
        "loaded_models_error": loaded_models_error or "",
        "gpu_memory": ollama_api.fetch_gpu_memory(),
    }
    # Для внешних клиентов (и для события подключения) — те же поля, но реплики целиком
    if with_posts:
        payload["posts"] = show.session.posts
    return payload


def publish_status():
    """Состояние спектакля в браузер — по сокету, без реплик (они ходят событиями)."""
    socketio.emit('status_update', status_payload(with_posts=False))


# Идёт ли уже рассылка состояния: её просят и старт спектакля, и подключение
# страницы, а ткач должен быть один
_status_pusher = {"alive": False}


def start_status_pusher():
    """Гонит состояние в браузер, пока идёт спектакль.

    Живёт ровно столько, сколько длится спектакль: без запущенного спектакля
    говорить нечего, а лишний поток будил бы процессор впустую. Второй ткач
    не заводится (их может позвать и старт, и новое подключение).
    """
    if _status_pusher["alive"]:
        return
    _status_pusher["alive"] = True

    def pump():
        try:
            while show.session.running:
                time.sleep(STATUS_PUSH_INTERVAL)
                publish_status()
            # Последнее состояние после занавеса: панель должна узнать, что всё,
            # не дожидаясь редкой подстраховки
            publish_status()
        except Exception as e:      # поток не должен рушить сервер молча
            print(f"  ⚠️  Рассылка состояния остановлена: {e}")
        finally:
            _status_pusher["alive"] = False

    threading.Thread(target=pump, daemon=True).start()


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
    """Скачанные модели Ollama и доступные облачные: ими заполняется список выбора."""
    models, error = ollama_api.fetch_ollama_models(force=True)
    names = sorted(models.keys()) if models else []
    # Облачные идут отдельным списком: в пульте они будут своей группой, чтобы
    # было видно, куда уйдёт реплика — в свой компьютер или в интернет
    cloud_state = cloud.status()
    return jsonify({
        "models": names,
        # Кто из моделей умеет размышлять: интерфейс не даст включить это там, где нельзя
        "thinking_models": [n for n in names if ollama_api.model_supports_thinking(n)],
        "cloud_models": [cloud.cloud_model_id(n) for n in cloud_state["models"]],
        "cloud": {"configured": cloud_state["configured"],
                  "base_url": cloud_state["base_url"],
                  "error": cloud_state["error"]},
        "error": error or "",
    })

def models_status_payload(models: list, force: bool = False) -> dict:
    """Статус моделей вместе с готовым объяснением.

    Причины бывают разные — модели нет в Ollama, нет ключа облачного шлюза,
    модели нет на шлюзе, — и собирать текст из полей дважды (на сервере и на
    странице) значит рано или поздно разойтись в объяснениях. Страница просто
    показывает то, что пришло.
    """
    status = ollama_api.check_models_available(models, force=force)
    status["message"] = ollama_api.models_problem_message(status)
    return status


def cast_payload() -> list:
    """
    Состав для пульта: сам участник, его роль и действующие числа.

    Отдаётся и на GET, и в ответе на правку состава. Пустое поле участника значит
    «взять из OPTIONS», поэтому вместе с числами уходит и их источник: иначе это
    пришлось бы держать в голове, а строка «Уйдёт в модель» после «Применить
    состав» на миг показывала бы прочерки.
    """
    cast = []
    for p in show.session.runtime_participants:
        item = dict(p)
        # Роль одним словом: странице так проще, чем два флага
        item["role"] = show.cast_role(p)
        if p.get("model") != "human":
            merged = ollama_api._merge_options(p)
            item["effective_options"] = {
                k: merged[k] for k in settings.PER_PARTICIPANT_OPTION_KEYS if merged.get(k) is not None
            }
            item["own_options"] = [k for k in settings.PER_PARTICIPANT_OPTION_KEYS if p.get(k) is not None]
            # Пока поле пустое, работают параметры из самого Modelfile модели
            item["model_defaults"] = ollama_api.fetch_model_parameters(p.get("model", ""))
        cast.append(item)
    return cast


@app.route('/api/participants', methods=['GET', 'POST'])
def participants():
    """
    GET  - состав спектакля (пока он не начат – заготовка) плюс проверки моделей
           и видеопамяти. Имена больше не генерируются на каждый запрос, поэтому
           перезагрузка страницы не подменяет труппу.
    POST - правка состава целиком: {participants: [{cast_id, display_name, role,
           gender, model, temperature, ...}]}. Работает и в настройке, и на ходу:
           место опознаётся по cast_id, поэтому его можно переставить, убрать или
           добавить, а роль — поменять (роль у каждого места одна).
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
        # Сцена правится вместе с составом: именно она переживёт новый спектакль
        # и перезапуск приложения
        show.save_theatre_settings()
        print(f"🎭 Состав обновлён: {[p.get('display_name') for p in show.session.runtime_participants]}")
        return jsonify({"success": True, "participants": cast_payload()})

    models = [p.get("model", "") for p in show.session.runtime_participants]
    return jsonify({
        "participants": cast_payload(),
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
        "models_status": models_status_payload(models),
        # И если они не влезают в видеопамять при текущем num_ctx
        "vram_status": ollama_api.check_vram_fit(models),
    })

@app.route('/api/participants/draft', methods=['POST'])
def draft_participant():
    """
    Заготовка нового места для кнопки «➕ Добавить»: имя, эмодзи, профессия
    и модель как у соседа по сцене. В составе при этом ничего не меняется —
    место появится только после «Применить состав».
    """
    data = request.get_json(silent=True) or {}
    role = str(data.get("role", "participant") or "participant")
    return jsonify({"success": True, "participant": show.draft_cast_entry(role)})

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
        # Аватар — часть пульта: его видит и следующий запуск, а не только
        # текущая страница (иначе «Найти аватар» приходилось бы повторять)
        show.save_theatre_settings()
        
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
    show.save_theatre_settings()

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
    # force=True: модели могли удалить или Ollama — перезапустить, поэтому перед
    # стартом список спрашиваем заново, а не берём из кэша
    models_status = models_status_payload([p.get("model", "") for p in cast], force=True)
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
                              kwargs={"on_post": publish_post, "on_draft": publish_draft})
    thread.daemon = True
    thread.start()
    # Спектакль пошёл — состояние начинает идти в браузер само, раз в секунду
    start_status_pusher()
    # Тему возвращаем: интерфейс показывает в шапке именно то, с чем играем
    return jsonify({"success": True, "session_id": show.session.session_id, "topic": show.session.topic})

@app.route('/api/reset', methods=['POST'])
def reset():
    """«Новый спектакль»: новые имена и характеры, а сцена и правила — те же."""
    show.session.new_show()
    # Новые имена — это тоже пульт: сохраняем, чтобы после перезапуска вернуться
    # именно к этому составу, а не к тому, что был до «Нового спектакля»
    show.save_theatre_settings()
    print(f"🎭 Новый состав: {[p.get('display_name') for p in show.session.runtime_participants]}")
    # Тему возвращаем вместе с составом: «Новый спектакль» имена меняет,
    # а тему оставляет — поле в пульте должно остаться заполненным
    return jsonify({"success": True, "participants": cast_payload(),
                    "topic": show.session.topic})


@app.route('/api/settings/reset', methods=['POST'])
def reset_settings():
    """«Полный сброс»: пульт заново из settings.py, сохранённое — забыть.

    Возвращает к исходному всё, что есть в редакторе: состав с ролями, общие
    правила общения, руководства модератора и правила судьи. Сохранённый пульт
    при этом забывается, чтобы сброс пережил перезапуск, — но забывается не весь:
    тема остаётся (см. forget_theatre_settings), потому что её не сбрасывает
    даже полный сброс: она про сюжет, а не про труппу.
    """
    show.session.reset_to_defaults()
    show.forget_theatre_settings()
    print("🧹 Полный сброс: состав и правила взяты из settings.py, "
          f"сохранённое забыто (тема осталась: {show.session.topic[:40] or '—'})")
    return jsonify({"success": True, "participants": cast_payload(),
                    "topic": show.session.topic})

@app.route('/api/post/<int:post_id>/turn')
def post_turn(post_id):
    """Полный отчёт о ходе, которым сказана эта реплика.

    Отдельным запросом, а не полем в самом посте: отчёт хода весит как сцена,
    и держать его в ленте — значит гонять мегабайты на каждой перерисовке ради
    того, что открывают редко. Отчёта нет только у человека (он никуда ничего
    не отправлял) или если спектакль уже сменился: отчёты живут в памяти и
    относятся к текущему спектаклю.
    """
    payload = show.session.turn_report(post_id)
    if payload is None:
        return jsonify({"error": "отчёта об этом ходе нет: он относится к текущему "
                                 "спектаклю, а у реплик человека его не бывает"}), 404
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/status')
def status():
    last_post_count = max(0, request.args.get("lastPostCount", 0, type=int))
    response = jsonify(status_payload(last_post_count))
    # Без этого браузер отдаёт статус из своего кэша, и панель с памятью GPU
    # «зависает» с устаревшими цифрами, пока не изменится счётчик постов
    response.headers["Cache-Control"] = "no-store"
    return response

# ============================================================
# WEBSOCKET СОБЫТИЯ
# ============================================================

# Socket.IO — основной канал: лента приходит событием new_post, состояние —
# status_update, а опрос (/api/status) остался редкой подстраховкой.
@socketio.on('connect')
def handle_connect():
    print("🔌 Клиент подключился через WebSocket")
    emit('state_update', status_payload(0))
    # Подключились к идущему спектаклю — состояние нужно сразу, а дальше его
    # будут приносить рассылки (иначе страница ждала бы своей подстраховки)
    emit('status_update', status_payload(with_posts=False))
    if show.session.running:
        start_status_pusher()

@socketio.on('disconnect')
def handle_disconnect():
    print("🔌 Клиент отключился от WebSocket")

@socketio.on('request_status')
def handle_request_status():
    emit('status_update', status_payload(with_posts=False))

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
    # Тема — часть пульта: придуманное однажды не должно спрашиваться заново
    # в каждом запуске
    show.save_theatre_settings()
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

    # Редактор — часть режиссёрского пульта, поэтому его правки тоже переживают
    # перезапуск: раньше сохранялись только правила судьи, а правила общения
    # и руководства после перезапуска тихо возвращались к дефолтным
    show.save_theatre_settings()

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

def port_from_argv(argv: list) -> int:
    """Порт из командной строки: `py . 5001` или `py . --port 5001`.

    Без аргумента берётся settings.PORT (5000), поэтому привычный запуск `py .`
    открывает браузер на том же адресе, что и раньше. Второй экземпляр так
    получает свой порт, а вместе с ним — и свои временные файлы (см.
    settings.prepare_instance): два театра на одной машине не мешают друг другу.
    """
    args = [str(arg).strip() for arg in (argv or [])]
    for index, text in enumerate(args):
        if text.startswith("--port="):
            text = text[len("--port="):]
        elif text in ("--port", "-p"):
            text = args[index + 1] if index + 1 < len(args) else ""
        if text.isdigit() and 0 < int(text) < 65536:
            return int(text)
    return int(settings.PORT)


def port_is_busy(port: int) -> bool:
    """Занят ли порт — проверкой своей, а не надеждой на сервер.

    Windows разрешает двум экземплярам сесть на один порт: werkzeug ставит
    SO_REUSEADDR, и второй `py .` встаёт рядом с первым. Само по себе это
    не поломка, а вот молчание о нём — поломка: соединения достаются тому,
    кто занял порт раньше, поэтому второй экземпляр жив, но недостижим, и в нём
    при этом новый, чистый спектакль. Проверяем без SO_REUSEADDR: с ним занятый
    порт выглядел бы свободным — именно поэтому беда и была незаметной.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", int(port)))
        return False  # порт свободен
    except OSError:
        return True
    finally:
        probe.close()


def port_holders(port: int, output: str = None) -> list:
    """Кто держит порт — по выводу `netstat -ano`.

    Нужно, чтобы назвать человеку точный номер процесса: без него ему пришлось
    бы самому вспоминать эту утилиту и её ключи. Разбираются только числа
    и слово LISTENING, поэтому кодировка консоли не мешает: netstat отвечает
    в OEM-кодировке, и русские заголовки от неё портятся, а числа — нет.
    """
    if output is None:
        output = _netstat_text()
    marker = f":{int(port)}"
    found = []
    for line in str(output or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        # Порт сверяется по КОНЦУ адреса, а не по вхождению: 5000 есть
        # и в адресе порта 50001, и тогда чужая строка выдала бы себя за нашу
        if not parts[1].endswith(marker) or parts[3].upper() != "LISTENING":
            continue
        if parts[4].isdigit() and int(parts[4]) not in found:
            found.append(int(parts[4]))
    return found


def _netstat_text() -> str:
    """Вывод `netstat -ano`: своей утилитой, а без неё — пустой строкой.

    Пустая строка значит «не знаю чей порт» — это не повод падать: спектакль
    важнее диагностики (см. port_holders).
    """
    try:
        done = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10)
    except Exception:
        return ""
    return done.stdout.decode("utf-8", "replace")


def port_conflict_notice(port: int, holders: list) -> list:
    """Что сказать в консоли, если порт уже занят другим экземпляром театра.

    Порт при этом НЕ подменяется: где попросили, там и играем, иначе поведение
    перестаёт быть предсказуемым — `py .` обязан вести на 5000, а другой порт
    назначается только аргументом (`py . 5001`). Но сказать вслух обязательно:
    соединения Windows отдаёт тому, кто занял порт раньше, поэтому второй
    экземпляр жив, а недостижим — и со стороны это выглядит как «театр помнит
    старый спектакль». И ещё важное: поднять второй экземпляр на том же порту
    можно, и это не ошибка — просто до закрытия первого его никто не увидит.
    """
    lines = [f"⚠️  Порт {port} уже занят другим экземпляром театра.",
             "   Соединения отдаются тому, кто занял порт раньше: пока он жив,"
             " браузер открывает его страницу",
             "   (возможно, со старым спектаклем), а этот экземпляр будет недостижим."]
    if holders:
        lines.append(f"   Держат порт процессы: {', '.join(str(pid) for pid in holders)}.")
        lines.append("   Закрыть ненужные (это команда для обычной консоли Windows):")
        lines.extend(f"      taskkill /PID {pid} /F" for pid in holders)
        lines.append("   После закрытия обновите страницу — отвечать начнёт этот экземпляр.")
    else:
        lines.append("   Какой процесс его держит — определить не удалось:"
                     f" посмотрите сами — netstat -ano | findstr :{port}")
    return lines


def main():
    port = port_from_argv(sys.argv[1:])
    # Порт проверяем сами (см. port_is_busy): иначе второй экземпляр сядет на
    # тот же порт молча, и браузер откроет первый — а там старый спектакль,
    # откуда и берётся «он помнит старые реплики»
    # Порт остаётся тем, что попросили: `py .` — это 5000, а другой порт
    # назначается только аргументом. Но о занятом порте говорим вслух (см.
    # port_conflict_notice): молчание тут и рождает «театр помнит старый спектакль»
    busy = port_is_busy(port)
    if busy:
        for line in port_conflict_notice(port, port_holders(port)):
            print(line)
        print()
    # Папка экземпляра заводится до первого чтения настроек: сцена, правила
    # судьи и стенограмма читаются уже из неё, а не из корня проекта
    instance_dir, moved = settings.prepare_instance(port)
    # И только теперь читаем сцену и правила судьи прошлых запусков: раньше
    # этого места неизвестно, где они лежат, и чтение смотрело бы в корень
    # проекта, хотя файлы экземпляра лежат в его папке
    show.load_theatre_settings()

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
    print(f"Файлы спектакля: {instance_dir}")
    if moved:
        print(f"  📦 Из корня проекта переехало: {', '.join(moved)}")
    print("🌐 Открываю браузер...")
    print(f"📍 Адрес: http://localhost:{port}")
    print()
    print("Нажмите Ctrl+C для остановки сервера")
    print("=" * 50)
    
    threading.Timer(1.5, lambda: webbrowser.open(f'http://localhost:{port}')).start()
    # allow_unsafe_werkzeug: без него flask-socketio падает с RuntimeError
    # "The Werkzeug web server is not designed to run in production", если stdin
    # не подключён к терминалу (перенаправленный вывод, запуск из IDE/службы).
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
