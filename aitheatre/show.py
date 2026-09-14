"""Спектакль: состав, характеры, сессия дебатов и поток, который их играет.

Здесь живёт единственный источник правды о труппе — объект `session`. Пульт,
сервер и поток спектакля работают с ним одним и тем же, поэтому правки на ходу
видны всем, а уже опубликованные реплики не переписываются.

Обратная зависимость только одна: сессия просит Ollama (ask_model) и получает
готовый текст. Наружу спектакль отдаёт посты через колбэк on_post — так этот
модуль ничего не знает про Flask и Socket.IO.
"""

import json
import random
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import ollama_api
from . import settings
from . import text

def load_theatre_settings():
    """
    Возвращает правила судьи и сцену, сохранённые в прошлых запусках: имена
    участников каждый спектакль новые, а это — режиссёрские настройки.
    """
    try:
        if settings.SETTINGS_FILE.exists():
            data = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            rules = data.get("judge_rules")
            if isinstance(rules, list) and rules:
                session.judge_rules = [str(r) for r in rules]
                print(f"⚖️  Загружены сохранённые правила судьи: {len(session.judge_rules)} пунктов")
            scene = sanitize_scene(data.get("scene"))
            if scene:
                session.scene = scene
                print(f"🎬 Загружена сохранённая сцена: мест {len(scene)}")
    except Exception as e:
        print(f"  ⚠️  Не читается {settings.SETTINGS_FILE.name}: {e}")

def save_theatre_settings():
    """Сохраняет правила судьи и сцену рядом с проектом (файл в .gitignore)."""
    try:
        settings.SETTINGS_FILE.write_text(
            json.dumps({"judge_rules": session.judge_rules, "scene": session.scene or []},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Не сохраняется {settings.SETTINGS_FILE.name}: {e}")

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
        "content_html": text.markdown_to_html(content),
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
        self.judge_rules = list(settings.DEFAULT_JUDGE_RULES)
        self.moderator_guidelines = []
        self.waiting_for_human = False
        self.moderator_message = None
        self.moderator_finished = False
        self.runtime_participants = []
        self.conversation_history = []
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
        """«Новый спектакль»: новые имена и характеры, настройки и сцена — те же."""
        guidelines = list(self.moderator_guidelines)
        judge_rules = list(self.judge_rules)
        static_instructions = list(self.static_instructions)
        # Сцена — то, что режиссёр настроил руками, поэтому её и переживает:
        # иначе «Новый спектакль» возвращал бы состав из файла, а не тот,
        # который только что собрали в пульте
        scene = [dict(place) for place in self.scene] if self.scene else None
        self.__init__()
        self.moderator_guidelines = guidelines
        self.judge_rules = judge_rules
        self.static_instructions = static_instructions
        self.scene = scene
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
            trimmed = text.trim_history_by_tokens(history_messages, text.estimate_tokens(system_prompt))
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
        trimmed = text.trim_history_by_tokens(history_messages, text.estimate_tokens(system_prompt))
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

        response, search_count, search_queries = ollama_api.ask_model(
            model=participant["model"],
            messages=messages,
            participant_name=participant["display_name"],
            options=ollama_api._merge_options(participant),
            think=ollama_api.resolve_think(participant),
        )

        self.add_post(
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
        "instruction": settings.DEFAULT_JUDGE_INSTRUCTION if role == "judge" else "",
    }
    set_cast_role(entry, role)

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
    Сцена: места состава без имён, аватаров и личных инструкций.

    Имена каждый спектакль новые, а вот роли, модели, порядок в очереди реплик и
    числа генерации режиссёр настраивает один раз — они и переживают и «Новый
    спектакль», и перезапуск приложения.
    """
    scene = []
    for participant in cast or []:
        place = {"model": participant.get("model", ""), "role": cast_role(participant)}
        for key in settings.PER_PARTICIPANT_OPTION_KEYS:
            if participant.get(key) is not None:
                place[key] = participant[key]
        if participant.get("think") in settings.THINK_MODES:
            place["think"] = participant["think"]
        if participant.get("preset"):
            place["preset"] = str(participant["preset"])[:32]
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
        for key in settings.PER_PARTICIPANT_OPTION_KEYS:
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                place[key] = value
        if item.get("think") in settings.THINK_MODES:
            place["think"] = item["think"]
        if item.get("preset") in settings.CHARACTER_PRESETS:
            place["preset"] = item["preset"]
        scene.append(place)
    return scene


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

        model = str(raw.get("model", (entry or {}).get("model", "")) or "").strip()
        if entry and entry.get("model") == "human":
            # Живое место человеком и остаётся: он говорит сам, модель ему не подсунуть
            model = "human"

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
                        "emoji": emoji,
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
            if entry.get("model") != "human" and u["model"]:
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

# Правила судьи из прошлых запусков (редактируются на ходу режиссёра)
load_theatre_settings()

# Состав заготовлен заранее: режиссёрский пульт правит его и до старта спектакля
session.load_new_cast()


def run_debate_thread(topic: str, on_post=None):
    """
    Играет спектакль акт за актом, пока режиссёр не опустит занавес.

    on_post — как сообщить о новой реплике (обычно это Socket.IO из web.py).
    Здесь про Flask не знают: спектакль просто отдаёт готовый пост тому, кто
    его позвал.
    """
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
                    
                    session.handle_ai_turn(participant, round_num)
                    
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
