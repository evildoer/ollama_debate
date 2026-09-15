"""Спектакль: состав, характеры, сессия дебатов и поток, который их играет.

Здесь живёт единственный источник правды о труппе — объект `session`. Пульт,
сервер и поток спектакля работают с ним одним и тем же, поэтому правки на ходу
видны всем, а уже опубликованные реплики не переписываются.

Обратная зависимость только одна: сессия просит Ollama (ask_model) и получает
готовый текст. Наружу спектакль отдаёт посты через колбэк on_post — так этот
модуль ничего не знает про Flask и Socket.IO.
"""

import copy
import json
import random
import re
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import ollama_api
from . import settings
from . import text
from . import tooltext

def load_theatre_settings():
    """
    Возвращает режиссёрский пульт, сохранённый в прошлых запусках: состав
    целиком (имена, аватары, модели, роли, числа и личные инструкции)    тему, а из правил — общие правила общения, руководства модератора и правила
    судьи.

    Так перезапуск приложения возвращает режиссёра к прежнему спектаклю, только
    с чистой историей: настроенное не приходится собирать заново. Файл может быть
    и от прежней версии — в нём тогда одна сцена без имён: места берутся оттуда,
    а имена и характеры разыгрываются как раньше.
    """
    try:
        if settings.SETTINGS_FILE.exists():
            data = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            topic = data.get("topic")
            if isinstance(topic, str) and topic.strip():
                session.topic = topic
                print(f"🎯 Загружена сохранённая тема: {topic.splitlines()[0][:60]}")
            _load_saved_instructions(data)
            cast = sanitize_cast(data.get("cast"))
            if cast:
                session.runtime_participants = cast
                # Сцена — производное от состава: она нужна «Новому спектаклю»,
                # который разыграет имена заново, а роли, числа и личные
                # инструкции мест возьмёт отсюда
                session.scene = scene_from_cast(cast)
                session.sync_cast_media()
                print(f"🎭 Загружен сохранённый состав: мест {len(cast)} — "
                      f"{', '.join(p['display_name'] for p in cast)}")
                return
            scene = sanitize_scene(data.get("scene"))
            if scene:
                session.scene = scene
                print(f"🎬 Загружена сохранённая сцена: мест {len(scene)}")
    except Exception as e:
        print(f"  ⚠️  Не читается {settings.SETTINGS_FILE.name}: {e}")


def _load_saved_instructions(data: dict):
    """Правила, руководства и общие инструкции из файла — в сессию."""
    for key, field, title in (
        ("judge_rules", "judge_rules", "правила судьи"),
        ("static_instructions", "static_instructions", "правила общения"),
        ("moderator_guidelines", "moderator_guidelines", "руководства модератора"),
    ):
        saved = data.get(key)
        if not isinstance(saved, list):
            continue
        # Пустой список — это «как в settings.py», а не «ничего не сказано»:
        # иначе стёртые в редакторе правила возвращались бы из дефолтов молча
        lines = [str(line) for line in saved if str(line).strip()]
        setattr(session, field, lines)
        if lines:
            print(f"📝 Загружены сохранённые {title}: {len(lines)} пунктов")


def save_theatre_settings():
    """Сохраняет пульт целиком рядом с проектом (файл в .gitignore).

    Состав сохраняется вместе с именами и личными инструкциями: режиссёр,
    вернувшись к театру после перезапуска, хочет видеть прежний спектакль.
    История при этом начинается с нуля — посты и снимки ходов живут в памяти.
    """
    try:
        settings.SETTINGS_FILE.write_text(
            json.dumps({
                "version": 2,
                # Тема тоже режиссёрская настройка: придумывать её заново каждый
                # запуск — работа, а не часть спектакля
                "topic": session.topic or "",
                "judge_rules": list(session.judge_rules or []),
                "static_instructions": list(session.static_instructions or []),
                "moderator_guidelines": list(session.moderator_guidelines or []),
                # Роль пишем и словом: файл правят руками, и «judge» читается
                # явно, тогда как два флага рядом с именем — уже загадка
                "cast": [{**dict(place), "role": cast_role(place)}
                         for place in session.runtime_participants],
            }, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Не сохраняется {settings.SETTINGS_FILE.name}: {e}")


def forget_theatre_settings():
    """Забыть сохранённый пульт.

    Нужен полному сбросу: если файл оставить, следующий запуск вернул бы то,
    от чего режиссёр только что отказался.
    """
    try:
        settings.SETTINGS_FILE.unlink(missing_ok=True)
    except OSError as e:
        print(f"  ⚠️  Не удаляется {settings.SETTINGS_FILE.name}: {e}")


# Заголовок раздела стенограммы — тот же, что пишет start_thinking_log.
# Ищем по нему, а не по любому «# » в начале строки: модели пишут заголовки
# и в самих мыслях, и обрезка по чужим заголовкам резала бы спектакль пополам
_THINKING_SECTION = re.compile(r"(?m)^(?=# \d{2}\.\d{2}\.\d{4} \d{2}:\d{2} — )")


def trim_thinking_log(keep: int = None) -> int:
    """Обрезает стенограмму до последних `keep` спектаклей.

    Стенограмма растёт с каждым ходом и никогда не чистилась: после десятков
    спектаклей это мегабайты, а читают её глазами и всегда с конца. Возвращает,
    сколько разделов убрано (0 — обрезать было нечего или запрещено настройкой).
    """
    keep = int(settings.THINKING_KEEP_SHOWS if keep is None else keep)
    if keep <= 0:                      # 0 — не чистить вовсе
        return 0

    path = settings.THINKING_FILE
    try:
        if not path.exists():
            return 0
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"  ⚠️  Не читается {path.name}: {e}")
        return 0

    parts = _THINKING_SECTION.split(raw)
    sections = parts[1:]
    if len(sections) <= keep:
        return 0

    dropped = len(sections) - keep
    try:
        path.write_text(parts[0] + "".join(sections[-keep:]), encoding="utf-8")
    except OSError as e:
        print(f"  ⚠️  Не обрезается {path.name}: {e}")
        return 0
    return dropped


def start_thinking_log(topic: str):
    """Начинает раздел размышлений этого спектакля в стенограмме.

    В ленте мысли видны только пока идёт ход, и после занавеса они бы пропали.
    Стенограмма (обычный markdown-файл рядом с проектом) остаётся: её можно
    прочитать позже — там видно, о чём модель думала на каждом своём ходу.

    Перед записью стенограмма обрезается до последних спектаклей: иначе файл
    рос бы вечно (см. THINKING_KEEP_SHOWS).
    """
    dropped = trim_thinking_log()
    if dropped:
        print(f"  🧹 Стенограмма: убрано {dropped} прежних спектаклей, "
              f"оставлено {settings.THINKING_KEEP_SHOWS}")
    try:
        with open(settings.THINKING_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"\n\n# {time.strftime('%d.%m.%Y %H:%M')} — "
                         f"{(topic or '').strip() or 'без темы'}\n")
    except Exception as e:
        print(f"  ⚠️  Не открывается {settings.THINKING_FILE.name}: {e}")


def save_thinking_entry(post: dict):
    """Складывает размышления хода в стенограмму — по одной записи на реплику.

    Туда же идёт набросок — прежняя версия реплики, которую модель сказала
    до поиска, а потом переписала: в ленте он живёт свёрнутым блоком, но после
    занавеса ленты уже нет, и прочитать его можно только здесь.

    Пустые размышления не пишутся: у местных моделей их не бывает вовсе,
    и файл не должен пухнуть заголовками без единой мысли.
    """
    thoughts = (post.get("thinking") or "").strip()
    sketch = (post.get("sketch") or "").strip()
    if not thoughts and not sketch:
        return
    title = (f"\n### {post.get('timestamp', '')} · {post.get('display_name', '')} "
             f"({post.get('model_used', '')}) · Акт {post.get('round', '?')}\n\n")
    body = ""
    if thoughts:
        body += thoughts + "\n"
    if sketch:
        body += f"\n**Сказано раньше (потом модель ответила заново):**\n\n{sketch}\n"
    try:
        with open(settings.THINKING_FILE, "a", encoding="utf-8") as handle:
            handle.write(title + body)
    except Exception as e:
        print(f"  ⚠️  Не сохраняются размышления в {settings.THINKING_FILE.name}: {e}")

def _message_view(msg: dict) -> dict:
    """Сообщение в том виде, в каком его показывают: роль, говорящий, вес и текст.

    Токены считаются тем же счётом, каким приложение меряет историю: строка
    «уехало столько-то» должна сходиться с тем, чем резали, иначе она врёт.
    """
    content = msg.get("content", "") or ""
    return {
        "role": str(msg.get("role", "") or ""),
        "name": str(msg.get("name", "") or ""),
        "tokens": text.estimate_tokens(content),
        "content": content,
    }


def build_prompt_payload(participant: dict, round_num: int, sent: list, added: list,
                         trim_report: dict, topic: str = "", search_count: int = 0) -> dict:
    """Снимок того, что уехало в модель на этом ходу.

    В ленте видно только ответ, а сколько истории к нему приложено, чьим окном
    она мерена и что из неё выброшено — не было видно нигде: это жило в консоли,
    то есть уходило вместе с ней. Здесь это лежит рядом с репликой.

    Сам снимок весит как сцена (десятки килобайт на ход), поэтому в посте
    остаётся его сводка, а текст уходит странице отдельным запросом — когда
    зритель блок раскроет (см. /api/post/<id>/prompt).
    """
    messages = [_message_view(m) for m in sent]
    extra = [_message_view(m) for m in added]
    removed = list(trim_report.get("removed") or [])
    window = int(trim_report.get("window") or 0)
    summary = {
        "stored": True,
        "messages": len(messages),
        "tokens": sum(m["tokens"] for m in messages),
        "window": window,
        # Чьё окно мерило историю: у облачного участника оно своё, а «не ограничено»
        # — это CLOUD_NUM_CTX = 0, то есть счёт без предела
        "window_kind": ("unbounded" if trim_report.get("unbounded")
                        else "cloud" if trim_report.get("cloud") else "local"),
        "removed_messages": len(removed),
        "removed_tokens": sum(int(r.get("tokens") or 0) for r in removed),
        # Что ход дописал в запрос уже на своих кругах (поиск и его результаты):
        # реплик там нет, а токены за них платятся — и знать об этом стоит
        "extra_messages": len(extra),
        "extra_tokens": sum(m["tokens"] for m in extra),
        "search_rounds": int(search_count or 0),
    }
    return {
        "summary": summary,
        "who": {
            "name": participant.get("display_name", ""),
            "model": participant.get("model", ""),
            "role": role_of(participant.get("is_moderator", False),
                            participant.get("is_judge", False)),
            "round": round_num,
            "topic": topic or "",
            "time": time.strftime("%H:%M"),
        },
        "budget": {
            "window": window,
            "kind": summary["window_kind"],
            "reserve": int(trim_report.get("reserve") or 0),
            "safety": int(trim_report.get("safety") or 0),
            "system_tokens": int(trim_report.get("system_tokens") or 0),
            "available": trim_report.get("available"),
            "history_tokens": int(trim_report.get("history_tokens") or 0),
            "kept_tokens": int(trim_report.get("kept_tokens") or 0),
            "messages_before": int(trim_report.get("messages_before") or len(messages)),
            "messages_after": int(trim_report.get("messages_after") or len(messages)),
        },
        "messages": messages,
        "added": extra,
        "removed": removed,
    }


def create_post(display_name: str, model_used: str, content: str, round_num: int, 
                avatar_url: str = None, avatar_emoji: str = None,
                search_count: int = 0, search_queries: list = None,
                role: str = "participant", gender: str = "male",
                thinking: str = "", sketch: str = "", prompt: dict = None) -> dict:
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
        "content_html": text.markdown_to_html(content),
        # Размышления модели — не реплика, но и не мусор: их тратят наши токены.
        # В ленте они живут свёрнутым блоком, так что прочитать их можно и после
        # спектакля, а не только пока модель говорит (см. _StreamingReply)
        "thinking": thinking or "",
        # А это прежняя версия самой реплики: модель сказала её до поиска и потом
        # ответила заново. В ленте ей отведён свой свёрнутый блок
        "sketch": sketch or "",
        "round": round_num,
        "timestamp": time.strftime("%H:%M"),
        "search_count": search_count,
        "search_queries": search_queries,
        # Что именно уехало в модель на этом ходу: в посте — только сводка,
        # сам снимок живёт в памяти сессии (см. DebateSession.prompt_payload)
        "prompt": (prompt or {}).get("summary"),
        "role": role,
        "role_icon": role_icons.get(role, "🎭"),
        "role_name": role_names.get(role, "Участник"),
        "gender": gender,
        "gender_symbol": "♂" if gender == "male" else "♀"
    }

def role_of(is_moderator: bool, is_judge: bool) -> str:
    """Роль места в ленте: модератор старше судьи — как и при разборе состава."""
    if is_moderator:
        return "moderator"
    if is_judge:
        return "judge"
    return "participant"

class _StreamingReply:
    """Черновик реплики: то, что модель говорит прямо сейчас.

    Пока ход идёт, в ленте растёт текст — как у живого человека, а не одним
    куском в конце. Черновик живёт отдельно от постов: он не попадает ни
    в историю, ни в счёт постов, ни в следующий промпт, а когда ход кончится,
    его место займёт настоящий пост (см. add_post).

    Отправляем не каждую порцию: лента всё равно не перерисует чаще INTERVAL,
    а часть шлюзов печатает по букве — на таком потоке лента захлебнулась бы.
    """

    INTERVAL = 0.12          # как часто отправлять набранное, в секундах
    THOUGHT_LIMIT = 20000    # сколько знаков размышлений держим в памяти
    THOUGHT_SHOWN = 600      # сколько из них видно в ленте, пока модель говорит
    SKETCH_LIMIT = 20000     # сколько знаков «сказано раньше» держим в памяти
    SKETCH_MARK = "\n\n———\n\n"   # разделитель, если версий было несколько

    def __init__(self, participant: dict, round_num: int, publisher):
        self.publisher = publisher
        self.stream_id = f"stream-{uuid.uuid4().hex[:8]}"
        self.text = ""
        self.thoughts = ""
        self.sketch = ""
        self.answer_started = False
        self.sent_at = 0.0
        self.started = False
        self.finished = False
        name = participant.get("display_name", "")
        # Вид черновика берём у поста: аватар, роль, значок — то же самое, иначе
        # черновик и настоящая реплика выглядели бы по-разному
        self.identity = create_post(
            display_name=name,
            model_used=participant.get("model", ""),
            content="",
            round_num=round_num,
            avatar_url=session.avatars.get(name),
            avatar_emoji=session.avatar_emojis.get(name, "📣"),
            role=role_of(participant.get("is_moderator", False),
                         participant.get("is_judge", False)),
            gender=participant.get("gender", "male"),
        )

    def feed(self, piece: str, replace: bool = False):
        """Очередная порция ответа. replace — прежний текст больше не в счёт.

        replace приходит на первый кусок каждого запроса: за один ход модель
        может говорить дважды (сказала без поиска, а после поиска — заново),
        и тогда в ленте должна остаться вторая реплика, а не склейка двух.

        Но сказанное в первый раз уже видел зритель и за него заплачены токены,
        поэтому оно не выбрасывается, а уходит в набросок (см. keep_sketch).
        """
        if replace:
            self.keep_sketch()
            self.text = ""
            self.answer_started = True
        self.text += piece
        self.send_soon(force=replace)

    def keep_sketch(self):
        """Сберечь сказанное до новой попытки: этот текст уже был в ленте.

        Ход бывает не один: сперва модель отвечает сама, потом её просят
        поискать, и она отвечает заново. Раньше в этот момент из ленты пропадало
        то, что зритель уже прочитал, — и выглядело это как «мысли, которые
        потом убрали». Набросок остаётся свёрнутым блоком (см. sketch в send),
        а в конце хода уходит в готовый пост.
        """
        text = self.visible().strip()
        if not text:
            return
        joined = text if not self.sketch else self.sketch + self.SKETCH_MARK + text
        self.sketch = joined[-self.SKETCH_LIMIT:]

    def visible(self) -> str:
        """Набранное без сырых вызовов поиска.

        Часть облачных моделей просит поиск не протоколом, а текстом —
        «search:web_search{query: "..."}». В ленте этому делать нечего: вызов
        вынимается, а поиск по нему выполняет код хода (см. tooltext). И для
        черновика тоже: иначе зритель видел бы вызов напечатанным, а потом
        он бы оттуда исчез.
        """
        return tooltext.take_calls(self.text)[0]

    def think(self, piece: str, replace: bool = False):
        """Порция размышлений: модель говорит сама с собой, пока не сказала вслух.

        Размышлений бывает больше самого ответа (у gpt-5-nano — 65 порций против
        6), и в ленте они не нужны целиком: интересны последние — те, что рядом
        с ответом. Поэтому храним и показываем только хвост.
        """
        if replace:
            self.thoughts = ""
        self.thoughts = (self.thoughts + piece)[-self.THOUGHT_LIMIT:]
        self.send_soon(force=replace)

    def send_soon(self, force: bool = False):
        """Отправить набранное, если оно того ждёт: не чаще INTERVAL."""
        now = time.monotonic()
        if force or now - self.sent_at >= self.INTERVAL:
            self.send(now)

    def thought_tail(self) -> str:
        """Хвост размышлений для ленты — без половины слова на конце обрезки."""
        text = self.thoughts
        if len(text) <= self.THOUGHT_SHOWN:
            return text
        cut = text[-self.THOUGHT_SHOWN:]
        space = cut.find(" ")
        return "…" + (cut[space + 1:] if space > 0 else cut)

    def thinking_full(self) -> str:
        """Мысли целиком — то, что останется в посте и в стенограмме.

        В ленте растёт только хвост (см. thought_tail): пока модель говорит,
        читать тысячи знаков незачем. А после спектакля они — единственный след
        её рассуждений, поэтому для поста берём их все, что уместились.
        """
        return self.thoughts.strip()

    def send(self, now: float = None):
        self.started = True
        self.sent_at = time.monotonic() if now is None else now
        shown = self.visible()
        self.publisher({**self.identity,
                        "stream_id": self.stream_id,
                        "content": shown,
                        "thinking": self.thought_tail(),
                        "sketch": self.sketch,
                        "answer_started": self.answer_started,
                        # Разметку собираем на каждой порции: жирный текст, списки
                        # и формулы-строчки появляются на глазах, а не в самом
                        # конце хода. Незакрытая звёздочка так звёздочкой
                        # и остаётся — её съест только готовая пара
                        "content_html": text.markdown_to_html(shown),
                        "draft": True})

    def finish(self):
        """Ход кончился: черновик уступает место настоящему посту."""
        if self.finished or not self.started:
            return
        self.finished = True
        self.publisher({"stream_id": self.stream_id, "draft": True, "done": True})

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
        self.judge_rules = list(settings.DEFAULT_JUDGE_RULES)
        self.moderator_guidelines = []
        self.waiting_for_human = False
        self.moderator_message = None
        self.moderator_finished = False
        self.runtime_participants = []
        self.conversation_history = []
        # Снимки запросов по номеру реплики: что именно уехало в модель
        self.prompt_log = {}
        self.prompt_forget_warned = False
        # Сцена: места состава без имён, аватаров и личных инструкций — роли,
        # модели, порядок и числа. None значит «своей сцены нет»: места берутся
        # из PARTICIPANTS. Пульт правит состав, а сцена — это то, что от него
        # остаётся на будущее, поэтому она переживает и новый спектакль,
        # и перезапуск приложения.
        self.scene = None

    # ------------------------------------------------------------
    # Состав: единственный источник правды
    # ------------------------------------------------------------
    # До старта это заготовка следующего спектакля, после старта - живой состав.
    # Поэтому режиссёрский пульт правит одни и те же данные в любой момент,
    # а не дублирует форму настройки отдельной формой модератора.

    def load_new_cast(self):
        """Новый состав: свежие имена, эмодзи и характеры, а места — со сцены.

        Сцены нет (первый запуск, «Состав из PARTICIPANTS») — места берутся
        из PARTICIPANTS, как было всегда.
        """
        self.runtime_participants = build_new_cast(self.scene or None)
        self.sync_cast_media()

    def reset_scene(self):
        """Забыть сцену и собрать состав заново из PARTICIPANTS.

        Это выход из положения, если составом наэкспериментировались так, что
        непонятно, откуда что взялось: имена, роли и порядок станут как в файле.
        """
        self.scene = None
        self.load_new_cast()

    def reset_to_defaults(self):
        """«Полный сброс»: весь пульт заново из настроек проекта.

        Тема, состав, общие правила общения, руководства модератора и правила
        судьи возвращаются к значениям из settings.py (у темы это пустота —
        в файле настроек её нет) — ровно так, как это выглядит при первом запуске
        с пустой папкой экземпляра. Сохранённый пульт при этом забывается (см.
        forget_theatre_settings): иначе следующий запуск вернул бы то, от чего
        режиссёр только что отказался.
        """
        self.topic = ""
        self.static_instructions = []
        self.judge_rules = list(settings.DEFAULT_JUDGE_RULES)
        self.moderator_guidelines = []
        self.scene = None
        self.load_new_cast()

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
        # Новый спектакль — новые реплики: снимки прежних ходов к ним не подходят
        self.prompt_log = {}
        self.sync_cast_media()

    def new_show(self):
        """«Новый спектакль»: новые имена и характеры, настройки, сцена и тема — те же."""
        guidelines = list(self.moderator_guidelines)
        judge_rules = list(self.judge_rules)
        static_instructions = list(self.static_instructions)
        # Тема переживает и «Новый спектакль»: сброс темы — это полный сброс,
        # а не новый состав той же труппы
        topic = self.topic
        # Сцена — то, что режиссёр настроил руками, поэтому её и переживает:
        # иначе «Новый спектакль» возвращал бы состав из файла, а не тот,
        # который только что собрали в пульте
        scene = [dict(place) for place in self.scene] if self.scene else None
        self.__init__()
        self.moderator_guidelines = guidelines
        self.judge_rules = judge_rules
        self.static_instructions = static_instructions
        self.scene = scene
        self.topic = topic
        self.load_new_cast()

    # ------------------------------------------------------------
    # Работа с постами
    # ------------------------------------------------------------

    def add_post(self, display_name, model_used, content, round_num,
                 search_count=0, search_queries=None,
                 is_moderator=False, is_judge=False, gender="male", thinking="",
                 sketch="", prompt=None):
        avatar_url = self.avatars.get(display_name)
        avatar_emoji = self.avatar_emojis.get(display_name, "📣")

        role = role_of(is_moderator, is_judge)

        post = create_post(display_name, model_used, content, round_num,
                          avatar_url, avatar_emoji, search_count, search_queries, role,
                          gender, thinking, sketch, prompt)
        self.posts.append(post)

        if content.strip():
            self.conversation_history.append({
                "display_name": display_name,
                "content": content,
                "is_moderator": is_moderator,
                "is_judge": is_judge,
                "round": round_num,
            })
        # Размышления в память спектакля не идут (иначе следующая модель прочитала
        # бы чужой черновик мыслей как сказанное вслух), а в файл — идут: там
        # стенограмма, и её читают после занавеса, а не посреди разговора
        save_thinking_entry(post)
        return post

    # ------------------------------------------------------------
    # Снимки запросов: что именно уехало в модель
    # ------------------------------------------------------------

    def remember_prompt(self, post_id: int, payload: dict) -> None:
        """Запомнить снимок запроса этого хода, забыв самые старые снимки.

        Снимок живёт в памяти, а не в файле: один ход весит как сама сцена,
        а через сотню ходов это уже десятки мегабайт — при том, что после
        перезапуска приложения посты исчезают вместе со снимками, и хранить
        их дольше просто не для кого (см. PROMPT_KEEP_TURNS).
        """
        self.prompt_log[post_id] = payload
        overflow = len(self.prompt_log) - max(0, int(settings.PROMPT_KEEP_TURNS))
        for old_id in list(self.prompt_log)[:max(0, overflow)]:
            forgotten = self.prompt_log.pop(old_id, None)
            # Сводка в посте и снимок — один и тот же словарь: пост сразу честно
            # говорит, что раскрывать больше нечего. Стереть блок молча нельзя:
            # «открыть и посмотреть» — это ровно то, зачем блок и делался
            if forgotten:
                forgotten["summary"]["stored"] = False
        if overflow > 0 and not self.prompt_forget_warned:
            self.prompt_forget_warned = True
            print(f"  🗂  Снимки запросов: держим последние {settings.PROMPT_KEEP_TURNS}, "
                  f"старые забываются — в старых репликах блок скажет об этом сам")

    def prompt_payload(self, post_id: int) -> dict:
        """Снимок запроса по номеру реплики (None — снимка нет и уже не будет)."""
        return self.prompt_log.get(int(post_id))

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

    def _get_history_for(self, viewer: dict, mode: str = "dialog",
                         round_num: int = None) -> list:
        """
        mode:
          "dialog"            — поток диалога: бусты модератора видны всем, а
                                вердикт судьи — всем, если судья публичный;
          "participants_only" — только посты обычных участников (для судьи),
                                а судье с опцией «только текущий акт» — ещё и
                                только за этот акт.
        """
        viewer_name = viewer.get("display_name", "")
        viewer_options = role_options(viewer)
        # Публичные судьи — те, чьё слово слышат остальные. Считаем по текущему
        # составу: сделав судью анонимным, его прошлые вердикты тоже закрываются
        public_judges = {
            p.get("display_name") for p in self.runtime_participants
            if p.get("is_judge") and role_options(p).get("publicity") == "public"
        }
        result = []
        for post in self.conversation_history:
            is_mod = post.get("is_moderator", False)
            is_judge = post.get("is_judge", False)

            if mode == "participants_only":
                if is_mod or is_judge:
                    continue
                if viewer.get("is_judge") and viewer_options.get("scope") == "act" \
                        and round_num is not None and post.get("round") != round_num:
                    continue
            elif mode == "dialog":
                if is_judge and post.get("display_name") != viewer_name \
                        and post.get("display_name") not in public_judges:
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
            elif post.get("is_judge"):
                # Сюда попадает только слово публичного судьи: анонимного другие
                # не слышат, и до этих строк его вердикт просто не доходит
                messages.append({
                    "role": "user",
                    "content": (f"⚖️ СУДЬЯ {speaker} выносит вердикт: {content}"),
                    "name": "judge",
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
            rules = [r for r in (self.judge_rules or settings.DEFAULT_JUDGE_RULES) if r.strip()]
        elif self.static_instructions:
            rules = [r for r in self.static_instructions if r.strip()]
        else:
            rules = settings.DEFAULT_STATIC_INSTRUCTIONS
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
            custom = settings.DEFAULT_JUDGE_INSTRUCTION
        if not (custom and custom.strip()):
            return []
        return [
            "ТВОИ ЛИЧНЫЕ ИНСТРУКЦИИ (обязательны к исполнению):\n"
            + self._substitute(custom.strip(), participant, other_names)
        ]

    def _search_block(self) -> list:
        if not settings.ENABLE_SEARCH:
            return []
        min_text = f" Сделай минимум {settings.MIN_SEARCHES} поиск(ов) перед ответом." if settings.MIN_SEARCHES > 0 else ""
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

    def build_messages_for_ai(self, participant: dict, round_num: int,
                              report: dict = None) -> list:
        """Сообщения для этой модели на этом ходу.

        report — необязательная посуда: если её передали, в неё ляжет отчёт
        обрезки истории (чьё окно, сколько было и что выброшено). Так снимок
        «что уехало в модель» получается из тех же чисел, которыми история
        и резалась, а не из второго, отдельного расчёта.
        """
        name = participant.get("display_name", "")
        name_norm = name.lower().replace(" ", "_")
        is_judge = participant.get("is_judge", False)

        system_prompt = self.get_system_prompt(participant)
        messages = [{"role": "system", "content": system_prompt, "name": "system"}]

        # ---- Судья ----
        if is_judge:
            # Что судья слышит, решает опция роли: всё обсуждение или только этот акт
            viewer_options = role_options(participant)
            history = self._get_history_for(participant, mode="participants_only",
                                            round_num=round_num)
            history_messages = self._format_history(name, history)
            trimmed, trim_report = text.trim_history_with_report(
                history_messages, text.estimate_tokens(system_prompt),
                model=participant.get("model", ""))
            messages.extend(trimmed)
            if report is not None:
                report.update(trim_report)

            if trimmed:
                task = ("оцени только текущий акт — то, что сказано с прошлого вердикта. "
                        if viewer_options.get("scope") == "act"
                        else "оцени выступления участников за всё обсуждение. ")
                messages.append({
                    "role": "user",
                    "content": (
                        f"Как {name}, {task}"
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
        # Окно — той модели, которая будет говорить: у облачного оно своё,
        # иначе история сцены режется по олламовским 7 тысячам токенов
        trimmed, trim_report = text.trim_history_with_report(
            history_messages, text.estimate_tokens(system_prompt),
            model=participant.get("model", ""))
        messages.extend(trimmed)
        if report is not None:
            report.update(trim_report)

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

    def handle_ai_turn(self, participant: dict, round_num: int, on_draft=None) -> tuple:
        self.current_action = "thinking"
        # Отчёт обрезки приходит из той же сборки сообщений, что и сами
        # сообщения: отдельного пересчёта для показа нет
        trim_report = {}
        messages = self.build_messages_for_ai(participant, round_num, report=trim_report)
        # Снимок делается ДО хода и копией: ask_model дописывает в этот же список
        # результаты поиска прямо на месте, и к концу хода «что уехало» было бы
        # уже не тем, с чего ход начался
        sent = copy.deepcopy(messages)

        # Черновик реплики: если спектакль умеет показывать её по кускам, текст
        # растёт в ленте, пока модель говорит (у местных моделей порций не будет —
        # on_delta до них не доходит, и черновик просто не начнётся)
        draft = None
        if on_draft is not None:
            draft = _StreamingReply(participant, round_num, on_draft)

        try:
            response, search_count, search_queries = ollama_api.ask_model(
                model=participant["model"],
                messages=messages,
                participant_name=participant["display_name"],
                options=ollama_api._merge_options(participant),
                think=ollama_api.resolve_think(participant),
                on_delta=draft.feed if draft else None,
                on_thought=draft.think if draft else None,
            )
        finally:
            # Черновик закрываем при любом выходе, в том числе при ошибке: иначе
            # недописанная реплика осталась бы висеть в ленте навсегда
            if draft is not None:
                draft.finish()

        # Что ход дописал в запрос уже на своих кругах: реплик там нет, но токены
        # за них платятся — и в снимке это должно быть видно отдельной строкой
        prompt = build_prompt_payload(
            participant, round_num, sent, messages[len(sent):], trim_report,
            topic=self.topic, search_count=search_count)

        post = self.add_post(
            display_name=participant["display_name"],
            model_used=participant["model"],
            content=response,
            round_num=round_num,
            search_count=search_count,
            search_queries=search_queries,
            is_judge=participant.get("is_judge", False),
            # Роль модератора тоже должна быть видна в ленте: пульт разрешает
            # назначить модератором не только живого человека, но и модель
            is_moderator=participant.get("is_moderator", False),
            gender=participant.get("gender", "male"),
            # Мысли, сказанные по дороге к ответу: в ленте — свёрнутым блоком,
            # в стенограмме — текстом. Черновик держит их для этого (см. think)
            thinking=draft.thinking_full() if draft else "",
            # А это прежняя версия реплики, от которой модель отказалась по пути
            # (обычно — чтобы сначала поискать): тоже свёрнутым блоком, чтобы
            # зритель мог дочитать то, что мелькнуло в ленте и пропало
            sketch=draft.sketch if draft else "",
            # А это снимок отправленного запроса: у этой реплики его можно
            # раскрыть и посмотреть, что именно прочитала модель
            prompt=prompt,
        )
        self.remember_prompt(post["id"], prompt)
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


def draw_character() -> str:
    """Случайный характер для нового спектакля.

    Все равны: и судья, и модератор тянут тот же жребий, что и остальные, а выпасть
    может любой набор — от педанта до «Хаоса». Повторы внутри спектакля разрешены.
    """
    keys = [k for k, v in settings.CHARACTER_PRESETS.items() if k != "custom" and v.get("params")]
    if not keys:
        return "custom"  # наборов нет — числа берутся из Modelfile модели
    return random.choice(keys)


def character_parameters(key: str) -> tuple:
    """Параметры и режим размышлений для характера: базовые значения плюс разброс.

    Разброс нужен, чтобы два «Педанта» в разных спектаклях всё же отличались.
    """
    preset = settings.CHARACTER_PRESETS.get(key) or settings.CHARACTER_PRESETS["custom"]
    drift = preset.get("drift", settings.CHARACTER_DRIFT)
    params = {}
    for param, base in (preset.get("params") or {}).items():
        step = drift.get(param, 0)
        value = base + random.uniform(-step, step) if step else base
        params[param] = _clamp_option(param, value)
    return params, preset.get("think", "auto")


# ── МЕСТА В СОСТАВЕ ────────────────────────────────────────────────────────
# У каждого места свой cast_id, и состав правят именно по нему, а не по номеру:
# режиссёр убирает, добавляет и переставляет участников прямо в пульте, поэтому
# «третий по счёту» в любой момент может оказаться другим человеком — вместе с
# чужими личными инструкциями, если опознавать место по индексу.
CAST_ROLES = ("participant", "moderator", "judge")


def cast_role(participant: dict) -> str:
    """Роль места в составе: обычный участник, модератор или судья."""
    if participant.get("is_moderator"):
        return "moderator"
    if participant.get("is_judge"):
        return "judge"
    return "participant"


def set_cast_role(participant: dict, role: str):
    """Роль ставится целиком: модератор и судья — разные места, а не два флага,
    поэтому «сделать судьёй» автоматически снимает прежнюю роль."""
    participant["is_moderator"] = role == "moderator"
    participant["is_judge"] = role == "judge"


# ── ОПЦИИ РОЛИ ───────────────────────────────────────────────────────────────
# Опция — это не выключатель, а положение: у неё конечный набор значений, и одно
# из них занято всегда. Опции есть только у роли судьи, и обе они про слух, потому
# что роль — это ведь прежде всего вопрос «кто кого слышит»:
#
#   scope     — что видит судья: всё обсуждение или только текущий акт
#   publicity — кто слышит судью: только режиссёр (как было всегда) или все
#
# Значения по умолчанию — это ровно то, как спектакль игрался до появления опций,
# поэтому старые сцены и файлы настроек ничего не теряют.
ROLE_OPTIONS = {
    "judge": {
        "scope": ("all", "act"),
        "publicity": ("anonymous", "public"),
    },
}

ROLE_OPTION_DEFAULTS = {
    "judge": {"scope": "all", "publicity": "anonymous"},
}


def sanitize_role_options(role: str, raw) -> dict:
    """Опции роли: неизвестные ключи и чужие значения просто не берутся.

    Так читаются и файл настроек (его правят руками), и старые сцены, где опций
    ещё не было, — место получает набор своей роли по умолчанию.
    """
    allowed = ROLE_OPTIONS.get(role) or {}
    options = dict(ROLE_OPTION_DEFAULTS.get(role, {}))
    if isinstance(raw, dict):
        for key, values in allowed.items():
            if raw.get(key) in values:
                options[key] = raw[key]
    return options


def role_options(participant: dict) -> dict:
    """Опции места — по его роли. У роли без опций это пустой словарь."""
    return sanitize_role_options(cast_role(participant), participant.get("role_options"))


def apply_role_options(entry: dict, sent=None):
    """Ставит месту набор опций его роли, сохраняя то, что уже стояло.

    Если роль без опций — поле убирается совсем: иначе в составе копился бы мусор
    от прежней роли, и место, снова став судьёй, вспомнило бы чужие настройки.
    """
    role = cast_role(entry)
    if not ROLE_OPTIONS.get(role):
        entry.pop("role_options", None)
        return
    merged = dict(entry.get("role_options") or {})
    if isinstance(sent, dict):
        merged.update(sent)
    entry["role_options"] = sanitize_role_options(role, merged)


def _free_pick(pool, used) -> str:
    """Случайное значение из списка, которого нет среди занятых.

    dict.fromkeys заодно снимает повторы в самом списке: имена и профессии
    когда-то выдавались через .remove(), а он убирает только одно вхождение —
    лишний повтор давал двух «Галин» в одном спектакле.
    """
    free = [value for value in dict.fromkeys(pool) if value not in used]
    return random.choice(free) if free else ""


def pick_name(gender: str, used_names: set) -> str:
    """Свободное имя для нужного пола; если свои кончились — берём из другого списка."""
    own = settings.MALE_NAMES if gender == "male" else settings.FEMALE_NAMES
    other = settings.FEMALE_NAMES if gender == "male" else settings.MALE_NAMES
    for pool in (own, other, own + other):
        name = _free_pick(pool, used_names)
        if name:
            return name
    return "Участник"


def pick_emoji(gender: str, used_emojis: set) -> str:
    """Эмодзи по полу: сначала свои, потом нейтральные; два одинаковых в одном
    спектакле не встречаются, а нейтральные добирают нехватку."""
    own = settings.AVATAR_EMOJIS_MALE if gender == "male" else settings.AVATAR_EMOJIS_FEMALE
    for pool in (own, settings.AVATAR_EMOJIS_NEUTRAL, settings.AVATAR_EMOJIS):
        emoji = _free_pick(pool, used_emojis)
        if emoji:
            return emoji
    return "📣"


def pick_keywords(gender: str, used: dict) -> str:
    """Ключевые слова для аватара: профессия плюс пол.

    Пол дописывается обязательно, иначе поисковик охотно отдаёт женщине
    «мужчину-геолога», а профессия не повторяется — иначе двум участникам
    искалась бы одна и та же картинка.
    """
    gender_word = "женщина" if gender == "female" else "мужчина"
    profession = _free_pick(settings.PROFESSIONS, used["professions"]) or "человек"
    used["professions"].add(profession)
    return f"{profession} {gender_word}"


def empty_used() -> dict:
    """Занятые имена, эмодзи, ключевые слова и профессии — для выбора свободных."""
    return {"names": set(), "emojis": set(), "keywords": set(), "professions": set()}


def used_from_cast(cast: list) -> dict:
    """Занятые имена, эмодзи и профессии по уже собранному составу."""
    used = empty_used()
    for participant in cast or []:
        used["names"].add(participant.get("display_name", ""))
        used["emojis"].add(participant.get("avatar_emoji", ""))
        keywords = participant.get("avatar_keywords", "") or ""
        used["keywords"].add(keywords)
        if keywords.strip():
            used["professions"].add(keywords.rsplit(" ", 1)[0])
    return used


def build_cast_entry(template: dict, used: dict) -> dict:
    """Одно место в составе: имя, пол, эмодзи, ключевые слова и характер.

    used — занятые значения; функция ими пользуется и тут же их пополняет,
    поэтому и вся труппа, и одно добавленное место собираются одинаково.
    """
    gender = random.choice(["male", "female"])
    role = template.get("role") or cast_role(template)
    name = pick_name(gender, used["names"])
    keywords = pick_keywords(gender, used)
    emoji = pick_emoji(gender, used["emojis"])
    used["names"].add(name)
    used["emojis"].add(emoji)
    used["keywords"].add(keywords)

    entry = {
        "cast_id": uuid.uuid4().hex[:8],
        "model": template.get("model", ""),
        "display_name": name,
        "avatar_keywords": keywords,
        "avatar_emoji": emoji,
        "avatar_url": None,
        "gender": gender,
        # Личная инструкция живёт у места: написанную режиссёром переносим,
        # а судье без неё ставим ту, что написана для роли в settings.py
        "instruction": str(template.get("instruction", "") or "").strip()
                        or (settings.DEFAULT_JUDGE_INSTRUCTION if role == "judge" else ""),
    }
    set_cast_role(entry, role)
    apply_role_options(entry, template.get("role_options"))

    # Персональные числа и режим размышлений: то, что задано явно, важнее жребия
    own_params = {key: template[key] for key in settings.PER_PARTICIPANT_OPTION_KEYS
                  if template.get(key) is not None}
    entry.update(own_params)
    if template.get("think") in settings.THINK_MODES and template.get("think") != "auto":
        entry["think"] = template["think"]
    if isinstance(template.get("preset"), str) and template["preset"]:
        entry["preset"] = template["preset"]
    # Характер на этот спектакль — общий жребий, без скидок на роль
    elif settings.RANDOMIZE_CHARACTERS and entry["model"] != "human" and not own_params:
        character = draw_character()
        params, think = character_parameters(character)
        entry.update(params)
        entry["preset"] = character
        if think != "auto":
            entry["think"] = think

    return entry


def build_new_cast(places: list = None) -> list:
    """
    Новая труппа: имена, эмодзи и характеры разыгрываются заново, а места (роли,
    модели, порядок, числа) берутся со сцены — того, что режиссёр настроил
    в пульте. Сцены нет — места берутся из PARTICIPANTS.
    """
    templates = places if places is not None else settings.PARTICIPANTS
    used = empty_used()
    return [build_cast_entry(template, used) for template in templates]


def scene_from_cast(cast: list) -> list:
    """
    Сцена: места состава без имён и аватаров — роли, модели, порядок, числа
    и личные инструкции.

    Имена и характеры «Новый спектакль» разыгрывает заново, а вот роли, модели,
    порядок в очереди реплик, числа и написанные руками инструкции режиссёр
    настраивает один раз — они и переживают и «Новый спектакль», и перезапуск
    приложения. Личная инструкция поэтому привязана к месту, а не к имени:
    она — часть режиссёрской настройки, а не свойство случайного имени.
    """
    scene = []
    for participant in cast or []:
        place = {"model": participant.get("model", ""), "role": cast_role(participant)}
        options = role_options(participant)
        if options:
            place["role_options"] = options
        for key in settings.PER_PARTICIPANT_OPTION_KEYS:
            if participant.get(key) is not None:
                place[key] = participant[key]
        if participant.get("think") in settings.THINK_MODES:
            place["think"] = participant["think"]
        if participant.get("preset"):
            place["preset"] = str(participant["preset"])[:32]
        instruction = str(participant.get("instruction", "") or "").strip()
        if instruction:
            place["instruction"] = instruction
        scene.append(place)
    return scene


def default_cast_model(cast: list, places: list = None) -> str:
    """
    Модель для нового места — по очереди: как у соседа по сцене, иначе как
    в PARTICIPANTS, иначе первая скачанная модель Ollama.

    Последняя попытка нужна для состава, собранного с нуля: соседа ещё нет,
    а список PARTICIPANTS пуст или в нём одни живые люди (их модель — «human»,
    её в участника не подставишь). Читать список моделей не страшно: он кэшируется,
    а в пульте это одно нажатие «➕». Нет Ollama или ничего не скачано — место
    честно останется без модели, и пульт скажет об этом в разделе «Готовность».
    """
    for participant in reversed(cast or []):
        model = participant.get("model", "")
        if model and model != "human":
            return model
    for place in (places if places is not None else settings.PARTICIPANTS):
        model = place.get("model", "")
        if model and model != "human":
            return model
    models, _error = ollama_api.fetch_ollama_models()
    return next(iter(sorted(models or {})), "")


def sanitize_scene(raw) -> list:
    """
    Сцена из файла настроек: лишнее отбрасывается, чужие значения не проходят.

    Файл лежит рядом с проектом и правится руками, поэтому сцену из него проверяем
    так же строго, как пришедшую из пульта: непонятная роль или строка вместо
    числа не должны ломать спектакль — такое место просто теряет это поле.
    """
    if not isinstance(raw, list):
        return []
    scene = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        place = {"model": str(item.get("model", "") or ""),
                 "role": role if role in CAST_ROLES else "participant"}
        options = sanitize_role_options(place["role"], item.get("role_options"))
        if options:
            place["role_options"] = options
        for key in settings.PER_PARTICIPANT_OPTION_KEYS:
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                place[key] = value
        if item.get("think") in settings.THINK_MODES:
            place["think"] = item["think"]
        if item.get("preset") in settings.CHARACTER_PRESETS:
            place["preset"] = item["preset"]
        instruction = item.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            place["instruction"] = instruction
        scene.append(place)
    return scene


def sanitize_cast(raw) -> list:
    """
    Сохранённый состав из файла: места целиком, как их собрал пульт, — с именами,
    аватарами, моделями, ролями, числами и личными инструкциями.

    Файл лежит рядом с проектом и правится руками, поэтому состав из него проверяем
    так же строго, как пришедший из пульта: непонятная роль или строка вместо
    числа не должны ломать спектакль — место просто теряет это поле. Место без
    имени не берётся вовсе: имя — это то, чем место зовётся в ленте и в истории.
    """
    if not isinstance(raw, list):
        return []
    cast = []
    used = empty_used()
    used_ids = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name", "") or "").strip()
        if not name:
            continue
        gender = "female" if item.get("gender") == "female" else "male"
        # Два места с одним именем запутали бы и историю для промптов, и ленту:
        # файл правится руками, поэтому второму имени достаётся свободное
        if name in used["names"]:
            replacement = pick_name(gender, used["names"])
            print(f"  ⚠️  В сохранённом составе два места звались {name} — "
                  f"второе названо {replacement}")
            name = replacement
        used["names"].add(name)

        cast_id = str(item.get("cast_id", "") or "")
        if not cast_id or cast_id in used_ids:
            cast_id = uuid.uuid4().hex[:8]
        used_ids.add(cast_id)

        avatar_url = item.get("avatar_url")
        place = {
            "cast_id": cast_id,
            "model": str(item.get("model", "") or ""),
            "display_name": name,
            "avatar_keywords": str(item.get("avatar_keywords", "") or ""),
            "avatar_emoji": str(item.get("avatar_emoji", "") or "") or "📣",
            "avatar_url": avatar_url if isinstance(avatar_url, str) and avatar_url.strip() else None,
            "gender": gender,
            "instruction": str(item.get("instruction", "") or ""),
        }
        # Роль в файле — одним словом («judge»), но рядом лежат и флаги состава:
        # файл правят руками, поэтому понимаем и то, и другое, а место с непонятной
        # ролью остаётся обычным участником
        role = item.get("role")
        set_cast_role(place, role if role in CAST_ROLES else cast_role(item))
        apply_role_options(place, item.get("role_options"))
        for key in settings.PER_PARTICIPANT_OPTION_KEYS:
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                place[key] = value
        if item.get("think") in settings.THINK_MODES and item.get("think") != "auto":
            place["think"] = item["think"]
        if item.get("preset") in settings.CHARACTER_PRESETS:
            place["preset"] = item["preset"]
        cast.append(place)
    return cast


def still_in_cast(participant: dict, cast: list) -> bool:
    """Место всё ещё в составе? Режиссёр может убрать его прямо посреди акта.

    Сравниваем по cast_id, а не по имени: имя правится в пульте на ходу,
    а место при этом остаётся тем же.
    """
    cast_id = participant.get("cast_id")
    if cast_id:
        return any(p.get("cast_id") == cast_id for p in cast)
    return participant in cast


def draft_cast_entry(role: str = "participant") -> dict:
    """
    Заготовка нового места для кнопки «➕ Добавить»: имя, эмодзи и профессия
    выбираются сразу, а модель — как у соседа по сцене.

    Заготовка живёт только на странице: в составе места ещё нет, и до кнопки
    «Применить состав» она спектакль не трогает.
    """
    template = {
        "model": default_cast_model(session.runtime_participants,
                                    session.scene or settings.PARTICIPANTS),
        "role": role if role in CAST_ROLES else "participant",
    }
    return build_cast_entry(template, used_from_cast(session.runtime_participants))


def apply_cast_patch(incoming: list) -> str:
    """
    Применяет состав целиком: имена, пол, роли, модели, ключевые слова, аватар,
    порядок в очереди реплик, добавление и удаление мест.

    Место опознаётся по cast_id, а не по номеру: режиссёр переставляет и убирает
    участников прямо в пульте, и по индексам личная инструкция уехала бы к соседу.
    Пришло без cast_id — значит это новое место, и ему придумываются имя, эмодзи и
    профессия, если клиент их не прислал.

    Одинаково работает и для заготовки, и для идущего спектакля: уже сказанное не
    трогается, меняется только то, что будет дальше. Запрос применяется целиком либо
    отклоняется целиком — иначе ошибка в последнем участнике оставила бы предыдущих
    уже переименованными, а форму — с прежними именами.
    """
    if not isinstance(incoming, list):
        return "Нужен список participants"
    if not incoming:
        return "В составе должно остаться хотя бы одно место"
    for raw in incoming:
        if not isinstance(raw, dict):
            return "Каждый участник должен быть объектом"

    cast = session.runtime_participants
    by_id = {p.get("cast_id"): p for p in cast if p.get("cast_id")}

    # Имена, эмодзи и профессии, занятые тем, что прислал пульт: по ним выбираются
    # свободные значения для новых мест
    used = empty_used()
    for raw in incoming:
        used["names"].add(str(raw.get("display_name", "") or "").strip())
        used["emojis"].add(str(raw.get("avatar_emoji", "") or ""))
        keywords = str(raw.get("avatar_keywords", "") or "")
        used["keywords"].add(keywords)
        if keywords.strip():
            used["professions"].add(keywords.rsplit(" ", 1)[0])

    updates = []
    for raw in incoming:
        entry = by_id.get(str(raw.get("cast_id") or ""))
        name = str(raw.get("display_name", "") or "").strip()
        gender = str(raw.get("gender", (entry or {}).get("gender", "male")) or "male")
        if gender not in ("male", "female"):
            return f"{name or 'Новое место'}: пол может быть только «male» или «female»"
        if not name:
            if entry:
                # У места на сцене имя уже есть, и пустое поле в пульте — это
                # скорее стёртая строка, чем желание остаться без имени
                return "У всех участников должны быть имена"
            # А новое место имени ещё не имеет: придумываем его так же,
            # как при подъёме занавеса
            name = pick_name(gender, used["names"])
            used["names"].add(name)

        # Роль — это место в очереди, а не два флага: судья и модератор друг друга
        # исключают, поэтому роль ставится целиком
        role = raw.get("role", cast_role(entry) if entry else "participant")
        role = str(role or "participant")
        if role not in CAST_ROLES:
            return f"{name}: роль может быть участник, модератор или судья"

        # Опции роли: из пульта приходят только разрешённые значения, поэтому
        # чужое — это уже расхождение клиента с сервером, и лучше сказать о нём
        # вслух, чем тихо поставить своё
        sent_options = raw.get("role_options")
        if sent_options is not None:
            if not isinstance(sent_options, dict):
                return f"{name}: опции роли должны быть набором значений"
            for key, value in sent_options.items():
                allowed = (ROLE_OPTIONS.get(role) or {}).get(key)
                if allowed is None:
                    # Опция от прежней роли: место только что сменило роль, и требовать
                    # от клиента идеально чистых полей было бы ловушкой — такая
                    # опция просто не ставится
                    continue
                if value not in allowed:
                    return f"{name}: «{key}» может быть " + " или ".join(allowed)

        # Модель меняется в обе стороны: место можно сделать живым участником
        # (модель «human») и вернуть ему модель обратно. Пустое поле — «как было»:
        # у живого места модель не теряется, а опечатка в чистом поле не делает
        # участника человеком
        model = str(raw.get("model", (entry or {}).get("model", "")) or "").strip()
        if not model:
            model = (entry or {}).get("model", "") or ""

        keywords = str(raw.get("avatar_keywords", "") or "")
        emoji = str(raw.get("avatar_emoji", "") or "")
        if not entry:
            if not keywords:
                keywords = pick_keywords(gender, used)
                used["keywords"].add(keywords)
            if not emoji:
                emoji = pick_emoji(gender, used["emojis"])
                used["emojis"].add(emoji)

        # Режим размышлений и «характер» — не числа Ollama, а наши поля
        think = raw.get("think", (entry or {}).get("think", "auto"))
        think = str(think or "auto")
        if think not in settings.THINK_MODES:
            return f"{name}: размышления могут быть auto, on или off"
        preset = raw.get("preset", (entry or {}).get("preset", ""))
        preset = preset if isinstance(preset, str) else ""

        # Параметры генерации разбираем здесь же: запрос применяется целиком либо
        # отклоняется целиком, иначе ошибка у последнего участника оставила бы
        # предыдущих уже переименованными, а форма - с прежними именами.
        options = {}
        for key in settings.PER_PARTICIPANT_OPTION_KEYS:
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
            if key == "top_k" and number < 1:
                # С нулём или отрицательным значением Ollama отклонит весь запрос
                return f"{name}: «top_k» должен быть не меньше 1"
            options[key] = number

        updates.append({"entry": entry, "name": name, "gender": gender,
                        "model": model, "options": options, "think": think,
                        "preset": preset, "role": role, "keywords": keywords,
                        "emoji": emoji, "role_options": sent_options,
                        "avatar_url": raw.get("avatar_url") or None,
                        "has_avatar_url": "avatar_url" in raw})

    names = [u["name"] for u in updates]
    if len(set(names)) != len(names):
        return "Имена участников должны быть разными"

    # Модели можно менять на ходу: проверяем все сразу, до применения.
    # Недоступная Ollama не мешает сохранить состав - модели ещё проверятся на старте.
    models = sorted({u["model"] for u in updates if u["model"] and u["model"] != "human"})
    models_status = ollama_api.check_models_available(models, force=True)
    if models_status["missing"] and not models_status["error"]:
        return ollama_api.models_problem_message(models_status)
    if models_status["error"]:
        print(f"⚠️  Модели не проверены ({models_status['error']}), состав сохраняю как есть")

    old_models = {p.get("model", "") for p in cast if p.get("model")}
    busy = session.current_action == "thinking"

    new_cast = []
    for u in updates:
        entry = u["entry"]
        was_role = cast_role(entry) if entry else "participant"
        if entry is None:
            # Новое место: пульт присылает его целиком (имя, эмодзи, профессию,
            # модель придумал сервер, когда пульт попросил заготовку)
            entry = {
                "cast_id": uuid.uuid4().hex[:8],
                "model": u["model"],
                "display_name": u["name"],
                "avatar_keywords": u["keywords"],
                "avatar_emoji": u["emoji"] or "📣",
                "avatar_url": u["avatar_url"],
                "gender": u["gender"],
                "instruction": settings.DEFAULT_JUDGE_INSTRUCTION if u["role"] == "judge" else "",
            }
        else:
            # Место уже играло: имя меняется вместе с историей для промптов,
            # иначе модель считала бы прошлые реплики чужими
            if u["name"] != entry.get("display_name"):
                session.rename_participant(entry.get("display_name", ""), u["name"])
                entry["display_name"] = u["name"]
            entry["gender"] = u["gender"]
            # Модель ставится и на новую, и на «human»: живое место — это место
            # с моделью «human», и никакого отдельного способа «стать человеком»
            # у пульта нет
            if u["model"]:
                entry["model"] = u["model"]
            if u["keywords"]:
                entry["avatar_keywords"] = u["keywords"]
            if u["emoji"]:
                entry["avatar_emoji"] = u["emoji"]
            if u["has_avatar_url"]:
                entry["avatar_url"] = u["avatar_url"]
        new_cast.append(entry)

        # Роль меняется целиком: бывший судья, став модератором, судьёй быть перестаёт
        set_cast_role(entry, u["role"])
        # Опции роли — её собственный набор: у судьи он есть, у остальных ролей
        # поле убирается, чтобы место не принесло старые опции в новую роль
        apply_role_options(entry, u["role_options"])
        # Правила судьи ходят вместе с ролью — но только те, что поставил сам
        # сервер вместе с ролью: инструкцию, написанную руками, роль не трогает
        if was_role != u["role"]:
            own = str(entry.get("instruction", "") or "")
            if u["role"] == "judge" and not own.strip():
                entry["instruction"] = settings.DEFAULT_JUDGE_INSTRUCTION
            elif was_role == "judge" \
                    and own.strip() == settings.DEFAULT_JUDGE_INSTRUCTION.strip():
                # Место перестало быть судьёй: правила судьи в другой роли —
                # это уже не правило роли, а чужой текст в чужом промпте
                entry["instruction"] = ""

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

    session.runtime_participants = new_cast
    # Сцена — это и есть порядок, роли и числа: она переживёт «Новый спектакль»
    session.scene = scene_from_cast(new_cast)
    session.sync_cast_media()

    # Модели заменённых и убранных мест этому спектаклю больше не нужны —
    # освобождаем память (но не ту, что прямо сейчас считает реплику)
    if not busy:
        still_used = {p.get("model", "") for p in new_cast if p.get("model")}
        for model in sorted(old_models - still_used):
            if model and model != "human":
                ollama_api.unload_model(model)

    return ""


# Глобальный экземпляр сессии
session = DebateSession()

# Правила судьи и сцену из прошлых запусков читает тот, кто запускает сервер
# (см. web.main): читать их при импорте нельзя — в момент импорта ещё не известно,
# на каком порту играет этот экземпляр, а значит и в какой папке лежат файлы
# (см. settings.prepare_instance). Иначе первое же чтение смотрело бы в корень
# проекта и сцена казалась бы потерянной

# Состав заготовлен заранее: режиссёрский пульт правит его и до старта спектакля
session.load_new_cast()


def run_debate_thread(topic: str, on_post=None, on_draft=None):
    """
    Играет спектакль акт за актом, пока режиссёр не опустит занавес.

    on_post — как сообщить о новой реплике (обычно это Socket.IO из web.py).
    on_draft — как сообщить о том, что реплика пишется прямо сейчас: пока
    облачная модель говорит, в ленте растёт черновик (см. _StreamingReply).
    Здесь про Flask не знают: спектакль просто отдаёт готовый пост тому, кто
    его позвал.
    """
    print(f"🎬 Поток дебатов запущен для темы: {topic}")
    # Размышления ходов уйдут в стенограмму: после занавеса ленты уже не будет
    start_thinking_log(topic)
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
            
            # Состав перечитываем на каждом акте: режиссёр мог убрать место или
            # добавить новое прямо на ходу, и этот акт играется уже новым составом
            act_cast = list(session.runtime_participants)
            print(f"  🎭 Персонажи: {[p.get('display_name', '') + ' (' + p.get('model', '') + ')' for p in act_cast]}")
            
            for participant in act_cast:
                # Убранный участник на сцену не выходит: слепок act_cast его ещё
                # держит, а в составе его уже нет
                if not still_in_cast(participant, session.runtime_participants):
                    print(f"  ⤵️  {participant.get('display_name', '')} убран из состава - пропускаю")
                    continue
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
                        
                        # Живое место могли убрать из состава, пока оно ждало
                        # реплики: без этой проверки спектакль ждал бы его вечно
                        if not still_in_cast(participant, session.runtime_participants):
                            print(f"\n⤵️  {participant.get('display_name', '')} убран из состава")
                            session.waiting_for_human = False
                            session.current_action = None
                            break
                        
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
                                    is_judge=participant.get("is_judge", False),
                                    gender=participant.get("gender", "male")
                                )
                                print(f"🎬 {participant.get('display_name', '')}: {current_message[:50]}")
                                if on_post:
                                    on_post(post)
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
                    ollama_api.unload_other_show_models(participant.get("model", ""), show_model_names())
                    
                    session.current_action = "thinking"
                    
                    session.handle_ai_turn(participant, round_num, on_draft=on_draft)
                    
                    post = session.posts[-1] if session.posts else None
                    if post and on_post:
                        on_post(post)
                    
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
        
        if settings.UNLOAD_AFTER_DEBATE:
            for model in show_model_names():
                ollama_api.unload_model(model)
