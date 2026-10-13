"""Спектакль: состав, характеры, сессия дебатов и поток, который их играет.

Здесь живёт единственный источник правды о труппе — объект `session`. Пульт,
сервер и поток спектакля работают с ним одним и тем же, поэтому правки на ходу
видны всем, а уже опубликованные реплики не переписываются.

Обратная зависимость только одна: сессия просит Ollama (ask_model) и получает
готовый текст. Наружу спектакль отдаёт посты через колбэк on_post — так этот
модуль ничего не знает про Flask и Socket.IO.
"""

import base64
import copy
import hashlib
import json
import random
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import cloud
from . import ollama_api
from . import pictures
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
            # Приложенное к теме — режиссёрская настройка, как и сама тема:
            # тема переживает и перезапуск, и архив, и картинка темы — вместе с ней
            session.topic_attachments = _topic_attachments_from(data)
            if session.topic_attachments:
                print(f"🖼  К теме приложено картинок: {len(session.topic_attachments)}")
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


def _topic_attachments_from(data: dict) -> list:
    """Приложенное к теме из файла пульта — вместе с отметкой, когда его показали.

    Адрес приходит из файла, который правят и руками, поэтому проверяется тем же
    сторожем, что и приложение из формы (см. upload_files): по адресу нельзя
    прочитать что попало, а пропавший файл просто не возвращается.

    `since` — сколько реплик было в ленте, когда картинку приложили: по этой
    отметке после возврата спектакля видно, кто её уже видел (см.
    restore_shown_pictures). Врать ею можно в обе стороны: слишком поздняя
    отметка показала бы картинку тому, кто её видел и заплатил за неё, слишком
    ранняя — спрятала бы её от того, кто не видел. Поэтому отметка берётся
    из файла, но читается как число, а не как чужой текст (см. _as_int).
    """
    taken = upload_files(data.get("topic_attachments") or [])
    since = {}
    for one in data.get("topic_attachments") or []:
        if isinstance(one, dict):
            since[str(one.get("url") or "")] = _as_int(one.get("since"))
    for one in taken:
        one["since"] = since.get(str(one.get("url") or ""), 0)
    return taken


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


# Отключённое правило остаётся в списке, но помечено: « ⏸ » впереди значит
# «пока не работает». Удалять правило ради одной паузы не приходится: оно ждёт
# здесь, пока его включат обратно (см. page.ruleIsOff), а спектаклю в это время
# не говорится (см. rules_in_force). Пометка живёт прямо в тексте правила,
# поэтому переживает и файл настроек, и архив: хранилище как было списком строк,
# так и осталось, и чужая старая запись читается как есть.
RULE_OFF_MARK = "⏸"


def rule_is_off(line) -> bool:
    """Выключено ли правило — по пометке впереди его текста."""
    return str(line or "").strip().startswith(RULE_OFF_MARK)


def rule_text(line) -> str:
    """Текст правила без пометки «выключено» — то, что видит режиссёр и модель."""
    written = str(line or "").strip()
    return written[len(RULE_OFF_MARK):].strip() if rule_is_off(written) else written


def rules_in_force(lines) -> list:
    """Только работающие правила: выключенные спектаклю не говорятся.

    Пометку снимаем по дороге — в промпт уезжает само правило, а не служебный
    знак (см. RULE_OFF_MARK). Пауза при этом не удаление: правило остаётся
    в списке и в редакторе, а снятие пометки возвращает его на сцену целиком,
    с тем же текстом, что был.
    """
    return [rule_text(line) for line in lines or []
            if str(line or "").strip() and not rule_is_off(line)]


# ── СЛЕД ЗАПИСИ ПУЛЬТА ─────────────────────────────────────────────────────
#
# Каждая запись настроек на диск оставляет о себе след: что именно записано,
# когда и сколько весит файл. Нужен он пульту: режиссёр нажимает «Применить» и
# хочет знать, что файл правда записан, а не верить в это (см. web.saved_payload,
# page.noteSettingsWrite). След один на процесс, поэтому о записи узнаёт и та
# вкладка, которая её не делала: он едет вместе с состоянием спектакля.
_last_settings_write = {}


def last_settings_write() -> dict:
    """Последняя запись пульта: {stamp, at, note, file, size}.

    Копия, а не сам словарь: читатель не должен править след у театра.
    """
    return dict(_last_settings_write)


def _note_settings_write(note: str) -> dict:
    """Отметить запись пульта: что записано, во сколько и сколько весит файл.

    Знак (stamp) растёт с каждой записью: по нему страница понимает, что запись
    новая, а не та же самая, о которой она уже сказала.
    """
    global _last_settings_write
    try:
        size = settings.SETTINGS_FILE.stat().st_size
    except OSError:
        size = 0
    _last_settings_write = {
        "stamp": int(_last_settings_write.get("stamp", 0)) + 1,
        "at": time.strftime("%H:%M:%S"),
        "note": str(note or "пульт"),
        "file": settings.SETTINGS_FILE.name,
        "size": size,
    }
    return dict(_last_settings_write)


def save_theatre_settings(note: str = "пульт"):
    """Сохраняет пульт целиком рядом с проектом (файл в .gitignore).

    Состав сохраняется вместе с именами и личными инструкциями: режиссёр,
    вернувшись к театру после перезапуска, хочет видеть прежний спектакль.
    История при этом начинается с нуля — посты и снимки ходов живут в памяти.

    note — что именно изменилось («тема», «состав», «старт спектакля»): это
    уходит в след записи, и пульт говорит об этом режиссёру словами, а не
    молчанием (см. _note_settings_write). Не записалось — следа не оставляем:
    обещать запись, которой не было, нельзя.
    """
    try:
        settings.SETTINGS_FILE.write_text(
            json.dumps({
                "version": 2,
                # Тема тоже режиссёрская настройка: придумывать её заново каждый
                # запуск — работа, а не часть спектакля
                "topic": session.topic or "",
                # Приложенное к теме лежит рядом с самой темой: у каждой картинки
                # своё `since` — сколько реплик было в ленте, когда её приложили
                # (см. _topic_attachments_from)
                "topic_attachments": [dict(one) for one in session.topic_attachments or []],
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
        return None
    return _note_settings_write(note)


def forget_theatre_settings(note: str = "пульт записан заново"):
    """Забыть сохранённый пульт, оставив тему.

    Нужен полному сбросу: если файл оставить как есть, следующий запуск вернул бы
    то, от чего режиссёр только что отказался. Удалить файл целиком нельзя —
    вместе с ним ушла бы тема, а её не сбрасывает даже полный сброс (см.
    reset_to_defaults): тема — про сюжет, а не про труппу, и в settings.py её нет.
    Поэтому в файле остаётся одна тема — и приложенная к ней картинка: она часть
    темы, а не труппы, — а состав и правила следующего запуска соберутся заново
    из settings.py, ровно как при первом запуске.
    """
    try:
        settings.SETTINGS_FILE.write_text(
            json.dumps({"version": 2, "topic": session.topic or "",
                        "topic_attachments": [dict(one) for one
                                              in session.topic_attachments or []]},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError as e:
        print(f"  ⚠️  Не сохраняется {settings.SETTINGS_FILE.name}: {e}")
        return None
    return _note_settings_write(note)


# ── ДАМП СПЕКТАКЛЯ ─────────────────────────────────────────────────────────
#
# Раньше подробности хода лежали в двух местах и в двух видах: размышления
# моделей — в стенограмме, которая росла от спектакля к спектаклю, а снимок
# запроса («что вошло в запрос к модели») — только в памяти и только у последних
# ходов.
# Нужны они с одной целью: разобраться, что произошло в последнем спектакле.
# Поэтому теперь это один файл — и в нём один спектакль.

# Как называть окно, которым мерили историю: то же слово, что и в ленте
WINDOW_NAMES = {"cloud": "окно облака", "local": "окно модели",
                "unbounded": "окно не ограничено (CLOUD_NUM_CTX = 0)"}

# Обратно словам: по этому словару прочитанное в файле окно становится тем же
# «чьим-то», каким было на ходу (см. parse_dump)
WINDOW_KINDS = {name: kind for kind, name in WINDOW_NAMES.items()}


def numbers_word(value) -> str:
    """Число с разрядами: «23 318» читается, а «23318» — уже нет."""
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value if value is not None else "—")


def duration_words(seconds) -> str:
    """Сколько длился ход — словами: «2 мин 15 с» читается, а «135.4 с» — нет.

    Секунды до десятых в тексте не нужны: в ДАМПе и в посте это мера
    ожидания, а не измерение. Минуты без остатка называются минутами,
    чтобы не появлялось «2 мин 0 с».
    """
    try:
        total = int(round(float(seconds or 0)))
    except (TypeError, ValueError):
        return "—"
    minutes, secs = divmod(max(0, total), 60)
    if minutes and secs:
        return f"{minutes} мин {secs} с"
    if minutes:
        return f"{minutes} мин"
    return f"{secs} с"


def quoted(text: str) -> str:
    """Текст куском: каждая строка с «> », чтобы границы были видны в файле.

    Фенсы (```) для этого не годятся: модели сами пишут их в ответах, и такой
    блок закрывался бы посреди реплики.
    """
    lines = (text or "").splitlines()
    return "\n".join(f"> {line}" if line.strip() else ">" for line in lines) or ">"


def clock(when) -> str:
    """Время события с тысячными долями — по хронологии видно, кто кого ждал.

    До микросекунд не нужны: ход длится секунды и минуты, а разница между
    двумя соседними событиями — это доли секунды, которых хватает, чтобы
    отличить «модель думала две минуты» от «две минуты шёл поиск».
    """
    try:
        moment = float(when)
    except (TypeError, ValueError):
        return ""
    return (time.strftime("%H:%M:%S", time.localtime(moment))
            + f".{int(moment % 1 * 1000):03d}")


def step_period(step: dict) -> str:
    """Когда событие началось и когда кончилось — одной строкой.

    Время берётся из самих часов события, а если оно уже записано словами —
    из записи (см. parse_dump): у прочитанного хода тех часов уже нет, а время
    событий осталось, и терять его незачем.
    """
    start = step.get("clock") or clock(step.get("t"))
    end = step.get("clock_end") or (clock(step.get("t_end")) if step.get("t_end") else "")
    if start and end and end != start:
        return f"{start} → {end}"
    return start


def ask_line(step: dict) -> str:
    """Один запрос хода одной строкой — с числами и их названиями.

    Именно здесь закрывается вопрос «что значит 3431 + 1246»: **ввод** — это то,
    что вошло в запрос (системный промпт, история, найденное), **вывод** — то, что вендор
    вернул, включая оплаченные размышления, которые в реплику не попадают.
    Рядом с числом вендора стоит наш собственный счёт (тот же счётчик, каким
    театр мерит историю): вендор считает своими токенами, поэтому числа и
    расходятся — и об этом лучше знать, чем удивляться разнице.

    Если же вендор чисел не прислал вовсе, остаётся только наш счёт: без него
    на месте ввода была бы дыра, а «сколько вошло в запрос» — первое, что хочется
    знать.
    """
    if step.get("error"):
        return f"не прошёл — {step['error']}"
    estimate = step.get("tokens_in_est")
    if step.get("tokens_in") is None and step.get("tokens_out") is None:
        weight = "числа токенов вендор не сообщил"
        if estimate:
            # Тут «→» не нужен: сравнивать не с чем, вендорских чисел нет
            weight += f" · наш счёт ≈{numbers_word(estimate)} токенов"
    else:
        weight = f"ввод {numbers_word(step.get('tokens_in'))}"
        if estimate:
            weight += f" (наш счёт ≈{numbers_word(estimate)})"
        weight += f" → вывод {numbers_word(step.get('tokens_out'))} токенов"
    parts = [weight]
    if step.get("reasoning_tokens"):
        parts.append(f"из них размышлений {numbers_word(step['reasoning_tokens'])}")
    if step.get("finish_reason"):
        # Код ответа без перевода читается как код: рядом с ним — его смысл
        meaning = finish_meaning(step["finish_reason"])
        parts.append(f"конец: {step['finish_reason']}" + (f" ({meaning})" if meaning else ""))
    # Слова — как на странице (см. turnAskText): файл и лента рассказывают
    # об одном запросе, и «инструмент отправлен» против «с инструментом»
    # читалось бы как два разных запроса
    parts.append("с инструментом поиска" if step.get("tools")
                 else "без инструмента поиска")
    # Соединитель — тот же, что на странице (см. turnAskText в page.py): один
    # и тот же ход читают в файле и в ленте, и «→ вывод» в одном месте против
    # «· вывод» в другом значило бы, что это два разных числа
    return " · ".join(parts)


# Каким значком помечать строку хронологии: шаг — он и есть шаг, а вот заминки
# и отказы должны отличаться от обычного запроса с первого взгляда
STEP_MARKS = {"refused": "⛔", "force": "🔍", "silence": "⚠️", "note": "·",
              "money": "💰"}

# Обратно значкам: по значку строка хронологии снова становится событием
# с его видом (см. parse_dump). Заминки от обычных записей отличает именно вид
STEP_KINDS = {mark.replace("\ufe0f", ""): kind for kind, mark in STEP_MARKS.items()}


def money(value) -> str:
    """Рубли с копейками: «2,46 ₽» — как на ценнике, а не «2.46 RUB».

    Прочерк вместо выдуманного числа: если шлюз остатка не дал, цены у нас нет,
    и подставлять ноль значило бы сказать «бесплатно».
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "— ₽"
    text = f"{number:,.2f}".replace(",", "\u00a0").replace(".", ",")
    return f"{text} ₽"


def step_markdown(step: dict, with_text: bool = True) -> str:
    """Одно событие хода — строкой. ДАМП и строится по событию за раз.

    with_text=False — только шапка события: так пишутся размышления, текст
    которых льётся в файл по кускам, в тот момент как приходит (см.
    dump_step_sink).
    """
    period = step_period(step)
    head = f"- {period} · " if period else "- "
    kind = step.get("kind")
    if kind == "ask":
        return f"{head}**запрос {step.get('n')}** — {ask_line(step)}"
    if kind == "search":
        line = (f"{head}🔍 **поиск {step.get('n')}** (лимит {step.get('limit')}): "
                f"«{step.get('query')}» — принесено "
                f"{numbers_word(step.get('tokens'))} токенов")
        return line + "\n" + quoted(step.get("results")) if with_text else line
    if kind == "thought":
        line = (f"{head}💭 **размышления** (к запросу {step.get('n')}) — "
                f"{numbers_word(step.get('tokens'))} токенов, в реплику не попадают")
        return line + "\n" + quoted(step.get("text")) if with_text else line
    mark = STEP_MARKS.get(kind, "·")
    return f"{head}{mark} {step.get('text')}"


def window_line(budget: dict) -> str:
    """Строка об окне говорящего — и куда делось остальное.

    Четыре числа здесь — не украшение, а честный ответ на «куда делись токены»:
    окно целиком, сколько из него оставлено модели на ответ (у облака запас
    берётся из CLOUD_MAX_TOKENS, а если он не задан — из олламовского предела
    ответа, см. settings.context_budget), технический запас на неточность
    счёта и сколько после всего этого оставалось истории.
    """
    available = ("без предела" if budget.get("available") is None
                 else numbers_word(budget.get("available")))
    # Ноль в запасе — не «ноль токенов на ответ», а «ответ не ограничиваем»:
    # у облака CLOUD_MAX_TOKENS = 0 именно это и значит (см. settings),
    # и писать «0 оставлено на ответ модели» значило бы врать противоположным
    reserve = int(budget.get("reserve") or 0)
    answer = (f"{numbers_word(reserve)} оставлено на ответ модели" if reserve > 0
              else "на ответ ничего не зарезервировано (CLOUD_MAX_TOKENS = 0 — ответ не ограничиваем)")
    return (f"**Окно говорящего:** {WINDOW_NAMES.get(budget.get('kind'), 'окно модели')} "
            f"{numbers_word(budget.get('window'))} токенов целиком — "
            f"{answer}, "
            f"{numbers_word(budget.get('safety'))} — технический запас, "
            f"на историю оставалось {available}\n")


def history_line(summary: dict, budget: dict) -> str:
    """Строка о том, что легло в запрос — у каждого числа своё имя.

    «Уехало» ни о чём не говорило: ни кто уехал, ни куда — и рядом стояло
    «из 1», которое считало другое (только сцену до обрезки). Теперь сказано,
    что именно отправлено: показ запроса к модели — по частям, каждая со своим
    весом (см. build_turn_report).
    """
    scene = int(budget.get("messages_after") or 0)
    tasks = max(0, int(summary.get("messages") or 0) - 1 - scene)
    line = (f"**Запрос к модели состоял из:** {summary.get('messages')} сообщ. "
            f"({numbers_word(summary.get('tokens'))} токенов) — системный промпт "
            f"{numbers_word(budget.get('system_tokens'))} токенов, из сцены {scene} "
            f"сообщ. ({numbers_word(budget.get('kept_tokens'))} токенов)")
    if tasks:
        line += f", и ещё {tasks} — задания хода"
    if summary.get("removed_messages"):
        # Без хвоста «самое раннее перечислено ниже»: оно и так ниже, и сказано
        # это ровно теми же словами, что в ленте (см. placeHtml в page.py)
        line += (f". Обрезка выбросила {summary.get('removed_messages')} сообщ. "
                 f"({numbers_word(summary.get('removed_tokens'))} токенов)")
    else:
        line += ". Обрезка ничего не тронула: сцена влезла в окно целиком"
    return line + "\n"


# Имена разделов отчёта о ходе — те же слова, что на странице (см. turnBlock
# в page.py). Отчёт один: его читают и в ДАМПе, и в ленте, и названия разделов
# в двух местах не должны расходиться, иначе непонятно, один это раздел или два.
#
# Порядок ключей — тот же, в каком разделы идут на странице (см. turnBodyHtml):
# по файлу и по ленте читают одно и то же, и разойтись в порядке значило бы
# заставить читателя собирать картину заново в каждом месте.
#
# Назначение разделов словами есть только на странице: в файле разделяет уже
# сам заголовок, и повторять под ним пояснение было бы шумом.
TURN_SECTIONS = {
    "who": "### 👤 Кто говорит и когда",
    "place": "### 📐 Место под историю: чьё окно и как оно заполнилось",
    "history": "### 🧭 Хронология хода: что происходило по порядку",
    "sketch": "### 🌱 Сказано раньше: прежняя версия реплики",
    "cut": "### ✂️ Что выбросила обрезка",
    "added": "### ✍️ Что приложение дописало в запрос",
    "request": "### 📨 Первый запрос к модели целиком",
    "answer": "### 💬 Реплика, которой ход кончился",
}

# Раздел по его заголовку — обратный словарь: по нему ДАМП читается назад
# (см. parse_dump). Он же и сторож: заголовки разделов в файле и в этом словаре
# разойтись не могут, потому что словарь один
TURN_SECTION_KEYS = {title: key for key, title in TURN_SECTIONS.items()}

# Как назвать роль сообщения словами: сами роли (system/user/assistant/tool) —
# это протокол, а в отчёте их читают глазами. Те же слова, что на странице
# (см. ROLE_WORDS в page.py)
ROLE_WORDS = {
    "system": "системный промпт",
    "user": "реплика в запросе",
    "assistant": "сказано самой моделью",
    "tool": "результат поиска",
}

# Значки ролей в ленте — те же, что у постов (см. create_post): иначе значок
# одной роли значил бы в файле и в ленте разное
ROLE_ICONS = {"participant": "🎭", "moderator": "🎬", "judge": "⚖️"}

# Что на странице говорят о наброске: это не размышления, а прежняя версия
# реплики, и без пояснения её читают как черновик мыслей (см. thinkingBlockHtml)
SKETCH_HINT = "Так бывает, когда модель сначала отвечает, а потом её просят поискать."

# Сколько строк длинного текста видно до раскрытия — в файле решать нечего,
# но подпись к наброску у страницы и файла общая (см. dump_turn_tail)

# Формат записи ДАМПа. Читается первым делом при чтении файла: по нему новый
# процесс решает, можно ли вернуть прежний спектакль (см. parse_dump). Меняется
# вместе с тем, как запись устроена.
#
# Под-версии: пока меняется только то, ЧТО в записи лежит (лицо участника
# переехало из реплики в состав), прошлый номер остаётся читаемым и живёт
# в списке ниже. Как только меняется сама запись — разделы, шапка, порядок, —
# номер уходит из списка, и тогда о расхождении спрашивают вслух
# (см. ask_about_dump): прочитать чужую запись можно, но обещать этого нельзя
# А седьмой номер — про переигровку: в хронологии появилась строка
# «**Переиграны реплики:** N», и без неё читающий увидел бы в спектакле и прежний
# ответ, и новый (см. dump_replayed). Прежние номера читаются как есть: строки
# такой в них просто нет, и спектакль в них один-единственный
DUMP_FORMAT = 7
DUMP_FORMATS_READABLE = (7, 6, 5, 4, 3)

# Что отвечают на тот вопрос: сцена пустая, прежний спектакль только на чтение
# или он же — с продолжением (см. load_play_from_dump)
DUMP_CHOICES = (("ч", "read"), ("читать", "read"), ("r", "read"),
                ("п", "continue"), ("продолжить", "continue"), ("c", "continue"))


def turn_summary_line(summary: dict) -> str:
    """Сводка хода — те же числа и в том же порядке, что в свёрнутой строке ленты.

    На странице у каждой реплики сверху строка «🧾 ход реплики · запросов 3 ·
    поисков 5 · …», и решают, раскрывать ли блок, именно по ней. В файле такой
    строки не было вовсе: по ДАМПу нельзя было понять, что за ход внутри,
    не прочитав его целиком. Теперь шапка записи (см. turn_heading) — это она
    и есть, и считается тем же счётом, что на странице (см. turnSummaryParts
    в page.py).
    """
    summary = summary or {}
    parts = []
    # Ноль запросов — не факт о ходе, а «ещё неизвестно»: строка пишется
    # в тот миг, когда ход начался, и до первого запроса о ней сказать нечего
    if summary.get("asks"):
        parts.append(f"запросов {summary['asks']}")
    if summary.get("search_rounds"):
        parts.append(f"поисков {summary['search_rounds']}")
    if summary.get("thought_steps"):
        parts.append(f"размышлений {summary['thought_steps']}")
    # Ввод — по ВСЕМ запросам хода, когда их было несколько: поиск это ещё один
    # круг, и вся история уезжает к модели заново (см. _input_tokens_total)
    if (summary.get("asks") or 0) > 1 and summary.get("tokens_in_total"):
        parts.append(f"на ввод всего {numbers_word(summary['tokens_in_total'])} токенов")
    elif summary.get("tokens"):
        parts.append(f"{numbers_word(summary['tokens'])} токенов на ввод")
    if summary.get("seconds"):
        parts.append(f"⏱ {duration_words(summary['seconds'])}")
    if summary.get("spent"):
        parts.append(f"💰 {money(summary['spent'])}")
    if summary.get("removed_messages"):
        parts.append(f"выброшено {summary['removed_messages']}")
    if summary.get("search_refusals"):
        parts.append(f"поиск сверх лимита {summary['search_refusals']}")
    if summary.get("silences"):
        parts.append(f"⚠️ без ответа {summary['silences']}")
    return " · ".join(parts)


def report_words(report: dict) -> str:
    """Итог спектакля словами — тем же счётом, что показывает страница.

    Нужен консоли: занавес опускается и в ней, а «сколько сыграно и во что
    обошлось» — первое, что спрашивают у отыгранного спектакля
    (см. DebateSession.show_report, page.curtainReportHtml).
    """
    report = report or {}
    parts = [f"реплик {report.get('posts', 0)}",
             f"ходов {report.get('turns', 0)}",
             f"актов {len(report.get('acts') or [])}"]
    if report.get("again"):
        parts.append(f"переиграно реплик {report['again']}")
    if report.get("with_versions"):
        parts.append(f"с прежними словами {report['with_versions']}")
    if report.get("spent"):
        parts.append(f"💰 {money(report['spent'])}")
    return " · ".join(parts)


def report_acts_words(report: dict) -> str:
    """Цена спектакля по актам — второй строкой итога (см. report_words).

    Пусто у спектакля без цен: у реплик человека хода на счёте нет вовсе,
    и «акт 1 — 0 ₽» было бы не числом, а выдумкой.
    """
    return " · ".join(
        f"акт {act.get('round')} — {money(act.get('spent'))}"
        for act in (report or {}).get("acts") or [] if act.get("spent"))


def cast_field(emoji, name, gender) -> str:
    """«🦊 Элина ♀» — кто говорил, одной строкой: в ленте у поста то же рядом.

    И значок, и пол стоят вместе с именем (см. postHeaderHtml), а не порознь:
    по этой же строке реплика собирается назад (см. _parse_heading), поэтому
    порядок и разделитель здесь не украшение, а способ прочитать её обратно.
    """
    return " ".join(part for part in (str(emoji or "").strip(),
                                      str(name or "").strip(),
                                      gender_symbol(gender)) if part)


def turn_heading(post_id: int, turn: dict) -> str:
    """Шапка записи о ходе: кто, чем и сводка — как свёрнутая строка в ленте.

    Числа, известные только к концу хода (сколько он шёл и сколько стоил),
    сначала пишутся без них, а потом шапка переписывается (см. dump_fix_heading):
    ждать с записью нельзя — у оборванного хода не осталось бы следа.
    """
    who = turn.get("who") or {}
    head = [f"## {post_id}",
            str(who.get("time") or ""),
            cast_field(who.get("emoji"), who.get("name"), who.get("gender")),
            str(who.get("model") or ""),
            str(who.get("role_name") or who.get("role") or ""),
            f"Акт {who.get('round')}"]
    return " · ".join(head) + " · " + turn_summary_line(turn.get("summary") or {})


def who_line(turn: dict) -> str:
    """Кто говорит и когда — одна строка, теми же словами, что в ленте.

    Пол здесь не украшение: он виден у каждой реплики в ленте (♂/♀ рядом
    с именем), и та же реплика в файле должна читаться так же.
    """
    who = turn.get("who") or {}
    return (f"**Кто:** **{who.get('name') or ''}** {gender_symbol(who.get('gender'))} · "
            f"{who.get('model') or ''} · Акт {who.get('round')} · {who.get('time') or ''}\n")


def cast_line() -> str:
    """Состав одной строкой — лицо, роль, модель — для шапки ДАМПа.

    Лицо участника — свойство места, а не реплики: портрет и эмодзи правятся
    в пульте, и тогда лицо меняется у всех его реплик, включая уже сказанные
    (см. post_view). Поэтому лицо лежит в записи одно на спектакль, а не
    строкой «Аватар» в каждой реплике: в файле — то же, что на странице.

    Места разделены « ‖ », поля внутри места — « · »: одним «·» не обойтись,
    он уже стоит между полями (см. parse_cast_line).
    """
    places = []
    for p in session.runtime_participants:
        name = str(p.get("display_name") or "").strip()
        if not name:
            continue
        places.append(" · ".join([
            cast_field(p.get("avatar_emoji"), name, p.get("gender")),
            ROLE_NAMES.get(cast_role(p), "Участник"),
            str(p.get("model") or "—"),
            str(p.get("avatar_url") or "").strip() or "—",
        ]))
    return " ‖ ".join(places)


def participant_face(name: str) -> tuple:
    """Лицо участника из состава: портрет и эмодзи ("", "") — такого места нет."""
    name = str(name or "")
    if not name or name not in session.avatar_emojis:
        return "", ""
    return session.avatars.get(name), session.avatar_emojis.get(name, "")


def post_view(post: dict) -> dict:
    """Реплика для ленты: лицо берётся у состава, а не хранится в реплике.

    Аватар — свойство участника. Пока место стоит в пульте, его портрет и эмодзи
    и есть лицо всех его реплик: поменяли в составе и применили — сменилось
    и в новых, и в уже сказанных. У реплик того, кого в составе больше нет,
    остаётся лицо, с которым он говорил: это и есть та единственная память
    о портрете, ради которой его стоит хранить в посте.

    И у каждого приложенного — свои слова (см. attachment_view): что это за
    картинка сама по себе и какою она уехала тем, кому уехала. Файл на диске
    остаётся режиссёрским, а модель получает приведённый к обычному размеру, —
    и по этой записи видно, кому что досталось, не выпрашивая это у сервера
    отдельно на каждую отрисовку ленты.
    """
    url, emoji = participant_face(post.get("display_name"))
    attachments = attachment_views(post.get("attachments"))
    if not url and not emoji and not attachments:
        return post
    view = dict(post)
    if attachments:
        view["attachments"] = attachments
    if url or emoji:
        view["avatar_url"] = _avatar_that_exists(url) or None
        view["avatar_emoji"] = emoji or post.get("avatar_emoji") or "📣"
    return view


def attachment_views(attachments: list) -> list:
    """Приложенное к реплике — для страницы: имя, адрес и свои слова.

    Пустой список — приложенного нет вовсе. Тем же счётом живут и приложенное
    к теме (см. topic_picture_view), и картинки в записи хода (см.
    message_pictures): одна картинка — одни и те же слова о ней.
    """
    out = []
    for one in attachments or []:
        view = attachment_view(one)
        if view:
            out.append(view)
    return out


def attachment_view(one: dict) -> dict:
    """Приложенная картинка для страницы — или пусто, если адреса в ней нет.

    own — что это за картинка сама по себе: «jpeg 1920×2560 · 2,7 Мб». Есть
    всегда (вид и стороны читаются только из заголовка файла, см.
    pictures.picture_marks), и читается это сразу: про свою же картинку
    хочется знать не только после того, как её кто-то получил.

    sent — какою она уехала моделям и кому именно: появляется, когда она
    вправду уехала (см. picture_travellers, travelled_picture_words).
    """
    url = str((one or {}).get("url") or "").strip()
    if not url:
        return {}
    view = {"url": url,
            "name": str((one or {}).get("name") or "").strip()
                    or url.rsplit("/", 1)[-1]}
    own = own_picture_words(url)
    if own:
        view["own"] = own
    sent = sent_picture_words(url)
    if sent:
        view["sent"] = sent
        # И адрес той же картинки в том виде, в каком её получила модель:
        # в записи хода показывается именно она (см. sent_picture_url)
        view["sent_url"] = sent_picture_url(url)
    return view


def own_picture_words(url: str) -> str:
    """Что это за картинка сама по себе: «jpeg 1920×2560 · 2,7 Мб» (пусто — не прочитать).

    Читается из заголовка файла и помнится по имени файла и весу (см.
    _ATTACHED_MARKS): лента спрашивает об этом на каждой отрисовке, а картинка
    при этом не перечитывается.
    """
    key = _picture_key(url)
    if key is None:
        return ""
    marks = _ATTACHED_MARKS.get(key)
    if marks is None:
        try:
            raw = (settings.UPLOAD_DIR / key[0]).read_bytes()
        except OSError:
            return ""
        marks = {**pictures.picture_marks(raw), "bytes": len(raw)}
        if not marks.get("kind"):
            return ""
        _ATTACHED_MARKS[key] = marks
    return pictures.marks_words(marks)


def picture_travellers(url: str) -> list:
    """Кому из состава картинка уехала — именами мест, в порядке состава.

    Читается там же, где решается «показывать ли её ещё раз» (см. images_shown):
    показанной картинка становится после удачного хода, поэтому это и есть
    «кому она вправду уехала», а не «кому её собирались отправить». Один
    и тот же файл дважды одному месту не считается — это множество.
    """
    shown = getattr(session, "images_shown", None) or {}
    who = []
    for place in getattr(session, "runtime_participants", None) or []:
        name = str(place.get("display_name") or "")
        model = str(place.get("model") or "")
        if name and model and url in shown.get(model, set()):
            who.append(name)
    return who


def sent_picture_url(url: str) -> str:
    """Адрес той же картинки в том виде, в каком её получила модель (пусто — нет файла).

    Имя файла то же, а вид определяет маршрут (см. web.sent_picture): отдельного
    файла на диске у приведённого к обычному размеру нет — оно собирается
    на отправке (см. post_image_pairs).
    """
    key = _picture_key(url)
    return f"/api/picture/{key[0]}/sent" if key else ""


def sent_picture_bytes(file: str):
    """Данные картинки в том виде, в каком её получила модель, — и её вид (пусто, пусто).

    Спрашивают это затем, чтобы показать в записи хода то самое изображение,
    на которое модель смотрела (см. attachment_view, sent_url): слова
    «уехало 64×64 · 180 Б» без самой картинки — это слова о том, чего не видно.

    Картинка уже собранная — из памяти (см. _UPLOAD_IMAGES); не собранная
    (спектакль вернулся из ДАМПа, и отправки в этом запуске ещё не было) —
    собирается тут же и запоминается, чтобы не пересобирать на каждую отрисовку.
    """
    path = _upload_path(f"/uploads/{str(file or '').strip()}")
    if path is None:
        return b"", ""
    key = (path.name, path.stat().st_size)
    payload = _UPLOAD_IMAGES.get(key)
    if payload is None:
        raw = path.read_bytes()
        kind = image_kind(raw)
        if not kind:
            return b"", ""
        payload, _note = pictures.prepare(
            f"data:{kind};base64," + base64.b64encode(raw).decode("ascii"))
        _UPLOAD_IMAGES[key] = payload
    head, marker, body = payload.partition(";base64,")
    if not marker:
        return b"", ""
    kind = head.split(":", 1)[-1] or "image/png"
    try:
        return base64.b64decode(body), kind
    except Exception:
        return b"", ""


def sent_picture_words(url: str) -> str:
    """Какою картинка уехала моделям — и кому именно: «🖼 уехало моделям (Харита): …».

    Пусто — никому не уехала, и говорить тут не о чем. «Кому» берётся у состава
    (см. picture_travellers): слова без имени модели и читались как «что-то куда-то
    уехало» (см. page.postAttachmentsHtml).
    """
    who = picture_travellers(url)
    if not who:
        return ""
    words = travelled_picture_words(url)
    return (f"🖼 уехало моделям ({', '.join(who)}): {words}" if words
            else f"🖼 уехало моделям ({', '.join(who)})")


def sent_picture_tokens(url: str):
    """Наш счёт веса уехавшей картинки в запросе (None — картинка не уезжала).

    Вендор о цене картинки отдельно не говорит: в его числах она уже слита
    с текстом запроса. А длина её данных в том же запросе видна — по ней
    и считаем тем же счётом, каким приложение меряет историю (см.
    text.estimate_tokens), чтобы число сходилось с тем, чем резали.
    """
    key = _picture_key(url)
    marks = _UPLOAD_MARKS.get(key) if key else None
    if not marks:
        return None
    return marks.get("tokens")


def pictures_report() -> dict:
    """Сколько картинок уехало моделям и во что это обошлось.

    Считается по тому же следу, по которому решается «показывать ли её ещё раз»
    (см. images_shown): картинка, уехавшая двум моделям, — это два показа,
    а сама она одна. Показанным считается только то, что доехало вместе
    с удачным ходом, — иначе счёт был бы про намерения, а не про отправки.

    Токены — наш счёт (см. sent_picture_tokens) и по каждому показу: одна
    и та же картинка, уехавшая двум моделям, оплачена дважды — в запросе
    каждой она лежала своей строкой, и место там занимала такое же. А у картинки,
    вернувшейся из ДАМПа прошлого запуска, счёта может и не быть — про такие
    говорится отдельно, а не «ноль».
    """
    shown = getattr(session, "images_shown", None) or {}
    urls, trips, tokens, unknown = set(), 0, 0, 0
    for places in shown.values():
        for url in places:
            trips += 1
            urls.add(url)
            cost = sent_picture_tokens(url)
            if cost is None:
                unknown += 1
            else:
                tokens += int(cost or 0)
    return {"pictures": len(urls), "trips": trips,
            "tokens": tokens, "unknown": unknown}


def travelled_picture_words(url: str) -> str:
    """Какою картинка уехала модели: «png 64×64 · 169 Б (приложено 8×8 · 75 Б)».

    Пусто — картинка ещё никуда не уезжала, и сказать о ней нечего. Известно это
    с той самой отправки: картинка приводится к обычному размеру один раз на файл
    (см. post_image_pairs), и другой раз за неё никто не платит.
    """
    key = _picture_key(url)
    marks = _UPLOAD_MARKS.get(key) if key else None
    if not marks:
        return ""
    return pictures.travel_words(marks.get("sent"), marks.get("attached"))


def topic_picture_view(one: dict) -> dict:
    """Картинка к теме — для страницы: имя, адрес и свои слова.

    Тем же счётом, что у приложенного к реплике (см. attachment_view): картинка
    темы уезжает моделям так же, и во что её привели — видно там же.
    """
    view = dict(one or {})
    marked = attachment_view(view)
    for key in ("own", "sent"):
        if marked.get(key):
            view[key] = marked[key]
    return view


def version_view(post: dict) -> dict:
    """Прежняя версия реплики — для блока «прежние слова» (см. post["versions"]).

    Лежит ровно то, чем версия была: слова, акт, время и говорящий. Отдельной
    копией, а не ссылкой на саму реплику: реплика остаётся в ленте и меняется
    дальше, а список версий — летопись, и меняться вместе с ней он не должен.

    Приложенное к реплике — тоже часть версии: к своей реплике картинку можно
    убрать или приложить другую, и прежняя остаётся при тех словах, с которыми
    была сказана — поэтому «↩ вернуть эти слова» возвращает и её (см.
    switch_words, page.postVersionsHtml).

    «Почему» названо по тому, кто говорил: слова человека правят (см. edit_post,
    revert_version), а ответ модели переигрывают (см. replay_plan_for). Обе
    версии оплачены, и обе остаются — поэтому у каждой и есть своё имя.
    """
    human = post.get("model_used") == "human"
    content = post.get("content") or ""
    return {
        "content": content,
        "content_html": post.get("content_html") or text.markdown_to_html(content),
        "timestamp": post.get("timestamp") or "",
        "round": int(post.get("round") or 0),
        "display_name": post.get("display_name") or "",
        "model_used": post.get("model_used") or "",
        "why": "изменена" if human else "переиграна",
        # Копией, а не ссылкой: список версий — летопись, и приложенное в ней
        # живёт своей жизнью, даже когда у реплики оно уже другое. А слова
        # о картинке — те же, что у реплики в ленте (см. attachment_views):
        # версия и сама картинка — одно и то же
        "attachments": attachment_views(post.get("attachments")),
    }


def remember_version(post: dict) -> list:
    """Летопись версий реплики: прежние слова — к тем, что уже были.

    Список растёт с каждой правкой и переигровкой, старые версии впереди:
    по нему видно, из чего реплика сложилась, и к любой из версий можно
    вернуться (см. version_view, revert_version, parse_dump).
    """
    return list(post.get("versions") or []) + [version_view(post)]


def added_purpose(summary: dict, added: list) -> str:
    """Из чего сложился дописанный хвост — теми же словами, что в ленте.

    Раньше тут стояло «по паре на каждый поиск, плюс напоминания», и с числами
    это не сходилось: пара — привычный случай, а не всегдашний (см. added_kinds).
    Слова те же, что на странице (см. addedPurpose в page.py), и считать их надо
    по тем же сообщениям — иначе о ходе скажут разное в двух местах.
    """
    summary = summary or {}
    kinds = summary.get("added_kinds") or added_kinds(added)
    words = []
    if kinds.get("asks"):
        words.append(f"просьба вызвать инструмент — {kinds['asks']}")
    if kinds.get("results"):
        words.append(f"найденное по ней — {kinds['results']}")
    if kinds.get("refusals"):
        words.append(f"отказ по лимиту поисков — {kinds['refusals']}")
    if kinds.get("nudges"):
        words.append(f"просьба приложения словами — {kinds['nudges']}")
    tokens = summary.get("extra_tokens")
    if tokens is None:
        tokens = sum(int(m.get("tokens") or 0) for m in (added or []))
    count = f"{len(added or [])} сообщ. ({numbers_word(tokens)} токенов)"
    tail = ("В хронологии выше те же поиски названы своими словами — "
            "с формулировкой запроса и своим весом.")
    if not words:
        return f"После первого запроса приложение дописало модели {count}. {tail}"
    return (f"После первого запроса приложение дописало модели {count}: "
            + " · ".join(words) + ". " + tail)


def message_line(index: int, total: int, message: dict) -> str:
    """Одно сообщение запроса одной строкой — с объяснением, если текста нет.

    Ноль токенов у сообщения — не пустое место, а сигнал: по протоколу OpenAI
    просьба о поиске приходит пустым текстом плюс полем `tool_calls`. Поэтому
    у такого сообщения названы и его поля со значениями, и что они значат.

    Собрана строка из того же, что видно в ленте (см. promptMessagesHtml):
    роль, кто говорил, та же роль словами и вес — иначе одно и то же сообщение
    в файле и на странице читалось бы по-разному.
    """
    role = str(message.get("role") or "")
    weight = (f"{numbers_word(message.get('tokens'))} токенов"
              if int(message.get("tokens") or 0) else "0 токенов текста")
    head = [f"№{index}/{total}", role]
    name = str(message.get("name") or "")
    if name and name != role:
        head.append(name)
    words = ROLE_WORDS.get(role)
    if words:
        head.append(words)
    head.append(weight)
    line = "- " + " · ".join(head)
    if message.get("note"):
        line += f"\n  - {message['note']}"
    return line


def messages_block(messages: list) -> str:
    """Сообщения запроса списком — так они и лежат в запросе, по порядку."""
    messages = list(messages or [])
    out = []
    for index, message in enumerate(messages, 1):
        out.append(message_line(index, len(messages), message))
        if message.get("content"):
            out.append(quoted(message.get("content")))
    return "\n".join(out) + "\n"


def dump_turn_header(post_id: int, turn: dict) -> str:
    """Начало записи о ходе: кто говорит, чьим окном мерено, что вошло в запрос.

    Пишется ДО запроса к модели (см. open_dump_turn): у хода, который оборвался
    на середине, в файле должно остаться начало, а не пустота.

    Разделы идут в том же порядке, что на странице (см. TURN_SECTIONS):
    один и тот же отчёт читают и в файле, и в ленте. Записи о том, что дописало
    приложение, здесь нет нарочно: дописанное лежит своим разделом ниже
    (см. dump_turn_tail), а одни и те же данные в двух местах — не полнота,
    а каша.
    """
    out = ["\n" + turn_heading(post_id, turn) + "\n"]
    out.append(TURN_SECTIONS["who"] + "\n")
    out.append(who_line(turn))
    # Окно и то, что в него вошло, — один раздел: это два взгляда на одно место
    out.append(TURN_SECTIONS["place"] + "\n")
    out.append(window_line(turn.get("budget") or {}))
    out.append(history_line(turn.get("summary") or {}, turn.get("budget") or {}))
    out.append(TURN_SECTIONS["history"] + "\n")
    return "\n".join(out) + "\n"


def dump_turn_tail(post: dict, turn: dict) -> str:
    """Конец записи о ходе: набросок, обрезка, дописанное и чем ход кончился.

    Порядок тот же, что на странице (см. turnBodyHtml): хронологию открывает
    начало записи (см. dump_turn_header), а дальше идёт то, что стало известно
    к концу хода. Времени хода тут своя строка: в ленте оно стоит в разделе
    «кто говорит и когда», а в файле — рядом с итогом, потому что сказано оно
    может быть только к концу хода.
    """
    out = []
    summary = turn.get("summary") or {}
    if post.get("sketch"):
        out.append("\n" + TURN_SECTIONS["sketch"] + "\n")
        out.append(SKETCH_HINT + "\n")
        out.append(quoted(post["sketch"]))
    if summary.get("removed_messages"):
        out.append("\n" + TURN_SECTIONS["cut"] + "\n")
        for gone in turn.get("removed") or []:
            out.append(f"- {gone.get('speaker')} · {numbers_word(gone.get('tokens'))} токенов · "
                       f"{gone.get('preview')}")
    if turn.get("added"):
        out.append("\n" + TURN_SECTIONS["added"] + "\n")
        out.append(added_purpose(summary, turn["added"]) + "\n")
        out.append(messages_block(turn["added"]))
    out.append("\n" + TURN_SECTIONS["request"] + "\n")
    out.append(messages_block(turn.get("messages") or []))
    if summary.get("seconds") is not None:
        out.append(f"\n**Ход длился:** {duration_words(summary['seconds'])}"
                   f" — от начала обрезки истории до готовой реплики\n")
    # Приложенное к реплике — рядом с самой репликой, а не отдельным разделом:
    # оно и есть часть её, и по этим строкам картинка вернётся в ленту вместе
    # с прежним спектаклем (см. attachment_lines)
    out.extend(attachment_section(post))
    out.append("\n" + TURN_SECTIONS["answer"] + "\n")
    out.append(quoted(post.get("content")))
    return "\n".join(out) + "\n"


def attachment_section(post: dict) -> list:
    """Приложенное к реплике строками ДАМПа: адреса — и сами картинки данными.

    Одно место на оба хода, машинный и человеческий (см. dump_turn_tail,
    dump_human_markdown): приложенное — часть реплики, чья бы она ни была.
    Данные пишутся рядом с адресами, потому что файл дописывается по ходу,
    и без них картинка жила бы только в папке того экземпляра, где спектакль
    играли (см. picture_data_lines).
    """
    pictures = attachment_lines(post)
    if not pictures:
        return []
    return [""] + pictures + picture_data_lines(post.get("attachments"))


def attachment_lines(post: dict) -> list:
    """Приложенное к реплике — строками ДАМПа: имя файла и куда он лёг.

    По этим строкам приложенное и возвращается вместе с прежним спектаклем
    (см. parse_dump): адрес — тот же, что у реплики в ленте, поэтому картинка
    видна и после перезапуска театра. А сами данные лежат тут же, следующей
    строкой (см. attachment_section, picture_data_lines): без них картинка
    жила бы только в папке своего экземпляра.

    Строка о том, какою картинка уехала модели, встаёт тут же, следом
    (см. dump_note_picture_travel): приложенное пишется в файл тогда, когда
    реплика сказана, а уехать оно может только позже.
    """
    out = []
    for one in post.get("attachments") or []:
        url = str((one or {}).get("url") or "").strip()
        if not url:
            continue
        name = str((one or {}).get("name") or "").strip() or url.rsplit("/", 1)[-1]
        out.append(f"**Приложено:** {name} → {url}")
    return out


# ── КАРТИНКИ В ДАМПЕ ──────────────────────────────────────
# Спектакль — это и его картинки: приложенные к репликам и к теме. Адреса в
# записи есть всегда (см. attachment_lines), но адрес живёт только в папке того
# экземпляра, где спектакль играли: ДАМП, перенесённый в другой театр, показывал
# бы рамки без картинок, а после чистки папки и свои собственные терял бы.
# Поэтому данные лежат тут же: за строкой «**Картинка:**» идёт блок «> »
# в base64 — тем же приёмом, что и текст реплик (см. quoted), потому что фенсы
# не годятся (см. quoted). Файл от этого тяжелее, зато он один и есть спектакль.


def _upload_path(url: str):
    """Файл приложенного по его адресу: имя, а не путь — адрес чужой текст."""
    name = str(url or "").strip().rstrip("/").split("/")[-1]
    if not name or name != Path(name).name:
        return None
    path = settings.UPLOAD_DIR / name
    return path if path.is_file() else None


def picture_data_lines(pictures: list) -> list:
    """Данные картинок — строками ДАМПа: строка с именем и сам base64 рядом.

    Уже записанная картинка второй раз не пишется: файл дописывается по ходу
    спектакля, и та же картинка, приложенная к другой реплике, только раздула бы
    запись. Помнит это _DUMP — по адресу, а не по содержимому (см. stored_picture:
    возвращённая из ДАМПа картинка сохраняет прежний адрес).
    """
    out = []
    for one in pictures or []:
        url = str((one or {}).get("url") or "").strip()
        if not url or url in _DUMP["pictures"]:
            continue
        path = _upload_path(url)
        if path is None:
            # Картинки больше нет: адрес в записи остался, а данных нет
            continue
        raw = path.read_bytes()
        name = str((one or {}).get("name") or "").strip() or path.name
        written = base64.b64encode(raw).decode("ascii")
        _DUMP["pictures"].add(url)
        out.append(f"**Картинка:** {url} · {name} · {image_kind(raw) or 'вид не опознан'} · "
                   f"{len(written)} знаков")
        for start in range(0, len(written), 76):
            out.append("> " + written[start:start + 76])
    return out


def topic_picture_line(one: dict) -> str:
    """Приложенное к теме одной строкой: имя, адрес и когда его приложили.

    Отметка «сколько реплик было в ленте» нужна возврату спектакля: по ней видно,
    кто картинку уже видел (см. restore_shown_pictures), и без неё доигранный
    спектакль показал бы её заново тем, кто уже заплатил за неё.
    """
    url = str((one or {}).get("url") or "").strip()
    name = str((one or {}).get("name") or "").strip() or url.rsplit("/", 1)[-1]
    return (f"**Картинка к теме:** {name} → {url} · реплик в ленте "
            f"к тому моменту: {_as_int((one or {}).get('since'))}")


def topic_picture_lines() -> list:
    """Приложенное к теме — строками ДАМПа, в порядке показа."""
    return [topic_picture_line(one) for one in session.topic_attachments or []
            if str((one or {}).get("url") or "").strip()]


def dump_topic_pictures() -> None:
    """Приложенное к теме — в ДАМП: что приложили, что убрали и сами данные.

    Дописывается тогда, когда список и вправду изменился (см.
    set_topic_attachments): приложить картинку к теме можно и на ходу, а ДАМП —
    единственное место, где спектакль лежит целиком. Убранное тоже называется:
    запись дописывается, и без такой строки возвращённый спектакль принёс бы
    картинку, которую уже убрали.
    """
    if not settings.DUMP_FILE.is_file():
        return                      # спектакля в записи ещё нет — и напоминать нечего
    here = {str((one or {}).get("url") or ""): one
            for one in session.topic_attachments or []
            if str((one or {}).get("url") or "").strip()}
    out = []
    for url, one in here.items():
        if url not in _DUMP["topic_pictures"]:
            _DUMP["topic_pictures"].add(url)
            out.append(topic_picture_line(one))
    for url in sorted(_DUMP["topic_pictures"] - set(here)):
        _DUMP["topic_pictures"].discard(url)
        _DUMP["topic_removed"].add(url)
        out.append(f"**Картинка к теме убрана:** {url}")
    out += picture_data_lines(session.topic_attachments)
    if out:
        dump_write("\n" + "\n".join(out) + "\n")


def dump_replayed(ids: list) -> None:
    """Сказанное заново — отметкой в ДАМПе: прежние записи этих реплик больше не реплики.

    Файл дописывается, а не переписывается (см. start_dump), поэтому переигранная
    реплика остаётся в нём такой, какой была сказана: рядом с ней ложится отметка,
    что её переиграли, а ниже — новая запись с тем же номером (см. add_post,
    edit_post). Читается последнее (см. parse_dump), поэтому запись остаётся
    и летописью правок, и текущим спектаклем, а тема с картинками не теряется
    вовсе: она лежит в тех же строках файла.
    """
    numbers = []
    for one in ids or []:
        try:
            numbers.append(int(one))
        except (TypeError, ValueError):
            continue
    if not numbers:
        return
    dump_write("\n**Переиграны реплики:** "
               + ", ".join(str(one) for one in numbers) + "\n")


def dump_human_markdown(post: dict) -> str:
    """Ход живого участника: отправлять никуда нечего, но в хронологии он есть."""
    head = " · ".join([f"## {post.get('id')}", str(post.get("timestamp") or ""),
                       cast_field(post.get("avatar_emoji"), post.get("display_name"),
                                  post.get("gender")),
                       str(post.get("model_used") or ""),
                       str(post.get("role_name") or ""), f"Акт {post.get('round')}"])
    # Приложенное — до самого текста: реплика человека читается как последний
    # «> »-блок записи (см. _last_quoted), и base64 после неё стал бы репликой
    pictures = "".join(line + "\n" for line in attachment_section(post))
    return (f"\n{head}\n\n"
            + f"Реплика человека: никуда не отправлялась, ни токенов, ни поиска.\n\n"
            + pictures
            + quoted(post.get("content")) + "\n")


# Открытый файл ДАМПа и событие, которое сейчас дописывается: держим их между
# вызовами, потому что ход пишется по частям — от запроса к запросу и от куска
# размышлений к куску (см. dump_step_sink). line_start — стоим ли мы в начале
# строки: по нему куски размышлений достраивают кавычки, не рвя строку
# pictures — адреса картинок, уже записанных данными; topic_pictures — что
# объявлено приложенным к теме, а topic_removed — что с тех пор убрали: файл
# дописывается по ходу, и без этих трёх пометок одна и та же картинка уезжала бы
# в запись по второму разу, а убранная возвращалась бы (см. picture_data_lines,
# dump_topic_pictures)
_DUMP = {"handle": None, "thought": None, "line_start": True,
         "pictures": set(), "pictures_seen": False,
         "topic_pictures": set(), "topic_removed": set()}


def _remember_written_pictures() -> None:
    """Пометить картинки, лежащие в файле данными, — чтобы не писать их снова.

    Файл дописывается, а не переписывается: продолженный спектакль вернулся
    в ленту из этого же ДАМПа (см. load_play_from_dump), и его картинки в файле
    уже есть. Без этой пометки правка реплики с картинкой клала бы в файл ещё
    раз все её мегабайты: ДАМП растёт от каждой правки и каждого возврата версии
    (см. picture_data_lines). Читаются только строки «**Картинка:**» — сами
    данные для этого разбирать незачем.
    """
    try:
        lines = Path(settings.DUMP_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        head = _PICTURE_HEAD_RE.match(line)
        if head:
            _DUMP["pictures"].add(head.group("url"))


def dump_write(text: str, force: bool = True) -> None:
    """Дописать в ДАМП — сразу, а не в конце хода.

    Сбрасывается на диск каждое событие, и даже каждый кусок размышлений:
    в этом и смысл живого ДАМПа — файл читают в тот момент, когда ход ещё идёт
    (или уже оборвался), а не после. Куски невелики, а ходов в спектакле десятки:
    экономить на сбросах здесь незачем.
    """
    if not text:
        return
    handle = _DUMP.get("handle")
    try:
        if handle is None:
            # Дописываем к прежнему файлу — к тому, что остался от прошлого
            # запуска: его картинки в нём уже лежат, и второй раз они бы уехали
            # в файл ещё раз (см. _remember_written_pictures)
            if not _DUMP.get("pictures_seen"):
                _remember_written_pictures()
                _DUMP["pictures_seen"] = True
            with open(settings.DUMP_FILE, "a", encoding="utf-8") as one:
                one.write(text)
            return
        handle.write(text)
        if force:
            handle.flush()
    except Exception as e:
        print(f"  ⚠️  Не сохраняется ход в {settings.DUMP_FILE.name}: {e}")


def _dump_thought_piece(piece: str) -> None:
    """Кусок размышлений — в файл, построчно и в кавычках, как всё остальное.

    Кусок приходит как придётся: он может кончаться на половине строки, а может
    содержать несколько строк сразу. Поэтому строки уходят целыми и с «> »,
    а недописанная строка — без перевода строки: файл растёт, как растут сами
    размышления, и при этом разметка цела.

    Кавычки тут не для красоты: модель пишет в размышлениях свои заголовки
    и списки, и без кавычек они рвали бы ДАМП на чужие разделы — и читать его
    стало бы нельзя (см. parse_dump).
    """
    text = str(piece or "")
    if not text:
        return
    out = []
    for index, part in enumerate(text.split("\n")):
        if index:
            out.append("\n")
            _DUMP["line_start"] = True
        if _DUMP.get("line_start"):
            out.append("> ")
            _DUMP["line_start"] = False
        out.append(part)
    dump_write("".join(out))


def dump_step_sink(step: dict, piece: str = None) -> None:
    """Событие хода — в файл, в тот же миг, как оно случилось.

    Размышления приходят кусками: шапка пишется одна, а дальше в файл течёт сам
    текст. Именно ради этого ДАМП и пишется по ходу дела — у модели, которая
    две минуты думает и так и не отвечает, в файле видно ровно то, что она
    думала, а не пустое место (см. cloud.journal_thought).
    """
    if step.get("kind") == "thought":
        if _DUMP.get("thought") is not step:
            _DUMP["thought"] = step
            dump_write(step_markdown(step, with_text=False) + "\n")
            _DUMP["line_start"] = True
        # Без force=False: файл читают глазами прямо во время хода
        _dump_thought_piece(piece)
        return
    if _DUMP.get("thought") is not None:
        # Размышления кончились: закрываем их последнюю строку, если она осталась
        # недописанной, — и тут же исправляем их шапку, потому что итоговый вес
        # и время окончания стали известны только сейчас (см. dump_fix_step)
        finished = _DUMP["thought"]
        _DUMP["thought"] = None
        if not _DUMP.get("line_start"):
            dump_write("\n")
        _DUMP["line_start"] = True
        dump_fix_step(finished)
    dump_write(step_markdown(step) + "\n")


def _dump_rewrite(change) -> None:
    """Переписать ДАМП по строкам: change(строки) — строки или None (писать нечего).

    Открытый на дописывание файл на это время закрывается, а после открывается
    снова: файл переписывается целиком, и дописывать в тот же миг нельзя. Всё это
    на одном месте потому, что правка в файле не одна (см. _dump_replace_line,
    dump_note_picture_travel), а закрытие и переоткрытие — то же самое.
    """
    path = Path(settings.DUMP_FILE)
    was_open = _DUMP.get("handle") is not None
    handle = _DUMP.get("handle")
    if handle is not None:
        try:
            handle.flush()
            handle.close()
        except Exception:
            pass
        _DUMP["handle"] = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except Exception:
        lines = []
    written = change(lines)
    if written is not None:
        temp = path.with_name(path.name + ".fix")
        try:
            temp.write_text("".join(written), encoding="utf-8")
            temp.replace(path)
        except Exception as e:
            print(f"  ⚠️  Не исправляется строка в {path.name}: {e}")
    if was_open:
        dump_reopen()


def _dump_replace_line(prefix: str, new_text: str) -> None:
    """Переписать в ДАМПе строку, начинающуюся с `prefix`.

    Живое письмо и правка тут не спорят. Строка появляется в файле в тот миг,
    когда событие случилось, — тогда у неё нет ни времени окончания, ни чисел
    вендора: ждать с записью нельзя, иначе у оборванного хода не осталось бы
    следа. А показывать устаревшее — врать: «1 токенов» у мыслей, текст
    которых на семьсот токенов, читается как ошибка. Поэтому, когда итог
    известен, файл переписывает ту же строку. Метка у каждой строки своя
    (у события — время начала, у записи о ходе — номер реплики), по ней
    строка и находится.
    """
    def change(lines):
        for index, line in enumerate(lines):
            if line.startswith(prefix):
                lines[index] = new_text + "\n"
                return lines
        return None
    _dump_rewrite(change)


def _attached_picture_url(line: str) -> str:
    """Адрес картинки из строки о приложенном — или пусто, если строка не о ней.

    Таких строк в записи две: приложенное к реплике (см. attachment_lines)
    и картинка к теме (см. topic_picture_line). Адрес у обеих — последнее слово
    перед « ·», по нему строка и находится: у приложенного к реплике адрес
    последний, у картинки к теме за ним идёт счёт реплик.
    """
    written = str(line or "").strip()
    if not (written.startswith("**Приложено:** ")
            or written.startswith("**Картинка к теме:** ")):
        return ""
    return written.rsplit("→", 1)[-1].strip().split(" ")[0].strip()


def dump_note_picture_travel(urls) -> None:
    """Дописать в ДАМП, какою картинка уехала модели, — рядом с приложенной.

    Приложенное пишется в файл тогда, когда реплика сказана, а уехать оно может
    только позже: приведённого к обычному размеру в тот миг ещё нет (см.
    pictures.prepare, post_image_pairs). Поэтому строка о том, что уехало,
    досчитывается тогда, когда картинка вправду уехала, — тут же за строкой
    о приложенном, потому что читают их вместе.

    Зовётся это только про те картинки, что и вправду уехали (см.
    commit_shown_pictures): про оставшуюся в стороне молчит и файл. Одну
    картинку могут приложить к нескольким репликам — строка встанет к каждой.
    """
    marks = {str(url or ""): picture_travel_line(url) for url in urls or []}
    marks = {url: line for url, line in marks.items() if url and line}
    if not marks:
        return

    def change(lines):
        out, added = [], False
        for index, line in enumerate(lines):
            out.append(line)
            told = marks.get(_attached_picture_url(line))
            if not told:
                continue
            # Уже сказано — второй раз не пишем: файл дописывается, и один и тот
            # же ход попадает в правку не раз (например, после перезапуска театра)
            following = lines[index + 1] if index + 1 < len(lines) else ""
            if following.strip() == told:
                continue
            out.append(told + "\n")
            added = True
        return out if added else None

    _dump_rewrite(change)


def picture_travel_line(url: str) -> str:
    """Строка о том, какою картинка уехала модели (пустая — ещё не уезжала)."""
    words = travelled_picture_words(url)
    return f"**Уехало модели:** {words}" if words else ""


def dump_fix_step(step: dict) -> None:
    """Исправить в ДАМПе строку события, у которого появились итоговые числа."""
    if step.get("kind") not in ("ask", "thought"):
        return
    if step.get("kind") == "thought" and not step.get("t_end"):
        # Мысль, пришедшая одним куском, до сих пор оставалась без времени
        # окончания: дописывать её больше некому, и этот миг и есть конец
        step["t_end"] = time.time()
    when = clock(step.get("t"))
    if not when:
        return
    _dump_replace_line(f"- {when} ·", step_markdown(step, with_text=False))


def dump_fix_heading(post_id: int, turn: dict) -> None:
    """Дописать в шапку записи то, что стало известно к концу хода.

    Шапка — это свёрнутая строка реплики: по ней решают, читать ли дальше
    (см. turn_heading). Сколько ход шёл и сколько стоил к его началу неизвестно,
    поэтому к концу хода та же строка переписывается целиком.
    """
    _dump_replace_line(f"## {post_id} · ", turn_heading(post_id, turn))


def drop_dump_turn(post_id) -> bool:
    """Убрать из ДАМПа незаконченную запись — перед тем как переспросить её ход.

    Ход, оборванный смертью процесса, остался в файле без хвоста: хвост пишется
    одним куском в самом конце хода (см. dump_turn_tail). Переспрошенный ляжет
    тем же номером (см. replay_broken_turn) — значит прежнюю запись надо убрать:
    две записи об одном ходе читались бы как две реплики, и разобрать файл
    было бы нельзя.

    Убирается только сама запись: всё, что записано после неё (например, шапка
    «Продолжение спектакля»), остаётся на месте — читать дальше надо по порядку.
    False — записи не нашлось или файл не читается; тогда переспрос отменяется,
    а оборванная реплика так и остаётся помеченной.
    """
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return False
    path = Path(settings.DUMP_FILE)
    _dump_reset()
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return False
    start = next((index for index, line in enumerate(lines)
                  if line.startswith(f"## {post_id} · ")), None)
    if start is None:
        return False
    end = start + 1
    while end < len(lines) and not (lines[end].startswith("## ")
                                    or lines[end].startswith("**Продолжение")):
        end += 1
    rest = lines[end:]
    temp = path.with_name(path.name + ".fix")
    try:
        temp.write_text("".join(lines[:start] + rest), encoding="utf-8")
        temp.replace(path)
    except Exception as e:
        print(f"  ⚠️  Не убирается прежняя запись из {path.name}: {e}")
        return False
    return True


def dump_reopen() -> None:
    """Открыть ДАМП дальше на дописывание — после того как строка исправлена."""
    try:
        _DUMP["handle"] = open(settings.DUMP_FILE, "a", encoding="utf-8")
    except Exception as e:
        _DUMP["handle"] = None
        print(f"  ⚠️  Не открывается {settings.DUMP_FILE.name}: {e}")


def _dump_reset() -> None:
    """Закрыть ДАМП, если он открыт: этот файл больше не наш.

    Нужно тому, кто начинает запись заново или стирает её целиком
    (см. start_dump и forget_play): открытый на дописывание файл пережил бы
    и перезапись, и удаление.
    """
    handle, _DUMP["handle"] = _DUMP.get("handle"), None
    _DUMP["thought"] = None
    _DUMP["line_start"] = True
    # Файл больше не наш: то, что было в нём записано, писать заново не надо
    _DUMP["pictures"] = set()
    _DUMP["pictures_seen"] = False
    _DUMP["topic_pictures"] = set()
    _DUMP["topic_removed"] = set()
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


def open_dump_turn(post_id: int, turn: dict, report: dict) -> None:
    """Начать запись о ходе и отдать журналу «перо»: события пишутся сразу.

    report["sink"] — это и есть перо (см. cloud.journal_push): каждое событие
    хода улетает в файл в тот момент, когда случилось. Файл открыт всё время
    хода, поэтому и оборванный ход остаётся в ДАМПе.
    """
    _DUMP["thought"] = None
    _DUMP["line_start"] = True
    dump_reopen()
    dump_write(dump_turn_header(post_id, turn))
    if report is not None:
        report["sink"] = dump_step_sink
        # Перо пишет событие сразу, а «правка» доводит его строку, когда числа
        # наконец известны (см. dump_fix_step): у запроса это время окончания
        # и числа вендора, у размышлений — итоговый вес
        report["fix"] = dump_fix_step


def close_dump_turn(post: dict, turn: dict) -> None:
    """Закончить запись о ходе: шапка, реплика, прежняя её версия — и закрыть файл."""
    if _DUMP.get("thought") is not None:
        # Мысль оказалась последним событием хода: её строка тоже ждёт итога
        finished = _DUMP["thought"]
        _DUMP["thought"] = None
        if not _DUMP.get("line_start"):
            dump_write("\n")
        _DUMP["line_start"] = True
        dump_fix_step(finished)
    if post.get("id") and turn:
        # Теперь известны и время хода, и его цена — шапка пишется заново
        dump_fix_heading(post["id"], turn)
    dump_write(dump_turn_tail(post, turn))
    handle = _DUMP.get("handle")
    _DUMP["handle"] = None
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


def start_dump(topic: str, keep: bool = False) -> None:
    """Начать ДАМП спектакля — заново, а не дописать к прежнему.

    Файл переписывается целиком на каждом спектакле: ДАМП — про последний
    спектакль, а не летопись. Иначе он рос бы вечно, а читают его всегда ради
    разбора свежего случая — так и было со стенограммой, которая никого
    не чистилась и после десятков спектаклей стала мегабайтами.

    И это же тот файл, из которого прежний спектакль возвращается в ленту
    после перезапуска (см. parse_dump): тема, формат записи, состав и все
    реплики в их ходами лежат здесь целиком, а не только в памяти процесса.

    keep=True — спектакль продолжается, а не начинается: файл не переписывается,
    и прежние реплики остаются в нём там, где были (запись о продолжении только
    дописывается). Так бывает, когда спектакль вернули из ДАМПа чужого формата
    и решили доиграть (см. load_play_from_dump).
    """
    # Осиротевший временный файл — след смерти процесса в самой середине правки
    # строки (см. _dump_replace_line): правка в нём не доехала до файла, а он
    # остался. Спектакль от этого не страдает, но и хранить его незачем
    dump_path = Path(settings.DUMP_FILE)
    stray_fix = dump_path.with_name(dump_path.name + ".fix")
    try:
        stray_fix.unlink()
    except OSError:
        pass
    if keep and dump_path.exists():
        # Состав записывается заново: доигрывать можно другим составом,
        # а лицо участника — часть записи (см. cast_line)
        when = time.strftime("%d.%m.%Y %H:%M")
        dump_write(f"\n\n**Продолжение спектакля:** {when}\n\n**Состав:** {cast_line()}\n\n")
        return
    _dump_reset()               # на всякий случай: прошлый файл больше не наш
    when = time.strftime("%d.%m.%Y %H:%M")
    header = (
        f"# ДАМП · {when} · порт {settings.PORT}\n\n"
        f"Формат записи: {DUMP_FORMAT}\n\n"
        f"**Тема:**\n{quoted((topic or '').strip())}\n\n"
        + "".join(line + "\n" for line in topic_picture_lines())
        + f"\n**Состав:** {cast_line()}\n\n"
        f"Спектакль целиком, реплика за репликой: что вошло в каждый запрос\n"
        f"к модели, что она попросила, что ей принесли и что она сказала —\n"
        f"со временем и числами токенов. Лицо говорящего берётся из состава\n"
        f"выше, а не хранится в каждой реплике: аватар — свойство участника.\n"
        f"Пишется по ходу дела, заново на каждый новый спектакль: по этому же\n"
        f"файлу прежний спектакль возвращается в ленту после перезапуска.\n\n"
        f"Каждая запись начинается шапкой «## номер · время · имя · модель ·\n"
        f"роль · акт · сводка» — это то же, что свёрнутая строка реплики\n"
        f"в ленте. Дальше — разделы в том порядке, как они идут на странице.\n\n"
        f"Как читать. **ввод** — сколько токенов было в этом запросе к модели;\n"
        f"**вывод** — сколько она вернула (размышления считаются выводом,\n"
        f"но в реплику не попадают); **наш счёт** — то, что театр посчитал сам\n"
        f"(tiktoken — тот же счётчик, каким мерится история), а число рядом —\n"
        f"счёт вендора, и они расходятся, потому что токенизаторы разные;\n"
        f"**вес найденного** — сколько токенов принёс поиск (они едут сверх\n"
        f"истории и оплачиваются тоже).\n\n"
        f"Приложенные картинки лежат в этом же файле, целиком: у каждой\n"
        f"своя строка «**Картинка:**» с адресом, именем, видом и весом, а под\n"
        f"ней — сами данные (base64, блоком «> »). Картинка к теме названа\n"
        f"в шапке («**Картинка к теме:**») вместе с отметкой, сколько реплик\n"
        f"было в ленте, когда её приложили. Так один этот файл возвращает\n"
        f"спектакль целиком — с картинками, темой и составом, — даже в другом\n"
        f"театре, где папки этого экземпляра нет.\n"
    )
    try:
        settings.DUMP_FILE.write_text(
            header + "".join(line + "\n"
                             for line in picture_data_lines(session.topic_attachments)),
            encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Не открывается {settings.DUMP_FILE.name}: {e}")


def dump_new_version(post: dict) -> None:
    """Новая версия реплики — в ДАМП, на прежнем месте.

    Так ложатся и правка слова человека, и возврат прежней версии: отметка
    говорит читающему, что запись выше — уже не реплика, а сама запись встаёт
    тем же номером (см. dump_replayed, parse_dump). Поэтому в спектакле
    остаётся одно место, а в файле — обе версии.
    """
    dump_replayed([int(post.get("id") or 0)])
    save_dump_entry(post)


def save_dump_entry(post: dict, turn: dict = None) -> None:
    """Дописать в ДАМП реплику, чей ход не писался по ходу дела.

    Так приходит только живая реплика: у неё ни запросов, ни поиска, поэтому
    ей нечего было писать заранее. Всё машинное пишется иначе — открывается
    до запроса и дописывается по событию (см. open_dump_turn).
    """
    if turn:
        dump_write(dump_turn_header(post.get("id"), turn))
        dump_write(dump_turn_tail(post, turn))
        return
    dump_write(dump_human_markdown(post))


# ── ЧТЕНИЕ ДАМПА: ПРЕЖНИЙ СПЕКТАКЛЬ ───────────────────────
#
# ДАМП — это не только хронология для чтения глазами, а сам спектакль: реплики,
# их ходы и всё, что к ним привело. По нему прежний спектакль возвращается
# в ленту после перезапуска (см. load_play_from_dump): посты и отчёты живут
# в памяти процесса, и без этого новая вёрстка означала бы «посты пропали».
#
# Читается ровно то, что пишется: заголовки разделов берутся из TURN_SECTIONS,
# а строки — из тех же заведений, что их и пишут (см. ask_line, window_line,
# history_line, message_line). Формат помечен номером (см. DUMP_FORMAT), и чужой
# номер честно называется нечитаемым: половина восстановленного спектакля хуже,
# чем никакого.

# Число с разрядами, как его пишет numbers_word: «31 445» (пробел любой ширины)
_GROUPED = r"[\d][\d\u00a0\u202f ]*"

_RECORD_RE = re.compile(r"^## (\d+) · (.+)$")
_SECTION_RE = re.compile(r"^### (.+)$")
_WINDOW_RE = re.compile(
    rf"^\*\*Окно говорящего:\*\* (?P<kind>.+?) (?P<window>{_GROUPED}) токенов целиком — "
    rf"(?P<answer>.+?), (?P<safety>{_GROUPED}) — технический запас, "
    rf"на историю оставалось (?P<available>.+)$")
_HISTORY_RE = re.compile(
    rf"^\*\*Запрос к модели состоял из:\*\* (?P<messages>\d+) сообщ\. "
    rf"\((?P<tokens>{_GROUPED}) токенов\) — системный промпт (?P<system>{_GROUPED}) токенов, "
    rf"из сцены (?P<scene>\d+) сообщ\. \((?P<kept>{_GROUPED}) токенов\)"
    rf"(?:, и ещё (?P<tasks>\d+) — задания хода)?\. "
    rf"(?:Обрезка выбросила (?P<removed>\d+) сообщ\. \((?P<removed_tokens>{_GROUPED}) токенов\)"
    rf"|Обрезка ничего не тронула: сцена влезла в окно целиком)")
_DURATION_RE = re.compile(r"^\*\*Ход длился:\*\* (?P<words>.+?)(?: —|$)")
_ASK_STEP_RE = re.compile(r"^-\s+(?P<period>.+?) · \*\*запрос (?P<n>\d+)\*\* — (?P<body>.+)$")
_SEARCH_STEP_RE = re.compile(
    rf"^-\s+(?P<period>.+?) · 🔍 \*\*поиск (?P<n>\d+)\*\* \(лимит (?P<limit>\d+)\): "
    rf"«(?P<query>.*)» — принесено (?P<tokens>{_GROUPED}) токенов$")
_THOUGHT_STEP_RE = re.compile(
    rf"^-\s+(?P<period>.+?) · 💭 \*\*размышления\*\* \(к запросу (?P<n>\d+)\) — "
    rf"(?P<tokens>{_GROUPED}) токенов, в реплику не попадают$")
_OTHER_STEP_RE = re.compile(r"^-\s+(?P<period>.+?) · (?P<mark>[⛔🔍⚠️·💰]) (?P<text>.*)$")
_CUT_LINE_RE = re.compile(
    rf"^- (?P<speaker>.*?) · (?P<tokens>{_GROUPED}) токенов · (?P<preview>.*)$")
_MESSAGE_RE = re.compile(
    r"^- №(?P<index>\d+)/(?P<total>\d+) · (?P<role>[^·]*) · (?P<rest>.*)$")
_AVATAR_RE = re.compile(r"^\*\*Аватар:\*\* (?P<path>\S+)$")
# Приложенное к реплике: имя файла и его адрес в папке экземпляра (см. attachment_lines)
_ATTACH_RE = re.compile(r"^\*\*Приложено:\*\* (?P<name>.+?) → (?P<url>\S+)$")
# Сама картинка, тут же в файле: адрес, имя, вид и вес; под этой строкой — base64
_PICTURE_HEAD_RE = re.compile(r"^\*\*Картинка:\*\* (?P<url>\S+) · (?P<name>.*?) · "
                              r"(?P<kind>[^·]+?) · (?P<signs>.+)$")
# Картинка к теме — в шапке записи: имя, адрес и когда её приложили (см. topic_picture_lines)
_TOPIC_PICTURE_RE = re.compile(
    r"^\*\*Картинка к теме:\*\* (?P<name>.+?) → (?P<url>\S+) · реплик в ленте "
    r"к тому моменту: (?P<since>.+)$")
_TOPIC_GONE_RE = re.compile(r"^\*\*Картинка к теме убрана:\*\* (?P<url>\S+)$")
# Переигранное: всё, что записано этими номерами выше этой строки, — уже не лента
_REPLAYED_RE = re.compile(r"^\*\*Переиграны реплики:\*\* (?P<ids>[\d,\s]+)$")


def _as_int(raw) -> int:
    """Число из текста с разрядами: «1 234» — это 1234."""
    return int(re.sub(r"[^\d]", "", str(raw if raw is not None else "")) or 0)


def duration_seconds(text) -> int:
    """Секунды из «2 мин 15 с» — обратно тому, что пишет duration_words."""
    written = str(text or "")
    minutes = re.search(r"(\d+)\s*мин", written)
    secs = re.search(r"(\d+)\s*с", written)
    return ((int(minutes.group(1)) * 60 if minutes else 0)
            + (int(secs.group(1)) if secs else 0))


def rubles(text):
    """Рубли из «1,89 ₽» — обратно тому, что пишет money (None — цены нет)."""
    written = re.sub(r"[^\d,.]", "", str(text or "")).replace(",", ".")
    try:
        return round(float(written), 2)
    except ValueError:
        return None


def _quoted_block(lines: list) -> tuple:
    """«> »-текст: сам текст и сколько строк он занял (см. quoted)."""
    out, used = [], 0
    for line in lines:
        if line.startswith("> "):
            out.append(line[2:])
        elif line.strip() == ">":
            out.append("")
        else:
            break
        used += 1
    return "\n".join(out), used


def _section_text(lines: list) -> str:
    """Весь «> »-текст раздела — реплика, набросок или найденное по поиску."""
    start = 0
    while start < len(lines) and not lines[start].startswith(">"):
        start += 1
    return _quoted_block(lines[start:])[0]


def _last_quoted(lines: list) -> str:
    """Последний «> »-текст записи — обратный взгляд тому, что делает _section_text.

    Нужен для записи, разделы которой незнакомы (см. ask_about_dump): реплика
    там — последний раздел, а значит последний текст в кавычках. Без этого
    попытка прочитать чужой файл давала реплики без слов — а это выглядело бы
    как «модель промолчала», то есть прямая неправда.
    """
    start = None
    for index, line in enumerate(lines):
        if line.startswith(">"):
            start = index
        elif line.strip() and start is not None:
            start = None
    return _quoted_block(lines[start:])[0] if start is not None else ""


def _split_sections(lines: list) -> dict:
    """Тело записи по разделам: имя раздела — его строки (см. TURN_SECTIONS)."""
    sections, key = {}, ""
    for line in lines:
        found = _SECTION_RE.match(line)
        if found:
            key = TURN_SECTION_KEYS.get("### " + found.group(1).strip(), "")
            sections[key] = []
            continue
        if key:
            sections[key].append(line)
    return sections


def _with_period(step: dict, period: str) -> dict:
    """Время события — словами: часов у прочитанного хода уже нет."""
    start, _, end = str(period or "").partition(" → ")
    step["clock"] = start.strip()
    step["clock_end"] = end.strip()
    return step


def _ask_body(body: str) -> dict:
    """Числа запроса из его строки — обратно тому, что пишет ask_line."""
    if body.startswith("не прошёл — "):
        return {"error": body[len("не прошёл — "):]}
    step = {}
    if "числа токенов вендор не сообщил" in body:
        # Запрос, который ещё шёл (или оборвался): числа вендора появятся только
        # с ответом, а до тех пор есть один наш счёт. Дальше разбор общий —
        # именно тут терялся признак «с инструментом поиска», и в ленте стояло
        # «без инструмента» про запрос, который уехал с ним
        found = re.search(rf"наш счёт ≈({_GROUPED}) токенов", body)
        if found:
            step["tokens_in_est"] = _as_int(found.group(1))
    else:
        for key, pattern in (("tokens_in", rf"ввод ({_GROUPED})"),
                             ("tokens_in_est", rf"наш счёт ≈({_GROUPED})\)"),
                             ("tokens_out", rf"→ вывод ({_GROUPED}) токенов"),
                             ("reasoning_tokens", rf"из них размышлений ({_GROUPED})")):
            found = re.search(pattern, body)
            if found:
                step[key] = _as_int(found.group(1))
    found = re.search(r"конец: (\S+)", body)
    if found:
        step["finish_reason"] = found.group(1)
    if "без инструмента поиска" in body:
        step["tools"] = False
    elif "с инструментом поиска" in body:
        step["tools"] = True
    return step


def _step_from(lines: list, index: int) -> tuple:
    """Одно событие хода, начиная со строки index: событие и что читать дальше.

    Порядок разбора — тот же, что порядок письма (см. step_markdown): строка
    запроса, поиска и размышлений узнаётся по своему заголовку, а остальное —
    по значку события (он у каждого вида свой, см. STEP_MARKS).
    """
    line = lines[index]
    found = _ASK_STEP_RE.match(line)
    if found:
        step = {"kind": "ask", "n": int(found.group("n")),
                **_ask_body(found.group("body"))}
        return _with_period(step, found.group("period")), index + 1
    found = _SEARCH_STEP_RE.match(line)
    if found:
        step = {"kind": "search", "n": int(found.group("n")),
                "limit": int(found.group("limit")), "query": found.group("query"),
                "tokens": _as_int(found.group("tokens"))}
        step["results"], used = _quoted_block(lines[index + 1:])
        return _with_period(step, found.group("period")), index + 1 + used
    found = _THOUGHT_STEP_RE.match(line)
    if found:
        step = {"kind": "thought", "n": int(found.group("n")),
                "tokens": _as_int(found.group("tokens"))}
        step["text"], used = _quoted_block(lines[index + 1:])
        return _with_period(step, found.group("period")), index + 1 + used
    found = _OTHER_STEP_RE.match(line)
    if found:
        mark = found.group("mark").replace("\ufe0f", "")
        step = {"kind": STEP_KINDS.get(mark, "note"), "text": found.group("text")}
        return _with_period(step, found.group("period")), index + 1
    return None, index + 1


def _parse_steps(lines: list) -> list:
    """Хронология хода: события по порядку, со своим временем и весом."""
    steps, index = [], 0
    while index < len(lines):
        step, index = _step_from(lines, index)
        if step:
            steps.append(step)
    return steps


def _record_seconds(lines: list):
    """Сколько ход шёл — из его же строки (см. dump_turn_tail).

    Строку ищут по всей записи, а не только в хронологии: время хода известно
    только к его концу, и в файле у него своё место — рядом с остальным итогом.
    """
    for line in lines or []:
        found = _DURATION_RE.match(line)
        if found:
            return duration_seconds(found.group("words"))
    return None


def _parse_cut(lines: list) -> list:
    """Что выбросила обрезка — обратно тому, что пишет раздел «✂️»."""
    removed = []
    for line in lines or []:
        found = _CUT_LINE_RE.match(line)
        if found:
            removed.append({"speaker": found.group("speaker").strip(),
                            "tokens": _as_int(found.group("tokens")),
                            "preview": found.group("preview")})
    return removed


def _parse_messages(lines: list) -> list:
    """Сообщения запроса — обратно тому, что пишет message_line."""
    messages, index = [], 0
    while index < len(lines):
        found = _MESSAGE_RE.match(lines[index])
        if not found:
            index += 1
            continue
        # Вес стоит последним полем (так его пишет message_line), перед ним
        # может стоять та же роль словами, а перед ней — чьё это сообщение
        fields = [part.strip() for part in found.group("rest").split(" · ")]
        if fields:
            fields = fields[:-1]
        if fields and fields[-1] in ROLE_WORDS.values():
            fields = fields[:-1]
        name = fields[0] if fields else ""
        if name == "—":
            name = ""
        cursor = index + 1
        note = ""
        if cursor < len(lines) and lines[cursor].startswith("  - "):
            note = lines[cursor][4:].strip()
            cursor += 1
        content, used = _quoted_block(lines[cursor:])
        messages.append({"role": found.group("role").strip(), "name": name,
                         "tokens": text.estimate_tokens(content),
                         "content": content, "note": note})
        index = cursor + used
    return messages


def _parse_place(lines: list) -> dict:
    """Раздел «место под историю» → окно говорящего и то, что в него вошло."""
    budget = {"kind": "local", "window": 0, "reserve": 0, "safety": 0,
              "available": None, "system_tokens": 0, "kept_tokens": 0,
              "history_tokens": 0, "messages_before": 0, "messages_after": 0}
    for line in lines or []:
        found = _WINDOW_RE.match(line)
        if found:
            budget["kind"] = WINDOW_KINDS.get(found.group("kind").strip(), "local")
            budget["window"] = _as_int(found.group("window"))
            # Ноль в запасе — это «на ответ не зарезервировано»: тем же словом
            # это сказано в файле (см. window_line)
            budget["reserve"] = (0 if "не зарезервировано" in found.group("answer")
                                 else _as_int(found.group("answer")))
            budget["safety"] = _as_int(found.group("safety"))
            available = found.group("available").strip()
            budget["available"] = (None if "без предела" in available
                                   else _as_int(available))
        found = _HISTORY_RE.match(line)
        if found:
            budget["system_tokens"] = _as_int(found.group("system"))
            budget["kept_tokens"] = _as_int(found.group("kept"))
            budget["history_tokens"] = _as_int(found.group("tokens"))
            # А это числа самого запроса — они живут в этом же разделе, потому
            # что он пишется до запроса: у хода, оборванного посреди него,
            # другого свидетеля нет (см. _parse_record)
            budget["messages_after"] = int(found.group("scene"))
            # Сколько было до обрезки — не отдельным числом, а суммой:
            # выброшенное и оставленное — и есть вся сцена
            budget["messages_before"] = (budget["messages_after"]
                                         + _as_int(found.group("removed")))
            budget["request_messages"] = int(found.group("messages"))
            budget["request_tokens"] = budget["history_tokens"]
    return budget


def _split_emoji(field: str) -> tuple:
    """Значок и имя из «🦊 Элина»: у поста в ленте они стоят рядом (см. create_post)."""
    first, space, rest_text = str(field or "").partition(" ")
    if space and first and not re.search(r"[0-9A-Za-zА-Яа-яЁё]", first):
        return first, rest_text.strip()
    return "", str(field or "").strip()


def _avatar_from(lines: list) -> str:
    """Портрет говорившего из записи — обратно тому, что пишет avatar_line.

    Ищется по всей записи, а не только в разделе «кто»: у реплики человека
    разделов нет вовсе, а портрет у неё такой же (см. dump_human_markdown).
    Чужого тут оказаться не может: в записи портрет назван один раз, а тексты
    моделей и системный промпт идут в кавычках (см. quoted).
    """
    for line in lines or []:
        found = _AVATAR_RE.match(line)
        if found:
            return found.group("path")
    return ""


def _pictures_in_dump(lines: list, store: bool) -> dict:
    """Картинки, записанные данными: адрес → байты. Возвращает словарь.

    Данные лежат под своей строкой блоками «> » (см. picture_data_lines), и без
    них картинка живёт только в папке экземпляра: перенесённый ДАМП показал бы
    рамку вместо неё. store=False — не разбирать вовсе: так читается запись для
    вопроса «а что в файле?» (см. dump_preview), и раскладывать картинки по диску
    ради одного числа незачем.
    """
    if not store:
        return {}
    found = {}
    for index, line in enumerate(lines or []):
        head = _PICTURE_HEAD_RE.match(line)
        if not head:
            continue
        written, _used = _quoted_block(lines[index + 1:])
        # Берётся ровно столько знаков, сколько записано в строке: следом в том же
        # блоке «> » стоит обычный текст — реплика человека или ответ модели,
        # и без этой отсечки он приклеился бы к данным (см. picture_data_lines)
        data = "".join(piece.strip() for piece in written)[: _as_int(head.group("signs"))]
        try:
            found[head.group("url")] = base64.b64decode(data, validate=True)
        except Exception:
            continue
    return found


def _topic_pictures_in_dump(lines: list, pictures: dict, store: bool) -> list:
    """Приложенное к теме по записи — списком, как его хранит сессия.

    Читаются все строки файла, а не одна шапка: картинку прикладывают и на ходу,
    и тогда её строка стоит в хронологии (см. dump_topic_pictures). Убранное
    вычитается — иначе возвращённый спектакль принёс бы её назад.
    """
    order, since, gone = [], {}, set()
    for line in lines or []:
        found = _TOPIC_PICTURE_RE.match(line)
        if found:
            url = found.group("url").strip()
            if url not in since:
                order.append(url)
            since[url] = (found.group("name").strip(), _as_int(found.group("since")))
            continue
        found = _TOPIC_GONE_RE.match(line)
        if found:
            gone.add(found.group("url").strip())
    out = []
    for url in order:
        if url in gone:
            continue
        name, when = since[url]
        raw = (pictures or {}).get(url)
        if raw:
            picture, error = stored_picture(url, name, raw)
            if picture is None:
                print(f"  ⚠️  Картинка к теме из ДАМПа не вернулась: {error}")
                continue
            picture["since"] = when
            out.append(picture)
            continue
        taken = upload_files([{"name": name, "url": url}])
        if taken:
            taken[0]["since"] = when
            out.append(taken[0])
    return out


def _attachments_from(lines: list, pictures: dict = None, store: bool = False) -> list:
    """Приложенное к реплике из записи — обратно тому, что пишет attachment_lines.

    Ищется по всей записи, как и лицо: у реплики человека разделов нет вовсе,
    а у машинной приложенное стоит рядом с самой репликой (см. dump_turn_tail).
    Каждое имя проверяется тем же сторожем, что и у поста из формы (см. upload_files):
    адрес в файле — такой же чужой текст, и по нему нельзя прочитать что попало.
    Пропавшие файлы отсеиваются молча: ДАМП живёт дольше картинок, и адрес в нём
    бывает уже никуда не ведёт — рамка без картинки хуже, чем её отсутствие.

    Но если тут же лежат и сами данные — картинка возвращается из них, и запись
    оказывается самодостаточной: чужая папка приложенных для неё не нужна.
    """
    found = []
    for line in lines or []:
        match = _ATTACH_RE.match(line)
        if match:
            found.append({"name": match.group("name").strip(),
                          "url": match.group("url").strip()})
    if store:
        restored = []
        for one in found:
            raw = (pictures or {}).get(one["url"])
            if not raw:
                restored.append(one)
                continue
            picture, error = stored_picture(one["url"], one["name"], raw)
            if picture is None:
                print(f"  ⚠️  Картинка из ДАМПа не вернулась: {error}")
                continue
            restored.append(picture)
        found = restored
    return upload_files(found)


def _avatar_that_exists(url) -> str:
    """Адрес портрета, если сам файл ещё на месте (пустая строка — нет его).

    Записи в ДАМПе живут дольше картинок: портрет можно удалить или подобрать
    заново, и тогда адрес из файла уже никуда не ведёт. Пустая рамка вместо
    лица хуже эмодзи, поэтому адрес проверяется, а не берётся на слово.
    """
    path = str(url or "").strip()
    if not path:
        return ""
    name = path.rstrip("/").split("/")[-1]
    return path if name and (settings.AVATAR_DIR / name).exists() else ""


def _split_gender(name: str) -> tuple:
    """Имя и пол из «Элина ♀» — обратно тому, что пишет cast_field."""
    written = str(name or "").strip()
    for mark, gender in (("♀", "female"), ("♂", "male")):
        if written.endswith(mark):
            return written[:-1].strip(), gender
    return written, ""


def _parse_heading(rest: str) -> tuple:
    """Шапка записи: кто говорил, сколько ход шёл и сколько он стоил."""
    fields = [part.strip() for part in str(rest or "").split(" · ")]
    who = {"name": "", "model": "", "role_name": "", "round": 0, "time": "",
           "emoji": "", "gender": ""}
    if len(fields) >= 5:
        who["time"] = fields[0]
        who["emoji"], name = _split_emoji(fields[1])
        who["name"], who["gender"] = _split_gender(name)
        who["model"] = fields[2]
        who["role_name"] = fields[3]
        round_found = re.search(r"\d+", fields[4])
        who["round"] = int(round_found.group()) if round_found else 0
    # В остальных полях — сводка хода: у неё свои значки, и по ним находятся
    # ровно те два числа, которых задним числом не посчитать
    seconds, spent = None, None
    for part in fields[5:]:
        if part.startswith("⏱ "):
            seconds = duration_seconds(part[2:])
        elif part.startswith("💰 "):
            spent = rubles(part[2:])
    return who, seconds, spent


def restored_summary(turn: dict, seconds=None, spent=None) -> dict:
    """Сводка хода по его же частям — тем же счётом, которым его вёл живой ход.

    Числа не вычитываются из шапки по буквам, а считаются заново: счёт один
    и тот же (см. build_turn_report, added_kinds, _input_tokens_total), и разойтись
    с тем, что театр показывал на ходу, они не могут. Из шапки берутся только
    те два, которые задним числом не посчитать: сколько ход шёл и сколько стоил.
    """
    messages = turn.get("messages") or []
    added = turn.get("added") or []
    removed = turn.get("removed") or []
    steps = turn.get("steps") or []
    budget = turn.get("budget") or {}
    return {
        "messages": len(messages),
        "tokens": sum(int(m.get("tokens") or 0) for m in messages),
        "window": int(budget.get("window") or 0),
        "window_kind": budget.get("kind") or "local",
        "removed_messages": len(removed),
        "removed_tokens": sum(int(r.get("tokens") or 0) for r in removed),
        "extra_messages": len(added),
        "extra_tokens": sum(int(m.get("tokens") or 0) for m in added),
        "added_kinds": added_kinds(added),
        "search_rounds": _steps_count(steps, "search"),
        "asks": _steps_count(steps, "ask"),
        "thought_steps": _steps_count(steps, "thought"),
        "search_refusals": _steps_count(steps, "refused"),
        "silences": _steps_count(steps, "silence"),
        "tokens_in_total": _input_tokens_total(steps),
        "seconds": seconds,
        "spent": spent,
    }


def _post_dict(post_id: int, who: dict, content: str, sketch: str, thinking: str,
               summary, queries: list, faces: dict = None,
               interrupted: bool = False, attachments: list = None) -> dict:
    """Реплика ленты из разобранной записи — теми же полями, что create_post.

    interrupted=True — ход оборвала смерть процесса, а не модель (см. _parse_record):
    у такой записи нет хвоста, а значит и реплики; в ленте это надо сказать словами,
    иначе пустая рамка читалась бы как «модель промолчала в этом ходу».

    Лицо берётся у состава из шапки файла (см. parse_cast_line), а если имени
    там нет — у состава, который сейчас в пульте: имя то же — значит и лицо то же.
    Если файла за адресом уже нет, остаётся эмодзи: пустая рамка вместо лица
    хуже значка.
    """
    faces = faces or {}
    role = who.get("role") or "participant"
    name = who.get("name") or ""
    gender = who.get("gender") or "male"
    dump_avatar, dump_emoji = faces.get(name, (None, None))
    return {
        "id": post_id,
        "display_name": name,
        "model_used": who.get("model") or "",
        # Портрет — из состава в шапке записи (у прежнего формата — из строки
        # «Аватар» самой реплики), а если файла уже нет — из пульта
        "avatar_url": (_avatar_that_exists(who.get("avatar"))
                       or _avatar_that_exists(dump_avatar)
                       or _avatar_that_exists(session.avatars.get(name)) or None),
        "avatar_emoji": (who.get("emoji") or dump_emoji
                         or session.avatar_emojis.get(name)
                         or ROLE_ICONS.get(role, "📣")),
        "content": content,
        "content_html": text.markdown_to_html(content),
        "thinking": thinking,
        "sketch": sketch,
        "round": int(who.get("round") or 0),
        "timestamp": who.get("time") or "",
        "search_count": len(queries or []),
        "search_queries": list(queries or []),
        "turn": summary,
        "role": role,
        "role_icon": ROLE_ICONS.get(role, "🎭"),
        "role_name": ROLE_NAMES.get(role, "Участник"),
        "gender": gender,
        "gender_symbol": gender_symbol(gender),
        "interrupted": bool(interrupted),
        # Приложенное к реплике возвращается вместе с ней (см. _attachments_from):
        # адрес тот же, что и в ленте, поэтому картинка видна и у прежнего спектакля
        "attachments": list(attachments or []),
    }


def _parse_record(post_id: int, rest: str, body: list, faces: dict = None,
                  pictures: dict = None, store: bool = False) -> dict:
    """Одна запись ДАМПа → реплика ленты и, если ход был машинным, отчёт о нём."""
    who, seconds, spent = _parse_heading(rest)
    sections = _split_sections(body)
    if not who.get("gender"):
        # Пол виден в строке «кто» — тем же значком, что у поста в ленте
        who["gender"] = ("female" if "♀" in "\n".join(sections.get("who") or [])
                          else "male")
    who["role"] = ROLE_BY_NAME.get(who.get("role_name") or "", "participant")
    who["avatar"] = _avatar_from(body)
    who["attachments"] = _attachments_from(body, pictures, store)
    who["topic"] = ""
    if not [key for key in sections if key]:
        # Ни одного знакомого раздела: либо реплика человека (у неё запросов
        # и поиска нет, поэтому и разделов нет), либо запись другой версии
        # театра — тогда реплика там последним разделом (см. _last_quoted)
        return {"post": _post_dict(post_id, who,
                                   _last_quoted(body) or _section_text(body),
                                   "", "", None, [], faces,
                                   attachments=who["attachments"]),
                "turn": None}
    # Записи без раздела «💬 Реплика, которой ход кончился» обрывает не модель,
    # а смерть процесса: хвост записи пишется одним куском в самом конце хода
    # (см. dump_turn_tail), и у хода, убитого посреди генерации, этого раздела
    # просто нет. Молчащей модели тут быть не может: даже про пустой ответ театр
    # говорит словами, и слова эти лежат в том же разделе
    interrupted = "answer" not in sections
    steps = _parse_steps(sections.get("history") or [])
    turn = {
        "who": who,
        "budget": _parse_place(sections.get("place") or []),
        "messages": _parse_messages(sections.get("request") or []),
        "added": _parse_messages(sections.get("added") or []),
        "removed": _parse_cut(sections.get("cut") or []),
        "steps": steps,
        "sketch": _section_text(sections.get("sketch") or []),
        "answer": _section_text(sections.get("answer") or []),
        # Размышления в ленте лежат шагами хронологии, а у реплики есть своё
        # поле под них (см. create_post) — вот его и собираем из тех же шагов
        "thinking": "\n\n".join(step.get("text", "") for step in steps
                                 if step.get("kind") == "thought"),
    }
    turn["summary"] = restored_summary(
        turn, seconds if seconds is not None else _record_seconds(body), spent)
    if interrupted:
        # Раздела «📨 первый запрос» в оборванной записи нет, и считать сообщения
        # не по чему — но их знает раздел «📐 место под историю»: он пишется
        # ДО запроса. Без этого в свёрнутой строке хода стояло бы «0 токенов
        # на ввод» про запрос, который на самом деле уехал — то есть ложь на месте
        # числа (в файле рядом стоит настоящее: см. history_line)
        budget = turn.get("budget") or {}
        turn["summary"]["messages"] = int(budget.get("request_messages") or 0)
        turn["summary"]["tokens"] = int(budget.get("request_tokens") or 0)
    queries = [step.get("query") or "" for step in steps if step.get("kind") == "search"]
    post = _post_dict(post_id, who, turn["answer"], turn["sketch"], turn["thinking"],
                      turn["summary"], queries, faces, interrupted=interrupted,
                      attachments=who["attachments"])
    turn["interrupted"] = interrupted
    return {"post": post, "turn": turn}


def _add_record(current: tuple, body: list, posts: list, turns: dict,
                faces: dict = None, pictures: dict = None, store: bool = False,
                slot: int = None, versions: list = None) -> None:
    """Одна запись — в спектакль: реплика всегда, отчёт о ходе — если он был.

    slot — место, с которого эта запись была убрана как переигранная: сказанное
    заново встаёт ровно на своё место, а не в конец ленты (см. dump_replayed,
    parse_dump). Без него запись дописывается в конец — как и раньше.

    versions — прежние слова этой реплики, собранные по отметкам о переигранном
    (см. parse_dump). У реплики, которая ни разу не менялась, их нет вовсе —
    и блока «прежние слова» у неё тогда тоже нет.
    """
    post_id, rest = current
    record = _parse_record(post_id, rest, body, faces, pictures, store)
    if versions:
        record["post"]["versions"] = list(versions)
    if slot is None:
        posts.append(record["post"])
    else:
        posts.insert(min(slot, len(posts)), record["post"])
    if record["turn"]:
        turns[int(post_id)] = record["turn"]


def dump_version(written: str) -> int:
    """Номер формата из шапки файла (0 — номер не назван вовсе).

    Ноль — это не «старый формат», а «файл не наш»: так выглядит чужой файл
    или ДАМП времён, когда номера ещё не было (см. DUMP_FORMAT).
    """
    for line in str(written or "").splitlines()[:12]:
        found = re.match(r"^Формат записи: (\d+)$", line.strip())
        if found:
            return int(found.group(1))
    return 0


def parse_cast_line(text) -> dict:
    """Состав из шапки — обратно тому, что пишет cast_line: имя → (портрет, эмодзи)."""
    faces = {}
    for place in str(text or "").split(" ‖ "):
        fields = [field.strip() for field in place.split(" · ")]
        if len(fields) < 4:
            continue
        emoji, name = _split_emoji(fields[0])
        name, _ = _split_gender(name)
        if name:
            faces[name] = ("" if fields[3] == "—" else fields[3], emoji)
    return faces


def parse_dump(written: str, tolerant: bool = False, store: bool = False) -> dict:
    """Прочитать ДАМП обратно — прежний спектакль целиком.

    None — значит «файл не наш»: другой формат записи (см. DUMP_FORMAT), пусто
    или ни одной записи. Половина спектакля хуже, чем никакого: непрочитанная
    реплика в ленте выглядела бы как «модель промолчала в этом ходу».

    tolerant=True — согласие читать чужой формат (см. ask_about_dump): так тоже
    может получиться толк, но обещать нечего. Записи узнаются по своему началу
    («## номер · …»), поэтому даже файл без номера формата читается по строкам —
    а вот если записей в нём нет вовсе, читать и вправду нечего.

    store=True — разложить картинки из записи по папке экземпляра и поставить
    репликам их адреса (см. stored_picture): так спектакль возвращается целиком,
    даже если папку приложенных успели почистить или ДАМП перенесли в другой
    театр. store=False — только прочитать: так спрашивают «а что в файле?».
    """
    lines = str(written or "").splitlines()
    version = dump_version(written)
    if not tolerant and version not in DUMP_FORMATS_READABLE:
        return None
    topic = ""
    for index, line in enumerate(lines):
        if line.strip() == "**Тема:**":
            topic = _section_text(lines[index + 1:])
            break
    # Состав берётся последним из записанных: спектакль мог продолжаться,
    # и тогда лицо участника менялось уже во второй записи (см. start_dump)
    faces = {}
    for line in lines:
        if line.startswith("**Состав:** "):
            faces = parse_cast_line(line[len("**Состав:** "):])
    # Картинки разбираются до записей: реплика ссылается на адрес, а данные
    # к нему лежат ниже в том же файле (см. _pictures_in_dump)
    pictures = _pictures_in_dump(lines, store)
    posts, turns, slots, versions = [], {}, {}, {}
    current, body = None, []
    for line in lines:
        found = _RECORD_RE.match(line)
        if found:
            if current is not None:
                _add_record(current, body, posts, turns, faces, pictures, store,
                            slot=slots.pop(current[0], None),
                            versions=versions.pop(current[0], None))
            current, body = (int(found.group(1)), found.group(2)), []
            continue
        gone = _REPLAYED_RE.match(line)
        if gone:
            # Незакрытая запись сначала становится репликой, а уж потом убирается:
            # отметка стоит сразу под ней, и без этого убирать было бы нечего —
            # прежний ответ остался бы в спектакле рядом с новым
            if current is not None:
                _add_record(current, body, posts, turns, faces, pictures, store,
                            slot=slots.pop(current[0], None),
                            versions=versions.pop(current[0], None))
                current, body = None, []
            # Переигранные: прежние записи этих номеров убираются из спектакля,
            # а новые стоят ниже и остаются (см. dump_replayed). Место каждой
            # запоминается: переигранная ляжет тем же номером — то есть на своё
            # место, а не в конец ленты
            drop = {int(one) for one in re.findall(r"\d+", gone.group("ids"))}
            for post in [one for one in posts if int(one.get("id") or 0) in drop]:
                key = int(post.get("id") or 0)
                # Прежние слова убранной реплики не пропадают: они остаются
                # летописью, и её отдаёт лента блоком «прежние слова»
                # (см. version_view, page.postVersionsHtml). Собирается она
                # вместе с прежними версиями этой же реплики: переигранная
                # дважды, она помнит обе (см. remember_version)
                versions[key] = remember_version(post)
                slots[key] = posts.index(post)
                posts.remove(post)
                turns.pop(key, None)
            continue
        if line.startswith("**Продолжение"):
            # Шапка продолжения — не текст записи, а граница: она дописывается
            # в конец файла, а до неё не всегда успевает лечь новая запись
            # (см. start_dump). Без этой границы она — и всё, что после неё, —
            # стала бы хвостом последней записи, а реплика человека снова
            # читалась бы последним «> »-блоком записи — то есть картинкой
            if current is not None:
                _add_record(current, body, posts, turns, faces, pictures, store,
                            slot=slots.pop(current[0], None),
                            versions=versions.pop(current[0], None))
                current, body = None, []
            continue
        if current is not None:
            body.append(line)
    if current is not None:
        _add_record(current, body, posts, turns, faces, pictures, store,
                    slot=slots.pop(current[0], None),
                    versions=versions.pop(current[0], None))
    if not posts:
        return None
    return {
        "format": version,
        "topic": topic,
        "cast": faces,
        "posts": posts,
        "turns": turns,
        # Приложенное к теме — тоже часть спектакля, а не пульта: едет в записи
        # и возвращается вместе с ней (см. _topic_pictures_in_dump)
        "topic_pictures": _topic_pictures_in_dump(lines, pictures, store),
        "round": max(int(post.get("round") or 0) for post in posts),
        # Цена спектакля — сумма цен его ходов: другого источника у неё нет
        "spent": round(sum(float((t.get("summary") or {}).get("spent") or 0.0)
                           for t in turns.values()), 2),
    }


def _title_topic(written: str) -> str:
    """Тема из шапки ДАМПа: «# ДАМП · 16.09.2026 22:01 · тема».

    У записей прежних версий раздела «Тема» могло и не быть, а название
    спектакля в шапке было всегда — и для вопроса «а что там?» его хватает
    (см. dump_preview).
    """
    first = str(written or "").strip().splitlines()
    if not first or not first[0].startswith("#"):
        return ""
    parts = [piece.strip() for piece in first[0].split("·")]
    return parts[2] if len(parts) > 2 else ""


def dump_preview(written: str) -> str:
    """Что в ДАМПе — одной строкой: сколько реплик, актов, о чём и на сколько.

    Нужен вопросу о чужом формате (см. ask_about_dump): «открыть прежний
    спектакль» без этих чисел — выбор вслепую, а решается он именно ими.
    Чтение терпимое: речь как раз о записи, которую мы не обещаем понимать.
    """
    play = parse_dump(written, tolerant=True)
    if not play:
        return ""
    parts = [f"реплик {len(play['posts'])}, актов {play['round']}"]
    # Тема — с начала и до первой строки: в ДАМПе она может быть длинной цитатой
    topic = " ".join(str(play.get("topic") or "").split()) or _title_topic(written)
    if topic:
        parts.append(f"тема «{topic[:60].rstrip() + '…' if len(topic) > 60 else topic}»")
    if play.get("spent"):
        parts.append(f"цена {money(play['spent'])}")
    return ", ".join(parts)


def ask_about_dump(version: int, written: str = "") -> str:
    """Спросить, что делать с ДАМПом незнакомого формата.

    Формат меняется вместе с тем, как устроена запись, и обещать совместимость
    нельзя: файл может прочитаться наполовину — а непрочитанная реплика
    в ленте выглядела бы как «модель промолчала в этом ходу». Поэтому решает
    режиссёр: отказаться от прежнего спектакля, открыть его только на чтение
    или открыть и доиграть. Без консоли (запуск из IDE, из службы) спрашивать
    некого — тогда прежний спектакль просто не трогается.

    written — сам файл: по нему видно, что именно предстоит открыть (число
    реплик, актов и тему). Без этого «открыть прежний спектакль» — выбор вслепую,
    а вопрос «а он мне нужен?» решается именно этими числами (см. dump_preview).
    """
    known = ", ".join(str(v) for v in sorted(DUMP_FORMATS_READABLE))
    print(f"  ⚠️  {settings.DUMP_FILE.name} записан форматом "
          f"{version or 'без номера'}, а этот театр читает {known}.")
    print("   Формат — это устройство записи: чужая запись читается, но часть её")
    print("   полей может оказаться не тем, чем была записана.")
    preview = dump_preview(written)
    if preview:
        print(f"   В файле: {preview}.")
    print("   [Enter] ничего не читать и ничего не удалять: сцена будет пустой,")
    print("            а файл останется на месте, пока его не перепишет новый")
    print("            спектакль — в нём же и прежний, другой его копии нет.")
    print("   [ч] читать прежний спектакль: реплики в ленте со своими ходами,")
    print("       продолжать нельзя. Потом можно и доиграть — кнопкой")
    print("       «↩ Доиграть прежний» под занавесом, выбор не окончательный.")
    print("   [п] читать и доиграть: прежние реплики станут историей для моделей,")
    print("       акт и цена продолжатся, запись допишется в тот же файл.")
    if not (sys.stdin and sys.stdin.isatty()):
        print("   Консоли нет, спросить некого — оставляю как есть.")
        return "skip"
    try:
        answer = input("   Ваш выбор: ").strip().lower()
    except (EOFError, OSError, KeyboardInterrupt):
        print()
        return "skip"
    for key, mode in DUMP_CHOICES:
        if answer == key:
            return mode
    return "skip"


def load_play_from_dump(mode: str = None) -> int:
    """Вернуть в ленту прежний спектакль — по ДАМПу прошлого запуска.

    Раньше ДАМП был только для чтения глазами: реплики жили в памяти процесса,
    и перезапуск означал пустую сцену. Теперь файл — это и есть спектакль,
    и новый процесс (с новой вёрсткой) показывает прежние реплики с их ходами.

    mode — что делать с чужим форматом: None значит «спросить» (см. ask_about_dump),
    "skip" — не трогать, "read" — только на чтение, "continue" — ещё и доиграть.
    Возвращает число возвращённых реплик: 0 — возвращать нечего.
    """
    try:
        written = settings.DUMP_FILE.read_text(encoding="utf-8")
    except OSError:
        return 0
    # Пустой файл — это не чужой формат, а «спектакля тут нет»: пугать
    # предупреждением нечем, а вот в чужом формате признаться надо
    if not written.strip():
        return 0
    version = dump_version(written)
    readable = version in DUMP_FORMATS_READABLE
    if mode is None:
        mode = "read" if readable else ask_about_dump(version, written)
    if mode == "skip":
        if not readable:
            print(f"  ⚠️  Прежний спектакль не возвращается: {settings.DUMP_FILE.name} "
                  f"остался как был, сцена пустая")
        return 0
    # store=True: картинки раскладываются по папке экземпляра — запись
    # самодостаточна, и спектакль возвращается вместе с ними
    play = parse_dump(written, tolerant=not readable, store=True)
    if not play:
        # Файл нашего формата без единой записи — это не чужой формат, а спектакль,
        # убитый до первой реплики (например, посреди хода модератора): в шапке темы
        # и состава хватает, а читать нечего. Сказать про него «чужого формата»
        # значило бы соврать — и послать искать причину не там, где она есть
        if readable:
            print(f"  🗒  В {settings.DUMP_FILE.name} нет ни одной реплики — "
                  f"сцена пустая, прошлый спектакль оборвался в самом начале")
        else:
            print(f"  ⚠️  Прежний спектакль не возвращается: {settings.DUMP_FILE.name} "
                  f"записан не нашим форматом (ожидается {DUMP_FORMAT}), сцена будет пустой")
        return 0
    session.posts = play["posts"]
    session.turn_log = play["turns"]
    session.spent = play["spent"]
    session.current_round = play["round"]
    # Спектакль прошлый, и это видно: он доигран, а не идёт сейчас
    session.finished = True
    session.running = False
    session.waiting_for_human = False
    session.restored = True
    # А кто приложенное уже видел — там же, где и на ходу: по самой ленте
    # (см. restore_shown_pictures). Это не только про «не показывать дважды»:
    # по тому же следу видно, кому картинка уехала, и сколько их было всего
    # (см. picture_travellers, pictures_report) — а вернувшемуся спектаклю
    # сказать об этом нечем, и он показывал бы ноль уехавших картинок
    # История тут же — из самих реплик: conversation_history заведётся позже
    # (см. start_show), а след о показанном надо достать сейчас
    session.restore_shown_pictures(session.history_from_posts())
    # Лица участников, которых в составе уже нет, остаются в самих репликах,
    # а те, кто есть, берутся из пульта: имя то же — лицо то же (см. post_view)
    if not session.topic and play.get("topic"):
        session.topic = play["topic"]
    # А приложенное к теме — из самой записи: оно про этот спектакль, а в пульте
    # его может и не быть (или быть другим — пульт правят и после занавеса).
    # Пусто в записи — значит остаётся то, что уже лежит в сессии
    if play.get("topic_pictures"):
        session.topic_attachments = play["topic_pictures"]
    # «Доиграть» — значит во время старта прежние реплики останутся в ленте,
    # а историей для моделей станет то же, что уже сказано (см. start_show)
    session.resume_ready = mode == "continue"
    if readable and version != DUMP_FORMAT:
        print(f"  ℹ️  {settings.DUMP_FILE.name} записан форматом {version} — "
              f"читаю как есть, это под-версия записи")
    print(f"🗒  Вернулся прежний спектакль из {settings.DUMP_FILE.name}: "
          f"реплик {len(play['posts'])}, актов {play['round']}, "
          f"цена {money(play['spent'])}"
          + (", доиграем прежним составом" if session.resume_ready else ""))
    return len(play["posts"])


# ── АРХИВ СПЕКТАКЛЕЙ (СЕЙВЫ) ────────────────────────────────
#
# Спектакль целиком — это два файла экземпляра и приложенные картинки: пульт
# (см. save_theatre_settings) и ДАМП, в котором лежит сама история (см. parse_dump).
# Значит, сохранить спектакль — это просто положить их рядом под именем, а вернуть —
# положить обратно. Хранятся они НЕ по порту (см. settings.SAVES_DIR): театр на 5077 —
# та же игра на той же машине, и спектакль, сыгранный там, должен открываться и на 5000.
# Поэтому же у одного спектакля бывает сколько угодно сохранений: одно — то, что было
# вчера, другое — ветка, которую хочется посмотреть.


def saves_dir() -> Path:
    """Папка архива — одна на все порты (создаётся при первом обращении)."""
    settings.SAVES_DIR.mkdir(parents=True, exist_ok=True)
    return settings.SAVES_DIR


def save_folder(name: str) -> str:
    """Имя папки сохранения из имени спектакля — своё, а не присланное.

    Имя — текст режиссёра: в нём бывает и «../../», и что угодно ещё, поэтому
    папка собирается только из букв и цифр, а для похожих имён к ней дописывается
    короткий отпечаток. Папка — не имя: как сохранение называется, лежит в save.json.
    """
    clean = re.sub(r"[^\w]+", "-", str(name or "").lower(), flags=re.UNICODE).strip("-")
    clean = clean[:40] or "спектакль"
    return f"{clean}-{uuid.uuid4().hex[:6]}"


def _save_folder_path(save_id: str) -> Path:
    """Папка сохранения по его имени — или None, если такое имя нам не годится.

    Имя приходит из формы (см. web.saves): внутрь архива ему ходить нельзя
    ни в какую сторону — ни за его пределы, ни по чужому пути.
    """
    save_id = str(save_id or "").strip()
    if not save_id or save_id != Path(save_id).name or save_id.startswith("."):
        return None
    folder = settings.SAVES_DIR / save_id
    if not (folder / "save.json").is_file():
        return None
    return folder


# В каком состоянии спектакль: под занавесом — или он паркуется на ходу.
# Сохранение разрешено как раз в них (см. save_play), а слова эти показываются
# в архиве: вернувшийся спектакль должен быть тем же, чем был сохранён
PHASE_WORDS = {
    "live": "говорит модель",
    "human": "на ходу человека",
    "finished": "занавес опущен",
    "setup": "спектакль ещё не начат",
}


def play_phase() -> str:
    """Где остановился спектакль: «live», «human», «finished» или «setup».

    Это про то, можно ли брать копию файлов (см. save_play): на ходу человека
    и под занавесом в них никто не пишет — а пока говорит модель, ДАМП
    дописывается её ходом, и копия была бы половиной записи.
    """
    if session.running:
        return "human" if session.waiting_for_human else "live"
    return "finished" if session.posts else "setup"


def save_play(name: str, port: int = None) -> tuple:
    """Сохранить нынешний спектакль в архив — целиком, под именем режиссёра.

    Копируется всё, что составляет спектакль: пульт, ДАМП и приложенные картинки.
    Картинки — потому что посты ссылаются на них адресами (см. post_images),
    и в другом порту те же адреса вели бы в пустое место: своё сохранение
    приносит и своё приложенное.

    Завершать спектакль для этого не надо: сохранение — копия, а не занавес,
    и оно берётся в любой безопасный момент — на ходу человека или под занавесом
    (см. play_phase). Пока говорит модель, отказ: ДАМП в это время дописывается
    её ходом, и половина записи — это не спектакль.
    """
    name = str(name or "").strip()
    if not name:
        return None, "сохранению нужно имя — по нему его потом и найдут"
    phase = play_phase()
    if phase == "live":
        return None, ("сейчас говорит модель: сохранить можно на вашем ходу "
                      "или под занавесом")
    folder = settings.SAVES_DIR / save_folder(name)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if settings.SETTINGS_FILE.is_file():
            shutil.copyfile(settings.SETTINGS_FILE, folder / "settings.json")
        if settings.DUMP_FILE.is_file():
            shutil.copyfile(settings.DUMP_FILE, folder / "damp.md")
        pictures = 0
        if settings.UPLOAD_DIR.is_dir():
            target = folder / "uploads"
            target.mkdir(parents=True, exist_ok=True)
            for one in settings.UPLOAD_DIR.iterdir():
                if one.is_file():
                    shutil.copyfile(one, target / one.name)
                    pictures += 1
        meta = {
            "id": folder.name,
            "name": name,
            "saved_at": time.strftime("%d.%m.%Y %H:%M"),
            "at": time.time(),
            "port": int(port if port is not None else settings.PORT),
            "topic": str(session.topic or ""),
            "posts": len(session.posts),
            "round": int(session.current_round or 0),
            "spent": round(float(session.spent or 0.0), 2),
            "cast": [str(p.get("display_name") or "") for p in session.runtime_participants],
            "pictures": pictures,
            # Состояние спектакля на момент сохранения: по нему видно, вернётся
            # ли он под занавес или на ходу человека (см. load_save)
            "phase": phase,
            "phase_text": PHASE_WORDS.get(phase, ""),
        }
        (folder / "save.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Спектакль не сохранился: {e}")
        return None, f"спектакль не сохранился: {e}"
    print(f"💾 Сохранён спектакль «{name}»: реплик {meta['posts']}, "
          f"актов {meta['round']}, приложенного {pictures}, "
          f"{meta['phase_text']} — {folder}")
    return meta, ""


def list_saves() -> list:
    """Что лежит в архиве — от свежих сохранений к старым.

    Чужой мусор в папке архива — не наша забота: сохранением считается только
    то, у чего есть save.json (так же, как чужая запись не ДАМП).
    """
    found = []
    root = saves_dir()
    for folder in root.iterdir():
        if not folder.is_dir() or not (folder / "save.json").is_file():
            continue
        try:
            meta = json.loads((folder / "save.json").read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠️  Не читается сохранение {folder.name}: {e}")
            continue
        if not isinstance(meta, dict):
            continue
        meta["id"] = folder.name
        meta.setdefault("name", folder.name)
        meta.setdefault("at", 0)
        found.append(meta)
    return sorted(found, key=lambda meta: float(meta.get("at") or 0), reverse=True)


def load_save(save_id: str) -> tuple:
    """Вернуть сохранённый спектакль на сцену — в этом порту.

    Кладутся обратно те же три вещи, что и были взяты (см. save_play), а потом
    пульт и история читаются тем же кодом, что и при запуске театра: иначе
    сохранённый спектакль жил бы своей жизнью, отличной от прежнего (см.
    load_theatre_settings и load_play_from_dump).

    Идущий спектакль этому помешает: ДАМП и пульт подменяются как раз в тот момент,
    когда по ним пишут. Поэтому о таком отказе надо сказать словами, а не портить
    и архив, и сцену (см. web.saves).
    """
    folder = _save_folder_path(save_id)
    if folder is None:
        return None, "такого сохранения нет в архиве"
    if session.running:
        return None, "спектакль идёт: подожди до занавеса"
    try:
        meta = json.loads((folder / "save.json").read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"сохранение не читается: {e}"
    try:
        if (folder / "settings.json").is_file():
            shutil.copyfile(folder / "settings.json", settings.SETTINGS_FILE)
        if (folder / "damp.md").is_file():
            shutil.copyfile(folder / "damp.md", settings.DUMP_FILE)
        source = folder / "uploads"
        if source.is_dir():
            settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            for one in source.iterdir():
                if one.is_file():
                    shutil.copyfile(one, settings.UPLOAD_DIR / one.name)
    except Exception as e:
        print(f"  ⚠️  Сохранение не вернулось: {e}")
        return None, f"сохранение не вернулось: {e}"
    _dump_reset()          # прежний файл больше не наш: его перезаписали
    load_theatre_settings()
    # Приложенное вернувшегося спектакля пока не показано никому: кому оно
    # достанется, решит его же история — при «Доиграть прежний» (см.
    # restore_shown_pictures)
    session.forget_shown_pictures()
    posts = load_play_from_dump("read")
    # Чем спектакль был сохранён, тем он и вернётся: «занавес опущен» так
    # и останется занавесом, а «на ходу человека» — тем же ходом, которому
    # не хватает одной кнопки (см. status_payload, play_phase)
    session.restored_phase = str(meta.get("phase") or "")
    print(f"📂 Вернулся сохранённый спектакль «{meta.get('name')}»: "
          f"реплик {posts}, {meta.get('phase_text') or 'состояние не записано'}")
    meta["posts"] = posts
    return meta, ""


def delete_save(save_id: str) -> tuple:
    """Удалить сохранение — когда спектакль уже точно не нужен.

    Папка удаляется целиком и только внутри архива: имя приходит из формы,
    и ходить по нему куда угодно (см. _save_folder_path) нельзя.
    """
    folder = _save_folder_path(save_id)
    if folder is None:
        return False, "такого сохранения нет в архиве"
    try:
        shutil.rmtree(folder)
    except Exception as e:
        print(f"  ⚠️  Сохранение не удалилось: {e}")
        return False, f"сохранение не удалилось: {e}"
    print(f"🗑  Сохранение {folder.name} удалено")
    return True, ""


def forget_play() -> None:
    """Забыть прежний спектакль — и в памяти, и на диске.

    «Новый спектакль» и «Полный сброс» — это «начать заново», и прежний
    спектакль после них не должен возвращаться при перезапуске: иначе кнопка
    значила бы «забыть, пока я не закрыл окно». Файл удаляется целиком: он и есть
    спектакль, а следующий начнёт его заново (см. start_dump).
    """
    session.posts = []
    session.turn_log = {}
    session.spent = 0.0
    session.money_told = False
    session.restored = False
    session.restored_phase = ""
    # Прощён и он, и показанное в нём: прежние адреса никуда не ведут, а новый
    # спектакль начинается с чистой памяти (см. forget_shown_pictures)
    session.forget_shown_pictures()
    # Прежний спектакль прощён — и доигрывать его тоже больше нечего
    session.resume_ready = False
    _dump_reset()
    try:
        settings.DUMP_FILE.unlink()
    except OSError:
        pass

# Смысл кода ответа вендора словами: сам по себе он ничего не говорит, а ход
# им и объясняется. `tool_calls` — та самая строка, которую видели у харитона:
# сообщение есть, текста в нём ноль, и понять это можно было только рядом
# с хронологией.
FINISH_MEANINGS = {
    "stop": "вендор считает, что модель договорила",
    "length": "ответ оборвался по пределу вывода",
    "tool_calls": "слов модель не сказала: она попросила вызвать инструмент",
    "function_call": "слов модель не сказала: она попросила вызвать функцию",
    "content_filter": "вендор вырезал содержимое своим фильтром",
}


def finish_meaning(reason: str = "") -> str:
    """Что значит код ответа вендора — словами, а не «конец: tool_calls»."""
    return FINISH_MEANINGS.get(str(reason or "").strip().lower(), "")


# Поля сообщения, которые не текст, а другое: у протокола инструментов их три,
# и как раз они оказываются у сообщения там, где текста нет вовсе
_MESSAGE_FIELDS = ("images", "tool_calls", "function_call", "tool_name",
                   "tool_call_id")


def _calls_summary(calls) -> str:
    """Вызовы инструмента одной строкой: имя и запрос — «search_web «Сыктывкар...»»."""
    written = []
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        func = call.get("function") if isinstance(call.get("function"), dict) else call
        name = str(func.get("name") or "?")
        args = func.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (TypeError, ValueError):
                args = {}
        query = str((args or {}).get("query") or "") if isinstance(args, dict) else ""
        written.append(f"{name} «{query}»" if query else name)
    return ", ".join(written)


def message_fields(msg: dict) -> list:
    """Поля сообщения кроме текста — с их значениями: «tool_calls: 1 вызов — …»."""
    found = []
    for key in _MESSAGE_FIELDS:
        value = msg.get(key)
        if not value:
            continue
        if key in ("tool_calls", "function_call"):
            calls = value if isinstance(value, list) else [value]
            found.append(f"`{key}` = {len(calls)} вызов(ов): {_calls_summary(calls)}")
        elif key == "images":
            # Считаются сами картинки, а не их данные: в записи и в отчёте
            # нужен след «уехала картинка», а не мегабайты base64 (см. _message_view).
            # А рядом с числом — во что картинку привели перед отправкой
            # (см. sent_pictures_words): по этой строке видно, что получила модель
            count = len(value) if isinstance(value, (list, tuple)) else int(value or 0)
            marks = sent_pictures_words(value)
            found.append(f"`{key}` = {count} — приложено к этой реплике"
                         + (f": {marks}" if marks else ""))
        else:
            found.append(f"`{key}` = {value}")
    return found


def sent_pictures_words(images: list) -> str:
    """Что именно уехало модели картинками: «png 64×64 · 169 Б; …».

    В сообщении от картинки остались только её адрес-данные: приложенные
    к реплике едут именно так (см. _format_history), а у Ollama это тот же адрес
    без предисловия. По нему и видно, какою картинку получила модель, — не
    режиссёрский файл, а приведённый к обычному размеру (см. pictures.travel_words).
    Запоминается с первого же раза (см. _PAYLOAD_MARKS): считать заново на каждой
    отрисовке отчёта тут делать нечего. А не картинка — о такой сказать нечего.
    """
    words = []
    for one in (images if isinstance(images, (list, tuple)) else []):
        if not isinstance(one, str):
            continue
        marks = _PAYLOAD_MARKS.get(one)
        if marks is None:
            marks = pictures.ready_marks(one)
            if not marks.get("kind"):
                continue
            _PAYLOAD_MARKS[one] = marks
        words.append(pictures.travel_words(marks))
    return "; ".join(words)


def message_note(msg: dict) -> str:
    """Чем это сообщение полно, если текста в нём нет.

    «assistant · харитон · 0 токенов» — верный сигнал (что-то было!) и ни капли
    смысла: по протоколу OpenAI ход, целиком состоящий из просьбы о поиске, —
    это пустой текст плюс поле `tool_calls`. Здесь сказано, какие поля в нём есть
    с их значениями и что из этого следует.
    """
    filled = bool(str(msg.get("content") or "").strip())
    fields = message_fields(msg)
    # У ответа инструмента (роль tool или поле tool_name) объяснять надо другое:
    # его тело — это найденное поиском, и лежит оно отдельно, в хронологии
    if msg.get("tool_name") or msg.get("role") == "tool":
        head = ("это не слова модели, а ответ инструмента поиска: тело сообщения — "
                "найденное, и текст его стоит в хронологии, вместе с формулировкой "
                "запроса" if filled
                else "это ответ инструмента поиска, и тела текста в нём нет")
        return head + (f" — {'; '.join(fields)}" if fields else "")
    if filled:
        # Картинка — не текст, и сама она в записи не видна: по этой строке
        # читающий знает, что реплика уехала модели вместе с приложенным —
        # и каким именно: во что его привели перед отправкой (см. _format_history,
        # sent_pictures_words)
        pictures = msg.get("images") or []
        if not pictures:
            return ""
        told = sent_pictures_words(pictures)
        return (f"к этой реплике приложено картинок: {len(pictures)} — "
                f"уехали вместе с ней" + (f": {told}" if told else ""))
    if not fields:
        return ("тела текста нет и других полей тоже — приложение отправило пустое "
                "сообщение (это уже наша ошибка, а не хитрость протокола)")
    if msg.get("tool_calls") or msg.get("function_call"):
        head = ("тела текста нет, но сообщение не пустое: слова у модели "
                "заменены вызовом инструмента —")
    else:
        head = "тела текста нет, но у сообщения есть поля —"
    return f"{head} {'; '.join(fields)}"


def _message_view(msg: dict) -> dict:
    """Сообщение в том виде, в каком его показывают: роль, говорящий, вес и текст.

    Токены считаются тем же счётом, каким приложение меряет историю: строка
    «запрос состоял из столько-то» должна сходиться с тем, чем резали,
    иначе она врёт. И у каждого сообщения есть `note` — чем оно полно, если
    текста в нём нет (см. message_note).
    """
    content = msg.get("content", "") or ""
    return {
        "role": str(msg.get("role", "") or ""),
        "name": str(msg.get("name", "") or ""),
        "tokens": text.estimate_tokens(content),
        "content": content,
        # Сколько картинок уехало с этим сообщением: сами данные в отчёт
        # не кладутся — они весят мегабайты, — а след «уехала картинка» нужен
        # и в отчёте хода, и в ДАМПе (см. message_note, _format_history)
        "images": len(msg.get("images") or []),
        # А рядом со следом — и сама картинка, одним адресом: модель отвечала
        # по ней, и без неё от «уехала картинка» остаётся одно слово (см.
        # message_pictures, page.promptMessagesHtml)
        "pictures": message_pictures(msg.get("images")),
        "note": message_note(msg),
    }


def message_pictures(images: list) -> list:
    """Картинки, уехавшие этим сообщением, — для записи хода и для страницы.

    В самом сообщении от картинки остался только её адрес-данные (см.
    _format_history), а отчёт о ходе нужен людям: по этой картинке модель
    и отвечала, и она же там показана превью, с возможностью открыть
    оригинал (см. page.promptMessagesHtml). Находится картинка по тому же
    адресу данных (см. _PAYLOAD_FILES) — чужая (из прежнего запуска, где
    адреса в памяти уже нет) остаётся без картинки, но со своими словами.
    """
    out = []
    for one in (images if isinstance(images, (list, tuple)) else []):
        told = _PAYLOAD_FILES.get(one) if isinstance(one, str) else None
        if not told:
            continue
        view = attachment_view(told)
        if view:
            out.append(view)
    return out


def message_pictures_now(message: dict) -> list:
    """Картинки сообщения — словами на сейчас: «кому уехало» узнаётся позже.

    Запись хода складывается в тот миг, когда картинка только собирается уехать
    (см. build_turn_report, commit_shown_pictures), и показа — с именами тех, кому
    она досталась, — там тогда ещё нет. А читают отчёт потом: «собирались
    отправить» вместо «уехало Харите» — это неправда в отчёте о том, что было
    (см. turn_report, attachment_view).
    """
    out = []
    for one in message.get("pictures") or []:
        fresh = attachment_view(one)
        if fresh:
            out.append(fresh)
    return out


def build_turn_report(participant: dict, round_num: int, sent: list, added: list,
                      trim_report: dict, topic: str = "", search_count: int = 0,
                      steps: list = None) -> dict:
    """Полный отчёт о ходе: что вошло в запрос, что происходило и чем кончилось.

    Раньше это было разложено по трём блокам (размышления, набросок, снимок
    запроса) и по двум местам хранения — и сложить общую картину из этого было
    нельзя: числа были тут, поиски там, а их связь — только в голове. Теперь
    отчёт один, и в нём строгая хронология: сначала откуда ход взялся (окно,
    история, обрезка), потом что происходило по порядку (запросы с числами,
    поиски с формулировкой и со всем принесённым), потом что сказала модель.

    Отчёт весит как сцена (десятки килобайт на ход), поэтому в посте остаётся его
    сводка, а текст уходит странице отдельным запросом — когда зритель блок
    раскроет (см. /api/post/<id>/turn). Тот же отчёт целиком уходит в ДАМП
    спектакля (см. dump_turn_header и step_markdown).
    """
    messages = [_message_view(m) for m in sent]
    extra = [_message_view(m) for m in added]
    removed = list(trim_report.get("removed") or [])
    window = int(trim_report.get("window") or 0)
    # Журнал хода передаётся сюда тем же списком, а не копией: отчёт собирается
    # ДО запроса к модели (иначе ДАМП не с чего было бы начинать), а события
    # хода ложатся в этот список по ходу дела и должны быть видно в отчёте
    steps = steps if steps is not None else []
    summary = {
        "messages": len(messages),
        "tokens": sum(m["tokens"] for m in messages),
        "window": window,
        # Чьё окно мерило историю: у облачного участника оно своё, а «не ограничено»
        # — это CLOUD_NUM_CTX = 0, то есть счёт без предела
        "window_kind": ("unbounded" if trim_report.get("unbounded")
                        else "cloud" if trim_report.get("cloud") else "local"),
        "removed_messages": len(removed),
        "removed_tokens": sum(int(r.get("tokens") or 0) for r in removed),
        # Что приложение дописало в запрос уже на своих кругах: строки «прошу
        # поиск» и найденное по ним. Реплик там нет, а токены за это платятся —
        # и знать об этом стоит (в шапке это названо своими словами, а не «ход
        # дописал»: дописывает приложение, а не модель)
        "extra_messages": len(extra),
        "extra_tokens": sum(m["tokens"] for m in extra),
        # Из чего этот хвост состоит — по самим сообщениям: в шапке стояло
        # «по паре на каждый поиск, плюс напоминания», и с числами это не
        # сходилось (3 сообщ. — это как?), потому что пара — привычный случай,
        # а не всегдашний (см. added_kinds)
        "added_kinds": added_kinds(extra),
        "search_rounds": int(search_count or 0),
        "asks": sum(1 for step in steps if step.get("kind") == "ask"),
        # Заминки названы по отдельности, а не одним числом: отказ в поиске
        # сверх лимита — это ещё не беда (модель просто просила больше, чем ей
        # дали), а вот молчание — уже беда. Общая их сумма говорила бы «что-то
        # было», не говоря что (см. refresh_turn_report — к концу хода шаги уже есть)
        "search_refusals": _steps_count(steps, "refused"),
        "silences": _steps_count(steps, "silence"),
    }
    return {
        "summary": summary,
        "who": {
            "name": participant.get("display_name", ""),
            "model": participant.get("model", ""),
            # Значок, пол и портрет — тоже часть «кто говорит»: в ленте они рядом
            # с именем, и в записи о ходе (см. who_line и avatar_line) — там же.
            # Портрет пишется путём, а не картинкой: у поста в ленте на этом месте
            # и стоит снимок (см. postAvatarHtml), и без пути после возврата
            # из ДАМПа лицо пропало бы
            "emoji": participant.get("avatar_emoji")
                     or session.avatar_emojis.get(participant.get("display_name", ""), ""),
            "avatar": participant.get("avatar_url")
                      or session.avatars.get(participant.get("display_name", ""), ""),
            "gender": participant.get("gender", "male"),
            "role": role_of(participant.get("is_moderator", False),
                            participant.get("is_judge", False)),
            # То же слово по-русски: в ДАМПе читают глазами, а «participant»
            # по-русски не читается (см. dump_turn_header)
            "role_name": ROLE_NAMES.get(role_of(participant.get("is_moderator", False),
                                                participant.get("is_judge", False)),
                                        "Участник"),
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
            # Ноль — это тоже ответ («сцены в запросе не было»), и подменять его
            # общим числом сообщений нельзя: иначе в шапке хода появлялось
            # «из сцены 2», хотя сцены там нет вовсе
            "messages_before": _as_number(trim_report.get("messages_before"), len(messages)),
            "messages_after": _as_number(trim_report.get("messages_after"), len(messages)),
        },
        "messages": messages,
        "added": extra,
        "removed": removed,
        "steps": steps,
    }


def added_kinds(added: list) -> dict:
    """Из чего состоит дописанный приложением хвост хода — по самим сообщениям.

    Привычно это «пара на каждый поиск»: просьба вызвать инструмент и ответ на
    неё. Но пара — привычный случай, а не всегдашний: три поиска одним вызовом
    дают одну просьбу и три ответа, а напоминаний в ходу может не быть вовсе.
    Фраза, начинавшаяся с «по паре на каждый поиск», с числами поэтому не
    сходилась. Считаем то, что в хвосте лежит: просьбы модели, найденное, отказы
    по лимиту и просьбы приложения словами (см. _message_view и refresh_turn_report:
    хвост достраивается по ходу и к концу хода считается заново).
    """
    kinds = {"asks": 0, "results": 0, "refusals": 0, "nudges": 0}
    for message in added or []:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role == "assistant":
            kinds["asks"] += 1
        elif content.startswith(ollama_api.SEARCH_RESULT_PREFIX):
            kinds["results"] += 1
        elif role == "tool":
            # Ответ инструмента: либо найденное по поиску, либо отказ по лимиту
            if content.startswith("[лимит поисков исчерпан]"):
                kinds["refusals"] += 1
            else:
                kinds["results"] += 1
        else:
            kinds["nudges"] += 1
    return kinds


def _steps_count(steps, kind: str) -> int:
    """Сколько в ходу было событий такого рода — по журналу, а не по догадке."""
    return sum(1 for step in (steps or []) if step.get("kind") == kind)


def _input_tokens_total(steps) -> int:
    """Сколько ВСЕГО уехало к модели на ввод за ход — по всем его запросам.

    Одним числом «токенов на ввод» ход не описать: поиск — это ещё один круг,
    и вся история уезжает к модели заново, поэтому за ход с пятью поисками
    она оплачивается шесть раз. Число вендора, если он его назвал, важнее
    нашего счёта — берём тем же порядком, что и в строке запроса.
    """
    total = 0
    for step in (steps or []):
        if step.get("kind") != "ask":
            continue
        total += int(step.get("tokens_in") or step.get("tokens_in_est") or 0)
    return total


def _as_number(value, fallback: int) -> int:
    """Число из отчёта, а если его там нет — запасное: ноль тоже число."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def refresh_turn_report(turn: dict, added: list, search_count: int,
                        journal: dict = None, spent: float = None,
                        seconds: float = None) -> None:
    """Дописать в отчёт то, что стало известно только к концу хода.

    Отчёт собирается до запроса к модели (см. build_turn_report), а к концу надо
    дописать то, чего тогда ещё не было: сколько сообщений дописало приложение
    (поиск и найденное) и сколько раз модель просила поиск. Шаги пересчитываются
    по журналу — он общий с отчётом и к этому моменту уже полон.
    """
    extra = [_message_view(m) for m in (added or [])]
    summary = turn.setdefault("summary", {})
    # Шаги берём из журнала, а не из старой ссылки: так же на них смотрит и
    # страница (см. turn_report), и подменённый список не остаётся незамеченным
    steps = (journal or {}).get("steps")
    if steps is None:
        steps = turn.get("steps") or []
    turn["steps"] = steps
    summary["extra_messages"] = len(extra)
    summary["extra_tokens"] = sum(m["tokens"] for m in extra)
    summary["added_kinds"] = added_kinds(extra)
    summary["search_rounds"] = int(search_count or 0)
    # Цена хода — по разнице остатков на ключе, а не по тарифам (см. _note_money).
    # Не знаем — так и говорим молчанием: ноль означал бы «бесплатно»
    summary["spent"] = spent
    # Сколько ход длился — рядом с ценой: время здесь такая же плата, и без
    # него у реплики видно, сколько она стоила, но не видно, чего она стоила
    # зрителю (см. handle_ai_turn)
    summary["seconds"] = None if seconds is None else round(float(seconds), 1)
    summary["asks"] = sum(1 for step in steps if step.get("kind") == "ask")
    # Размышления — тоже шаг хронологии, и о них стоит сказать в свёрнутой
    # строке: иначе о том, что модель что-то говорила сама с собой, и не узнать
    summary["thought_steps"] = sum(1 for step in steps if step.get("kind") == "thought")
    # Заминки к концу хода пересчитываются по журналу: в сводке от них было бы
    # одно слово «что-то было», а отказ в поиске и молчание модели — разные вещи
    summary["search_refusals"] = _steps_count(steps, "refused")
    summary["silences"] = _steps_count(steps, "silence")
    # А это — сколько уехало на ввод за ход ЦЕЛИКОМ, по всем запросам: именно
    # эту сумму видно в кабинете шлюза, и именно она объясняет, почему при
    # пяти поисках вход выглядит втрое больше первой истории (см. _input_tokens_total)
    summary["tokens_in_total"] = _input_tokens_total(steps)
    turn["added"] = extra


def _step_view(step: dict) -> dict:
    """Событие хода в том виде, в каком его показывает страница.

    Время — словами: в JSON метка времени нечитаема, а в хронологии важно именно
    «когда» (см. clock). Технический признак «запись ещё открыта» наружу тоже
    не идёт — страница показывает закрытые записи.
    """
    view = {key: value for key, value in step.items() if key != "open"}
    # Время — словами, и оно важнее часов: у восстановленного хода часов нет,
    # а время его событий осталось записанным (см. parse_dump)
    view["clock"] = step.get("clock") or clock(step.get("t"))
    view["clock_end"] = (step.get("clock_end")
                         or (clock(step.get("t_end")) if step.get("t_end") else ""))
    return view


# Как называть роль по-русски: и в ленте, и в ДАМПе (см. dump_turn_header)
ROLE_NAMES = {
    "participant": "Участник",
    "moderator": "Модератор",
    "judge": "Судья",
}

# Обратно словам: роль в прочитанной записи названа по-русски, а посты и отчёты
# работают с ней по машинному имени (см. parse_dump)
ROLE_BY_NAME = {name: role for role, name in ROLE_NAMES.items()}


def gender_symbol(gender) -> str:
    """Значок пола — тот же, что у поста в ленте (см. create_post)."""
    return "♂" if (gender or "male") == "male" else "♀"


def create_post(display_name: str, model_used: str, content: str, round_num: int, 
                avatar_url: str = None, avatar_emoji: str = None,
                search_count: int = 0, search_queries: list = None,
                role: str = "participant", gender: str = "male",
                thinking: str = "", sketch: str = "", turn: dict = None,
                attachments: list = None, public_judge: bool = None) -> dict:
    """Единая функция создания поста для любого участника (human или AI)"""
    if search_queries is None:
        search_queries = []
    if attachments is None:
        attachments = []
    
    # Роли и значки — из общего словаря: тот же значок нужен и посту, собранному
    # из прочитанного ДАМПа, и разойтись эти два места не должны
    role_names = ROLE_NAMES
    
    return {
        "id": len(session.posts) + 1,
        "display_name": display_name,
        "model_used": model_used,
        "avatar_url": avatar_url,
        "avatar_emoji": avatar_emoji or ROLE_ICONS.get(role, "📣"),
        "content": content,
        "content_html": text.markdown_to_html(content),
        # Размышления модели — не реплика, но и не мусор: их тратят наши токены.
        # В ленте они живут внутри блока «ход»: это часть пути к ответу, а не
        # отдельная сущность (см. _StreamingReply)
        "thinking": thinking or "",
        # А это прежняя версия самой реплики: модель сказала её до поиска и потом
        # ответила заново — в том же блоке, отдельным разделом
        "sketch": sketch or "",
        "round": round_num,
        "timestamp": time.strftime("%H:%M"),
        "search_count": search_count,
        "search_queries": search_queries,
        # Что именно вошло в запрос и что происходило на этом ходу: в посте —
        # только сводка, сам отчёт живёт в памяти сессии (см. DebateSession.turn_report)
        "turn": (turn or {}).get("summary"),
        "role": role,
        "role_icon": ROLE_ICONS.get(role, "🎭"),
        "role_name": role_names.get(role, "Участник"),
        # Слышали ли этот вердикт остальные — свойство самой реплики, а не места:
        # судью могут убрать из состава, и тогда об этом больше узнать неоткуда
        # (см. judge_is_public)
        "public_judge": public_judge,
        "gender": gender,
        "gender_symbol": gender_symbol(gender),
        # Приложенные к реплике картинки (см. save_upload): в ленте это то, что
        # режиссёр приложил сам, а моделям они уезжают по истории (см. post_images)
        "attachments": attachments,
    }


# Приложенные к реплике картинки: их читают для отправки модели (см. ниже),
# поэтому base64 держим в памяти — файл на диске уже никуда не денется
_UPLOAD_IMAGES = {}

# Что уехало модели, а что было приложено: ключ тот же, что у картинок выше,
# и счёт тут же. Лента и ДАМП говорят, какою картинка уехала (см.
# travelled_picture_words), а сама она одна — и меняется она только отправкой.
# Тут же и наш счёт её веса в токенах (см. pictures_report): вендор о цене
# картинки отдельно не говорит, а длина её данных в запросе видна
_UPLOAD_MARKS = {}

# Что это за картинка сама по себе — вид, стороны и вес файла на диске, ключ тот
# же. В ленте про неё спрашивают на каждой отрисовке (см. own_picture_words),
# а читается это один раз на файл
_ATTACHED_MARKS = {}

# То же по самому адресу данных: сообщение в отчёте о ходе хранит картинку
# именно так, и по нему её оттуда и достают — и слова о ней, и сама картинка
# для показа (см. _message_view, message_pictures)
_PAYLOAD_FILES = {}

# То же по самому адресу данных: сообщение в отчёте о ходе хранит картинку
# именно так, и по нему её оттуда и достают (см. sent_pictures_words)
_PAYLOAD_MARKS = {}

# Про кого и какую картинку уже сказано в консоли, что повторно она не уехала:
# без этого одна и та же строка повторялась бы на каждом ходу до конца
# спектакля (см. _format_history)
_IMAGES_NOT_RESENT = set()

def image_kind(data: bytes) -> str:
    """Что это за картинка на самом деле — по её первым байтам.

    Пустая строка — не картинка (или такая, которой мы не знаем). Разбор
    живёт рядом с остальными делами приложенного (см. pictures.picture_kind):
    вид нужен и здесь, и при отправке модели, и раздваивать его незачем
    """
    return pictures.picture_kind(data)


def save_upload(name: str, data_url: str) -> tuple:
    """Приложенная к реплике картинка — на диск. Возвращает (приложение, ошибка).

    Страница присылает файл адресом-данными («data:image/png;base64,…»), а не
    потоком: так одно и то же тело несёт и имя файла, и его содержимое, и не
    надо разбираться с границами формы. Всё, что до проверок, — чужой текст:
    и имя файла, и объявленный тип. Вид картинки определяется по содержимому.
    """
    marker = "base64,"
    if not data_url.startswith("data:") or marker not in data_url:
        return None, "файл пришёл не картинкой: ожидался адрес данных с base64"
    try:
        raw = base64.b64decode(data_url.split(marker, 1)[1], validate=True)
    except Exception:
        return None, "содержимое файла не читается: это не base64"
    kind = image_kind(raw)
    if kind not in settings.UPLOAD_TYPES:
        return None, ("такое приложение не отправить: принимаются картинки "
                      "(png, jpeg, webp, gif)")
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        return None, ("картинка больше "
                      f"{settings.MAX_UPLOAD_BYTES // (1024 * 1024)} МБ — модель её "
                      "всё равно не прочитает")

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Имя своё, а не присланное: имя файла — текст режиссёра, и в нём бывает
    # и «../../», и что угодно ещё. Оригинальное имя хранится рядом для показа
    filename = f"{uuid.uuid4().hex[:12]}{settings.UPLOAD_TYPES[kind]}"
    (settings.UPLOAD_DIR / filename).write_bytes(raw)
    # Вес — по значащей единице (см. pictures.human_bytes): целые килобайты
    # вниз показывали приложенный значок в 761 байт как «0 Кб», то есть как
    # «ничего не приложено»
    print(f"  🖼  Приложено к реплике: {name or filename} "
          f"({pictures.human_bytes(len(raw))}, {kind})")
    return {"name": str(name or filename), "url": f"/uploads/{filename}"}, ""


def stored_picture(url: str, name: str, raw: bytes) -> tuple:
    """Картинка из ДАМПа — в папку приложенных. Возвращает (приложение, ошибка).

    Проверки те же, что у принятой из формы (см. save_upload): вид определяется
    по содержимому, а не по объявленному, и слишком большая не принимается. Файл
    в ДАМПе — такой же чужой текст: его читают и правят руками.

    Имя берётся из адреса, если оно там есть и не занято другой картинкой:
    так адреса внутри ДАМПа остаются теми же, и один и тот же спектакль
    возвращается сколько угодно раз без новых копий. Занятое имя (или адрес
    с путём) — своё, от содержимого: чужую картинку перезаписывать нельзя.
    """
    kind = image_kind(raw)
    if kind not in settings.UPLOAD_TYPES:
        return None, (f"«{name}» в ДАМПе не картинка: принимаются png, jpeg, webp, gif")
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        return None, (f"«{name}» в ДАМПе больше "
                      f"{settings.MAX_UPLOAD_BYTES // (1024 * 1024)} МБ — модель "
                      "её всё равно не прочитает")
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = str(url or "").rstrip("/").split("/")[-1]
    target = settings.UPLOAD_DIR / filename if filename == Path(filename).name else None
    if target is not None and target.is_file() and target.read_bytes() != raw:
        target = None
    if target is None:
        filename = f"{hashlib.sha1(raw).hexdigest()[:12]}{settings.UPLOAD_TYPES[kind]}"
        target = settings.UPLOAD_DIR / filename
    if not target.is_file():
        target.write_bytes(raw)
    return {"name": str(name or filename), "url": f"/uploads/{filename}"}, ""


def upload_files(urls: list) -> list:
    """Приложения страницы в том виде, в каком они пойдут в пост.

    Адрес — чужой текст (он приходит из формы), поэтому каждое имя файла
    проверяется: и что оно не выводит из папки экземпляра, и что файл в ней
    правда есть. Остальное отбрасывается, а не «примеряется».
    """
    result = []
    for item in urls or []:
        url = str((item or {}).get("url") if isinstance(item, dict) else item or "")
        file = url.rstrip("/").split("/")[-1]
        if not file or file != Path(file).name:
            continue
        if not (settings.UPLOAD_DIR / file).is_file():
            continue
        name = str((item or {}).get("name") if isinstance(item, dict) else "") or file
        result.append({"name": name, "url": f"/uploads/{file}"})
    return result


def set_topic_attachments(attachments: list) -> list:
    """Приложить картинки к теме — или убрать их (состав списка и есть правка).

    Список приходит целиком: убрать картинку — это тот же список без неё, и так
    порядок и состав приложенного не разъезжаются между страницей и сервером.

    Отметка `since` — сколько реплик было в ленте, когда картинку приложили:
    по ней видно, кто её уже видел, когда спектакль вернётся из архива или
    из ДАМПа (см. restore_shown_pictures). У приложенной до спектакля отметка 0:
    её видел каждый, кто говорил после начала. У приложенной на ходу — те, кто
    говорил позже; а успевшие сказать раньше получат её на своём следующем ходу,
    и это верно: они её ещё не видели.
    """
    taken = upload_files(attachments or [])
    was = {str((one or {}).get("url") or ""): _as_int((one or {}).get("since"))
           for one in session.topic_attachments or [] if isinstance(one, dict)}
    now = session.spoken_count()
    for one in taken:
        # У уже приложенного отметка своя и не меняется: оно приложено раньше,
        # и «показано с этой реплики» — про тот момент, а не про сейчас
        one["since"] = was.get(str(one.get("url") or ""), now)
    session.topic_attachments = taken
    # И в ДАМП: запись — единственное место, где спектакль лежит целиком,
    # а приложить картинку к теме можно и на ходу (см. dump_topic_pictures)
    dump_topic_pictures()
    return taken


def set_topic(text: str) -> str:
    """Сменить тему — и, если она правда сменилась, начать разговор с ней заново.

    Тема — это про что спектакль, а не подпись к нему: с новой темой модели ещё
    не слышали приложенного к ней, поэтому картинка к теме снова свежая у каждой
    и каждая получит её по разу (см. forget_topic_pictures). Та же тема ничего
    не меняет: нажатие «Применить» второй раз не повторяет показ.

    Место одно на оба пути: и «Применить тему» в пульте, и старт спектакля
    с темой в теле запроса (см. web.moderator_topic, web.start). Иначе один
    из них забыл бы сказать, что тема сменилась.
    """
    text = str(text or "").strip()
    if text and text != (session.topic or "").strip():
        session.forget_topic_pictures()
        session.topic = text
    return session.topic or ""


def _picture_key(url: str):
    """Ключ приложенного в памяти: имя файла и его вес (см. _UPLOAD_IMAGES).

    По имени в одиночку нельзя: файл приложенной картинки не переписывается
    никогда, но папку приложенных чистят, и чужой файл с тем же именем не должен
    получить себе чужую память.
    """
    file = str(url or "").rstrip("/").split("/")[-1]
    if not file or file != Path(file).name:
        return None
    try:
        return (file, (settings.UPLOAD_DIR / file).stat().st_size)
    except OSError:
        return None


def post_image_pairs(attachments: list) -> list:
    """Картинки поста для модели — парами «адрес файла + его содержимое».

    Адрес нужен рядом с картинкой затем, чтобы помнить, кому она уже показана:
    повторно за неё платить нечем (см. _format_history), а у самих картинок
    в запросе имени нет — только base64.

    Тут же и запоминается, какою картинка уехала: лента и ДАМП говорят об этом
    рядом с самой картинкой (см. _UPLOAD_MARKS, travelled_picture_words).
    Картинка читается с диска один раз на ключ, а не на каждый ход.
    """
    images = []
    for item in attachments or []:
        url = str((item or {}).get("url") or "")
        key = _picture_key(url)
        if key is None:
            continue
        if key not in _UPLOAD_IMAGES:
            raw = (settings.UPLOAD_DIR / key[0]).read_bytes()
            kind = image_kind(raw)
            if not kind:
                continue
            payload = (f"data:{kind};base64,"
                       + base64.b64encode(raw).decode("ascii"))
            # Приведение к обычному размеру — здесь, на отправке, а не при
            # приёме: файл на диске и в ДАМПе остаётся таким, каким его принёс
            # режиссёр, а модели уезжает то, что она точно примет (см.
            # pictures.prepare). Помнится это по имени файла и весу (см. выше),
            # поэтому говорится тут ровно один раз — что именно сделано
            # с картинкой и сколько весило прежнее
            payload, note = pictures.prepare(payload)
            name = str((item or {}).get("name") or key[0])
            if note:
                print(f"  🖼  «{name}»: {note}")
            _UPLOAD_IMAGES[key] = payload
            # И рядом с тем, что уехало, — то, что было приложено: другое
            # рядом с приложенным и читают в ленте (см. pictures.travel_words)
            sent = pictures.ready_marks(payload)
            _UPLOAD_MARKS[key] = {"sent": sent,
                                  "attached": {**pictures.picture_marks(raw),
                                               "bytes": len(raw)},
                                  "tokens": text.estimate_tokens(payload)}
            _PAYLOAD_MARKS[payload] = sent
            # И по адресу данных: по нему картинка находится в записи хода,
            # где от самой картинки остался только он (см. message_pictures)
            _PAYLOAD_FILES[payload] = {"url": url, "name": name}
        images.append((url, _UPLOAD_IMAGES[key]))
    return images


def post_images(attachments: list) -> list:
    """Картинки поста для модели — одними адресами-данными (base64 внутри).

    Читаются в тот момент, когда история уезжает модели, а не при загрузке:
    пост с картинкой лежит в памяти весь спектакль, а запросов за это время
    десятки. Прочитанное помним по имени файла и размеру: файл приложенной
    картинки не переписывается никогда (см. save_upload).
    """
    return [payload for _url, payload in post_image_pairs(attachments)]


def images_already_shown_note(attachments: list) -> str:
    """Строка о картинке, которая этой модели уже показана.

    Место в контексте она занимает то же, что и тексты (тысячи токенов),
    поэтому повторно не уезжает — но и пропасть бесследно не должна: иначе
    модель решила бы, что к реплике ничего не прикладывали, и в лучшем случае
    переспросила бы, а в худшем — выдумала бы её содержание.
    """
    names = []
    for one in attachments or []:
        url = str((one or {}).get("url") or "")
        names.append(str((one or {}).get("name") or url.rsplit("/", 1)[-1]))
    return ("📎 К реплике приложена картинка "
            + ", ".join(f"«{name}»" for name in names)
            + " — она уже показана тебе в этом обсуждении, повторно не прикладывается")

def judge_is_public(participant: dict) -> bool:
    """Слышат ли этого судью остальные — или его слово только для режиссёра.

    Это опция роли (см. ROLE_OPTIONS). Запись о ней нужна потому, что ушедшего
    из состава судьи в текущем составе уже нет: без неё его прежние вердикты
    стали бы вдруг неслышными — а прошлое на ходу не переписывается
    (см. DebateSession._judge_was_heard)
    """
    return role_options(participant).get("publicity") == "public"


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
        # Приложения к реплике живого участника: страница кладёт их сюда вместе
        # с самой репликой (см. web.moderator_message), а ход человека забирает
        # их ровно один раз (см. play_show) — иначе картинка прилипла бы к трём
        # следующим репликам подряд
        self.pending_attachments = []
        # Кому какая картинка уже показана: модель → адреса приложенного к её
        # репликам. Повторно картинка не уезжает: она занимает в контексте
        # тысячи токенов, а модель уже видела её своими глазами (см.
        # _format_history)
        self.images_shown = {}
        # Что этой модели уедет на этом ходу: в показанное попадает только
        # после удачного хода — запросов в нём несколько (поиск и повтор после
        # него), и картинка должна быть во всех (см. commit_shown_pictures)
        self.images_pending = {}
        # Приложенное к теме: у темы нет реплики, к которой картинку можно было бы
        # приложить, поэтому она лежит отдельно и едет первым сообщением хода
        # (см. topic_picture_message). Правила те же, что у приложенного
        # к репликам: каждой модели — один раз, а `since` в записи картинки —
        # сколько реплик было в ленте, когда её приложили (см.
        # restore_shown_pictures)
        self.topic_attachments = []
        self.moderator_finished = False
        self.runtime_participants = []
        self.conversation_history = []
        # Сколько стоил спектакль: сумма цен ходов, а каждая из них — разница
        # остатков на ключе (см. cloud.balance_spent). Тарифов мы не знаем —
        # а это факт со счёта
        self.spent = 0.0
        # Сказали ли уже вслух, что остатка нет и цены не будет
        self.money_told = False
        # Часы хода: сколько уже идёт текущий ход и когда он оборвётся по сроку.
        # Заводит их тот, кто ведёт ход (см. ollama_api.ask_model), а читает
        # сайдбар — «думает 1:23, осталось 2:10» вместо немого ожидания,
        # по которому не понятно, ждать минуту или десять
        self.turn_started = None
        self.turn_deadline = None
        self.turn_limit = 0
        self.turn_extra = 0
        # Отчёты ходов по номеру реплики: что именно вошло в запрос, что
        # происходило по порядку и чем кончилось. В памяти — за весь
        # спектакль: читают это ради разбора свежего случая, а новый спектакль
        # начинает список заново (см. start_show), как и ДАМП переписывается
        self.turn_log = {}
        # Вернулся ли этот спектакль из ДАМПа прошлого запуска: по этому
        # признаку в ленте видно, что занавес был не сейчас (см. status_payload)
        self.restored = False
        # Чем вернувшийся спектакль был сохранён: «занавес опущен» или «на ходу
        # человека» — словами говорит страница (см. load_save)
        self.restored_phase = ""
        # Прежний спектакль вернулся с разрешением доиграть: по этому признаку
        # старт не начинает с чистого листа, а продолжает прежний — со своими
        # репликами в ленте, своей историей для моделей и с той же ценой
        # (см. start_show). Ставит его тот, кто читал ДАМП (см. load_play_from_dump)
        self.resume_ready = False
        # А это — про этот запуск: продолжает ли спектакль только что сыгранный
        self.resumed = False
        # Ход, оборванный смертью процесса, ждёт переспроса: прежние реплики
        # вернулись целыми, а эта — прерванной, и без неё цепочка разговора
        # рвётся (см. plan_replay и replay_broken_turn). Решается это на старте
        # продолжения, а играется там же, где и сам спектакль — первым делом
        self.replay_plan = None
        # Заказ ленты: правка сказанного, возврат версии или переигровка. На ходу
        # его берёт сам спектакль, между ходами (см. order_feed и take_order):
        # лента и ДАМП в это время — собственность идущего хода, и второй писатель
        # встал бы посреди незаконченной записи. Под занавесом заказ делается
        # сразу: писать в ленту больше некому, а спектакль останавливать ради
        # правки не приходится — статус не запрет (см. replay_show)
        self.feed_order = None
        # Что стало с последним заказом ленты — словами для страницы
        # (см. status_payload): состоянием, а не ответом на нажатие, потому что
        # заказ мог быть сделан и в другой вкладке, и на чужом ходу
        self.feed_note = None
        # Переигровка, посчитанная заказом и ждущая того, кто её сыграет:
        # план берёт ведущий спектакля (см. take_replay_next)
        self.replay_next = None
        # Прежние слова отложенных реплик: переигранная реплика встаёт тем же
        # номером, и её прежние слова должны остаться летописью, а не пропасть
        # (см. hold_from, add_post). Наполняется откладыванием ленты перед
        # переигровкой, а пустеет, как только переигровка кончилась (см.
        # return_held)
        self.held_versions = {}
        # Сколько раз играли сказанное заново и какие реплики этим задеты: итог
        # под занавесом говорит и об этом (см. show_report). Из ДАМПа не вернуть —
        # там летопись версий, а не число заходов, — поэтому счёт только про этот
        # запуск, а «реплик с прежними словами» считается по самой ленте
        self.replays = 0
        self.replayed_posts = set()
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

    def reset_to_defaults(self):
        """«Полный сброс»: весь пульт заново из настроек проекта.

        Состав, общие правила общения, руководства модератора и правила судьи
        возвращаются к значениям из settings.py — ровно так, как это выглядит
        при первом запуске с пустой папкой экземпляра. Сохранённый пульт при этом
        забывается (см. forget_theatre_settings): иначе следующий запуск вернул
        бы то, от чего режиссёр только что отказался.

        Тема не трогается — и здесь, и позже: она про сюжет, а не про труппу,
        и в settings.py её нет, поэтому каждый сброс означал бы «придумай тему
        заново». Сцена следует за составом: с ней уходили бы и личные инструкции
        мест, то есть ровно то, что сброс и должен забыть.
        """
        self.static_instructions = []
        self.judge_rules = list(settings.DEFAULT_JUDGE_RULES)
        self.moderator_guidelines = []
        self.scene = None
        self.load_new_cast()
        # Прежний спектакль тоже забывается — и в памяти, и на диске: после
        # «Полного сброса» сцена (и лента) начинается с чистого листа, а не
        # с прежних реплик (см. forget_play)
        forget_play()

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
        # Спектакль мог вернуться из ДАМПа с разрешением доиграть: тогда
        # прежние реплики остаются в ленте, историей для моделей становится
        # то же самое, а акт и цена продолжаются — это всё один спектакль
        resume = bool(self.resume_ready)
        self.resume_ready = False
        self.resumed = resume
        self.running = True
        self.session_id = uuid.uuid4().hex[:8]
        self.topic = topic
        self.current_participant = None
        self.current_action = None
        self.search_query = None
        self.finished = False
        self.waiting_for_human = False
        self.moderator_message = None
        self.pending_attachments = []
        # Занавес поднимается — и про показанное спрашивать не у кого: новую
        # сцену модели начинают с пустой головой. А у продолжения память есть,
        # и возвращает её сама история (см. restore_shown_pictures)
        self.forget_shown_pictures()
        self.moderator_finished = False
        # Спектакль играется сейчас: слова «прежний, возвращённый из ДАМПа»
        # после этого уже неверны, а доигранный когда-то спектакль не должен
        # выдавать себя за прежний после перезапуска (см. status_payload)
        self.restored = False
        self.restored_phase = ""
        if not resume:
            self.posts = []
            self.current_round = 0
            self.conversation_history = []
            # Счёт за спектакль начинается заново: это про этот вечер, а не про
            # все, что были (см. _note_money)
            self.spent = 0.0
            self.money_told = False
            # Новый спектакль — новые реплики: отчёты прежних ходов к ним не подходят
            self.turn_log = {}
            # И приложенное к теме в новом спектакле модель не помнит: показывать
            # придётся заново, а отсчёт реплик начинается с пустой ленты. Отметку
            # сбрасываем здесь, а не в forget_shown_pictures: из архива приходят
            # картинки с прежней отметкой, и она нужна доигрышу (см. load_save,
            # restore_shown_pictures)
            for one in self.topic_attachments or []:
                if isinstance(one, dict):
                    one["since"] = 0
            # И переспрашивать нечего: оборванный ход был у прежнего спектакля,
            # а этот начинается с чистой ленты (см. plan_replay)
            self.replay_plan = None
            # И переигровок у него не было ни одной: их счёт — про этот запуск
            # (см. show_report, replay_show)
            self.replays = 0
            self.replayed_posts = set()
        else:
            # Прежние реплики — это и есть история для моделей: продолжение
            # не начинается с чистого листа, иначе модель отвечала бы не на то,
            # что говорилось на сцене, а в пустоту
            self.conversation_history = self.history_from_posts()
            # Акт продолжения — по самой ленте, а не по памяти о последнем акте:
            # память бывает и стёрта (спектакль доигрывали правкой, из архива,
            # да и просто чужим кодом), а лента всегда на месте. Без этого
            # продолжение своей же сцены начиналось бы с первого акта
            # (см. page.canContinuePlay)
            self.current_round = int(self.posts[-1].get("round") or 0) if self.posts else 0
            # Цена продолжения — вся цена спектакля: он всё ещё тот же
            self.money_told = bool(self.spent)
            # А ход, оборванный смертью процесса, переспрашивается: реплики
            # в нём нет вовсе (см. _parse_record), и считать его сказанным
            # нельзя — оставленная дыра слышна всем, кто говорит после
            self.replay_plan = self.plan_replay()
            # Показанное прежнего спектакля помнится его же историей: картинку,
            # после которой модель уже говорила, она видела (см. restore_shown_pictures)
            self.restore_shown_pictures()
        self.sync_cast_media()

    def history_from_posts(self) -> list:
        """История для моделей — из ленты: это одно и то же, только словами.

        Отдельным заведением, потому что спрашивают её в двух случаях: когда
        спектакль продолжается (см. start_show) и когда сказанное переигрывают
        или правят (см. replay_plan_for, edit_post). Строки у них одни и те же,
        а разойтись эти два места не должны.

        Приложенное едет вместе с репликой: в ДАМПе лежат адреса картинок
        (см. attachment_lines), а без них прежнее приложенное пропало бы
        из истории целиком — и модель, которой картинка не досталась, не увидела
        бы её вовсе.
        """
        return [
            {"display_name": post.get("display_name", ""),
             "content": post.get("content", ""),
             "is_moderator": post.get("role") == "moderator",
             "is_judge": post.get("role") == "judge",
             "public_judge": post.get("public_judge"),
             "round": int(post.get("round") or 0),
             "attachments": list(post.get("attachments") or [])}
            for post in self.posts
            if str(post.get("content") or "").strip() or post.get("attachments")
        ]

    def plan_replay(self) -> dict:
        """Кого переспросить из-за хода, оборванного смертью процесса.

        Прежний спектакль возвращается целым, кроме одного хода: у того, что
        оборвала смерть процесса, реплики нет вовсе — она живёт только в памяти
        и в файл не попала (см. _parse_record). Дыра эта не пустая: остальные
        модели слышат разговор, в котором очередь дошла до кого-то — и он
        промолчал. Поэтому такой ход переспрашивается первым же делом
        (см. replay_broken_turn), и стоит это одной реплики: дешевле один раз
        заплатить за неё, чем продолжать разговор с оборванной цепочкой.

        Возвращает план или None. None — переспрашивать некого: оборванного
        хода нет, говорящего убрали из состава или его место стало живым. Тогда
        реплика остаётся в ленте помеченной как оборванная — это честнее, чем
        выдумывать за него слова.
        """
        if not self.posts:
            return None
        last = self.posts[-1]
        if not last.get("interrupted"):
            return None
        name = last.get("display_name") or ""
        who = next((p for p in self.runtime_participants
                    if p.get("display_name") == name), None)
        if who is None:
            print(f"  ⚠️  Ход {last.get('id')} ({name}) оборван, но {name} больше "
                  f"не в составе — переспрашивать некого, реплика остаётся "
                  f"помеченной оборванной")
            return None
        if not who.get("model") or who.get("model") == "human":
            print(f"  ⚠️  Ход {last.get('id')} ({name}) оборван, а место теперь "
                  f"живое — переспрашивать модель нечего")
            return None
        # Реплика уходит из ленты: переспрошенная встанет ровно на её место —
        # тем же номером и в том же акте. Иначе рядом с настоящим ответом
        # так и висела бы пустая рамка оборванного хода
        self.posts.pop()
        return {"post": last,
                "turn": self.turn_log.pop(int(last.get("id") or 0), None),
                "participant": dict(who),
                "round": int(last.get("round") or 0) or self.current_round}

    def replay_broken_turn(self, on_draft=None) -> dict:
        """Переспросить ход, оборванный смертью процесса (см. plan_replay).

        Играется он с того самого места, где оборвался: та же реплика, тот же
        акт, тот же номер — но история и состав нынешние. Запись о прежнем ходе
        в ДАМПе незакончена, а новая ляжет тем же номером: прежнюю убираем
        (см. drop_dump_turn), иначе в файле оказались бы две записи об одном
        ходе и читать его стало бы нельзя.

        Возвращает новую реплику или None — если переспрашивать нечего.
        """
        plan = self.replay_plan
        if not plan:
            return None
        self.replay_plan = None
        participant = plan["participant"]
        name = participant.get("display_name", "")
        print(f"  🔁 Ход {plan['post'].get('id')} ({name}, "
              f"{participant.get('model', '')}) оборвала смерть процесса — "
              f"переспрашиваю его первым: без него цепочка разговора рвётся")
        if not drop_dump_turn(plan["post"].get("id")):
            # Прежнюю запись из файла не убрать — переспрашивать нельзя: две
            # записи об одном ходе сделали бы ДАМП нечитаемым. Тогда реплика
            # возвращается в ленту помеченной оборванной — как если бы
            # переспрашивать было некого (см. plan_replay)
            print(f"  ⚠️  Прежняя запись об этом ходе не убирается из "
                  f"{settings.DUMP_FILE.name} — оставляю его оборванным")
            self.posts.append(plan["post"])
            if plan.get("turn"):
                self.turn_log[int(plan["post"].get("id") or 0)] = plan["turn"]
            return None
        self.current_participant = name
        self.current_action = "thinking"
        self.handle_ai_turn(participant, plan["round"], on_draft=on_draft)
        self.current_action = None
        return self.posts[-1] if self.posts else None

    # ------------------------------------------------------------
    # Переигровка и правка сказанного
    # ------------------------------------------------------------
    # Спектакль можно сыграть заново — не весь, а от любого места: реплику,
    # акт целиком или весь спектакль. Правила, тема и состав берутся нынешние:
    # ради этого и переигрывают — чтобы ответили по тому, что настроено сейчас.

    def index_of(self, post_id) -> int:
        """Место реплики в ленте по её номеру. None — такой реплики в ленте нет."""
        try:
            post_id = int(post_id)
        except (TypeError, ValueError):
            return None
        for index, post in enumerate(self.posts):
            if int(post.get("id") or 0) == post_id:
                return index
        return None

    def hold_from(self, index: int) -> dict:
        """Отложить ленту с этого места: переигранное встанет ровно на её место.

        Лента обрывается на том, с чего играют заново: модель не должна видеть
        то, что было сказано после переигранной реплики, — иначе её ответ был бы
        о будущем. Отложенное не пропадает: отыгранное занимает свои номера,
        а всё, чего переигровка не коснулась, возвращается в ленту тем же порядком
        (см. return_held).

        Возвращает отложенное: реплики вместе с отчётами их ходов.
        """
        index = max(0, min(int(index), len(self.posts)))
        kept = self.posts[:index]
        held = {"posts": list(self.posts[index:]), "turns": {}}
        for post in held["posts"]:
            key = int(post.get("id") or 0)
            if key in self.turn_log:
                held["turns"][key] = self.turn_log.pop(key)
        self.posts = kept
        # Прежние слова отложенных реплик держатся отдельно: переигранная встанет
        # тем же номером (см. add_post), и по ним видно, что́ она говорила прежде
        self.held_versions = {int(one.get("id") or 0): one for one in held["posts"]}
        self.conversation_history = self.history_from_posts()
        # Акт — по оставшемуся: переигранное продолжает спектакль с этого места,
        # а не начинает новый
        self.current_round = int(kept[-1].get("round") or 0) if kept else 0
        self.restore_shown_pictures()
        return held

    def return_held(self, held: dict) -> int:
        """Вернуть в ленту отложенное, чего переигранное не коснулось.

        Переигранное занимает свои номера заново (номер — это место в ленте,
        см. add_post), поэтому не тронутым считается всё, что нумеровано дальше
        отыгранного — и оно возвращается на своё место вместе с отчётами ходов:
        реплика без отчёта читалась бы как ход без следов.

        Возвращает, сколько реплик вернулось.
        """
        held = held or {}
        last = int(self.posts[-1].get("id") or 0) if self.posts else 0
        back = [one for one in held.get("posts") or []
                if int(one.get("id") or 0) > last]
        self.posts.extend(back)
        for one in back:
            key = int(one.get("id") or 0)
            if key in (held.get("turns") or {}):
                self.turn_log[key] = held["turns"][key]
        if back:
            self.conversation_history = self.history_from_posts()
            self.restore_shown_pictures()
        # Отложенное больше не ждёт: то, чего переигровка не коснулась, вернулось
        # на своё место, и прежними словами его летопись не ведут (см. add_post)
        self.held_versions = {}
        return len(back)

    def replay_scope(self, post_id, scope=None) -> tuple:
        """Что переигрывается: (место в ленте, сколько реплик, беда).

        Реплика — одна (та же, тем же участником, в том же акте), акт — весь акт
        целиком, с его первой реплики, спектакль — всё, с самого начала. Число
        реплик берётся из ленты, а не из состава: переигрывается ровно то, что было
        сказано, — и если место из состава убрали, акт от этого короче не станет
        (участник просто пропустит свой ход, см. run_debate_thread).

        Переигрывают всегда нынешние слова — и только ответы модели: свои слова
        правят (см. edit_post), а из прежних ответов модели выбирают лучший
        (см. revert_version). Переиграть «с этих слов» было бы третьим делом
        об одном и том же, и в ленте от него оставался бы бардак: слова из
        летописи вставали бы в разговор, а соседние ответы отвечали бы уже
        на другое.

        Ничего не меняет — по этому заказ ленты принимается или отклоняется сразу
        (см. check_order), а лента обрывается уже перед самой переигровкой
        (см. replay_plan_for).

        Возвращает (место, реплик, беда): беда не пустая — переигрывать нечего.
        """
        scope = str(scope or "post").strip().lower()
        if scope == "show":
            if not self.posts:
                return None, 0, "переигрывать нечего: лента пуста"
            index, count = 0, len(self.posts)
        else:
            if scope not in ("post", "round", "act"):
                return None, 0, f"неизвестно, что переигрывать: «{scope}»"
            index = self.index_of(post_id)
            if index is None:
                return None, 0, f"реплики №{post_id} в ленте нет"
            if scope in ("round", "act"):
                act = int(self.posts[index].get("round") or 0)
                index = next((place for place, one in enumerate(self.posts)
                              if int(one.get("round") or 0) == act), index)
                count = sum(1 for one in self.posts
                            if int(one.get("round") or 0) == act)
            else:
                count = 1
        # Реплику человека саму заново не играют: переспрашивать у неё нечего —
        # свои слова правят (см. edit_post), а отвечать заново просят у ответа
        # модели кнопкой «🔁 эту реплику». Акт и спектакль — другое дело: там
        # реплика человека просто начало, и с неё переигрывают всё, что после
        # (см. page.postActionsHtml)
        if scope == "post" and self.posts[index].get("model_used") == "human":
            return None, 0, ("переигрывать реплику человека нечего: её слова "
                             "правят, а заново отвечают у ответа модели")
        name = self.posts[index].get("display_name") or ""
        # С того, с кого играть, нужно начать — и он обязан быть на сцене. Место,
        # ушедшее из состава (или переименованное), начало бы переигровку
        # с чужого хода, а прежняя реплика пропала бы: отложенное возвращается
        # только тем, что нумеровано дальше отыгранного (см. return_held)
        if not any(one.get("display_name") == name
                   for one in self.runtime_participants):
            return None, 0, (f"места «{name}» больше нет в составе: "
                             f"переигрывать некому")
        return index, count, ""

    def replay_plan_for(self, post_id, scope=None, hold: bool = True) -> tuple:
        """Чем играть сказанное заново: с какого места лента обрывается и сколько.

        План — то, чем играют заново (см. run_debate_thread); вместе с планом
        возвращается отложенное, потому что лента уже оборвана и вернуть её
        без переигровки нечем.

        Номер реплики нужен не всему: реплику и акт с неё и начинают, а спектакль
        целиком — с первого слова, и номера у него нет вовсе. Страница так и шлёт:
        один лишь scope (см. page.replayWholeShow), поэтому требовать номер значило
        бы отказывать в переигровке всему спектаклю.

        hold=False — план без обрыва ленты: его берут заказом, и лента в этот
        миг ещё занята идущим ходом или ждёт возврата версии. Место, с которого
        её оборвать, лежит в самом плане ("index"), и обрывает её тот, кто
        и вправду начинает переигровку (см. replay_show).

        Возвращает (план, отложенное, беда): беда не пустая — переигрывать нечего.
        """
        index, count, problem = self.replay_scope(post_id, scope)
        if problem:
            return None, None, problem
        first = self.posts[index]
        plan = {"posts": count,
                "round": int(first.get("round") or 0) or 1,
                "name": first.get("display_name") or "",
                # Место обрыва ленты: по нему это делает replay_show, когда
                # план и вправду берут в игру
                "index": index}
        if not hold:
            return plan, None, ""
        held = self.hold_from(index)
        plan["held"] = held
        return plan, held, ""

    def replay_show(self, plan: dict) -> None:
        """Поднять занавес ради переигровки: тот же спектакль, но сказанное заново.

        Спектакль при этом идёт, как обычно, и ДАМП не переписывается: прежние
        записи остаются в файле, а переигранное ложится ниже, с отметкой
        (см. dump_replayed). Память о показанном у моделей тоже остаётся прежней —
        её возвращает сама лента (см. restore_shown_pictures).

        Здесь же лента и обрывается — у плана, пришедшего заказом (см.
        plan_feed_replay, replay_plan_for): переигранное встаёт тем же номером,
        а прежнее ждёт в отложенном до конца переигровки (см. return_held).
        """
        # Сколько раз играли заново и какие реплики — в счёт итога под занавесом
        # (см. show_report). Номера переигранных — те же, что и были: их берём
        # по месту обрыва ленты, а у плана, пришедшего заказом, лента уже
        # обрезана — там то же самое лежит в отложенном (см. hold_from)
        self.replays += 1
        tail = (plan.get("held") or {}).get("posts")
        if tail is None and plan.get("index") is not None:
            tail = self.posts[int(plan["index"]):]
        for one in list(tail or [])[:int(plan.get("posts") or 0)]:
            self.replayed_posts.add(int(one.get("id") or 0))
        # Обрыв ленты — после счёта: переигранные встанут теми же номерами,
        # а сами прежние реплики ждут в отложенном до конца переигровки
        # (см. return_held)
        if plan.get("held") is None and plan.get("index") is not None:
            plan["held"] = self.hold_from(int(plan["index"]))
        self.running = True
        self.finished = False
        self.moderator_finished = False
        self.waiting_for_human = False
        self.current_action = None
        self.current_participant = None
        self.search_query = None
        self.moderator_message = None
        self.pending_attachments = []
        # ДАМП дописывается, а не начинается заново (см. start_dump)
        self.resumed = True
        self.resume_ready = False
        self.restored = False
        self.restored_phase = ""
        # Оборванный ход тут не при чём: ленту переигрывают, а не возвращают
        self.replay_plan = None

    # ------------------------------------------------------------
    # Заказы ленты: правка, возврат версии и переигровка — без занавеса
    # ------------------------------------------------------------
    # Статус спектакля — это признак того, чем он сейчас занят, а не запрет:
    # сказанное правят, возвращают и переигрывают и под занавесом, и на ходу.
    # На ходу лента и ДАМП принадлежат идущему ходу, поэтому заказ из веб-потока
    # не делается сам собой: его берёт спектакль, между ходами (см. take_order).

    def order_feed(self, kind: str, **payload) -> str:
        """Заказать правку ленты: правку реплики, возврат версии или переигровку.

        Возвращает беду словами: пустая строка — заказ принят. Под занавесом
        (и до спектакля) он тут же и сделан: в ленту больше никто не пишет.
        А на ходу заказ берёт сам спектакль, между ходами, — останавливать его
        ради правки не приходится, и занавес для этого не нужен (см.
        take_order, run_debate_thread).

        Ждать заказу недолго: идущий ход не тянет ради него ни новых поисков,
        ни новых попыток — сказанное уже сказано (см. ollama_api.ask_model).
        """
        problem = self.check_order(kind, **payload)
        if problem:
            return self.note_feed_refusal(problem)
        # Заказ один: второй поверх первого — это не очередь, а гонка
        if self.feed_order is not None:
            problem = self.take_order()
            if problem:
                return problem
        self.feed_order = {"kind": str(kind or ""), **payload}
        if not self.running:
            return self.take_order()
        # Спектакль идёт: заказ принят, но сделает его он сам. Сказать об этом
        # надо сразу — иначе нажатие выглядит как «ничего не произошло»
        accepted = {"edit": "✍️ Правка реплики принята",
                    "version": "↩️ Возврат прежних слов принят",
                    "replay": "🔁 Переигровка принята"}
        self.feed_note = {"text": ((accepted.get(self.feed_order["kind"])
                                    or "Заказ ленте принят")
                                   + ": сделаю сразу после этого хода"),
                          "stamp": time.time(), "rebuild": False, "post": 0}
        print(f"  ⏳ {self.feed_note['text']}")
        return ""

    def note_feed_refusal(self, problem: str) -> str:
        """Отказ по заказу ленты — и словами, и состоянием.

        Состоянием — потому что заказ мог быть сделан и в другой вкладке,
        и на чужом ходу: там ответа на нажатие не видно, и молчание выглядело бы
        как «нажал, а ничего не произошло» (см. feed_note, web.feed_payload).
        """
        self.feed_note = {"text": f"⚠️ {problem}", "stamp": time.time(),
                          "rebuild": False, "post": 0}
        print(f"  ⚠️  Заказ ленты не сделан: {problem}")
        return problem

    def check_order(self, kind: str, **payload) -> str:
        """Можно ли сделать заказ, ничего не меняя: беда — словами, пусто — можно.

        Нужна заказам, пришедшим на ходу: делает их спектакль позже, между
        ходами, и отказ должен быть слышен сразу, а не после чужого хода
        (см. order_feed). Заодно это единственное место, где сказано, что
        править можно только слова человека, а возвращать прежние слова и 
        переигрывать — любой реплике: прямые вызовы (edit_post,
        revert_version) ходят через ту же дверь.
        """
        kind = str(kind or "")
        post_id = payload.get("post_id")
        if kind == "edit":
            _post, problem = self.post_to_edit(post_id, payload.get("content"),
                                              payload.get("attachments"))
            return problem
        if kind == "version":
            # Вернуть прежние слова можно любой реплике: у реплики человека так
            # возвращают её прежние слова, а у ответа модели — выбирают лучший
            # из её ответов (см. revert_version, switch_words)
            return self.version_pick(post_id, payload.get("version"))[2]
        if kind == "replay":
            return self.replay_scope(post_id, payload.get("scope"))[2]
        return f"неизвестно, что делать с лентой: «{kind}»"

    def post_to_edit(self, post_id, content, attachments=None) -> tuple:
        """Реплика человека, которую правят: (реплика, беда) — без единой правки.

        Ни слов, ни картинок — править нечего: реплика без того и другого
        перестала бы быть репликой (см. edit_post). Приложенное берётся то, что
        просят поставить, а без просьбы — то, что у реплики уже есть.
        """
        index = self.index_of(post_id)
        if index is None:
            return None, f"реплики №{post_id} в ленте нет"
        post = self.posts[index]
        if post.get("model_used") != "human":
            return None, "править можно реплику человека: у модели её переигрывают"
        said = str(content or "").strip()
        if attachments is None:
            left = [one for one in post.get("attachments") or []
                    if str((one or {}).get("url") or "").strip()]
        else:
            left = [one for one in attachments
                    if str((one or {}).get("url") or "").strip()]
        if not said and not left:
            return None, "пустая реплика: сказать нечего"
        return post, ""

    def take_order(self) -> str:
        """Сделать заказ ленты, если он есть: беда — словами, пусто — сделано.

        Зовут это двое: веб-поток, когда спектакль не идёт (см. order_feed),
        и сам спектакль, между ходами (см. run_debate_thread). Переигровка
        здесь не играется: её план ложится в replay_next, и играет заново тот,
        кто ведёт спектакль, — иначе в ленту писали бы двое.
        """
        order, self.feed_order = self.feed_order, None
        if not order:
            return ""
        kind = str(order.get("kind") or "")
        post_id = order.get("post_id")
        focus, problem = 0, ""
        if kind == "edit":
            post, problem = self.edit_post(post_id, order.get("content"),
                                           order.get("attachments"))
            note = "✍️ Реплика человека изменена"
            focus = int((post or {}).get("id") or 0)
        elif kind == "version":
            post, problem = self.revert_version(post_id, order.get("version"))
            note = "↩️ Реплике вернули прежние слова"
            focus = int((post or {}).get("id") or 0)
        elif kind == "replay":
            plan, focus, problem = self.plan_feed_replay(post_id, order.get("scope"))
            note = "🔁 Сказанное играется заново"
            if not problem:
                self.replay_next = plan
        else:
            return f"неизвестно, что делать с лентой: «{kind}»"
        if problem:
            return self.note_feed_refusal(problem)
        return self.note_feed_done(note, focus)

    def note_feed_done(self, note: str, post_id=0) -> str:
        """Сделанный заказ ленты — словами и состоянием.

        Странице это нужно дважды: и ответом на нажатие (как всплывающее
        сообщение прямо сейчас), и состоянием — заказ мог быть сделан в другой
        вкладке. Собирает она ленту заново и открывает ход той реплики, за которую
        взялись (см. feed_note, page.rebuildFeed)
        """
        self.feed_note = {"text": note, "stamp": time.time(),
                          "rebuild": True, "post": int(post_id or 0)}
        print(f"  🎬 {note}" + (f": реплика №{post_id}" if post_id else ""))
        return ""

    def take_replay_next(self):
        """Переигровка, посчитанная заказом (None — никакой): берут её один раз."""
        plan, self.replay_next = self.replay_next, None
        return plan

    def version_pick(self, post_id, version) -> tuple:
        """Выбранные прежние слова реплики: (реплика, номер версии, беда).

        Ничего не меняет — по этому заказ ленты принимается сразу, а сами слова
        переключаются уже тогда, когда лента свободна (см. take_order).
        """
        index = self.index_of(post_id)
        if index is None:
            return None, None, f"реплики №{post_id} в ленте нет"
        post = self.posts[index]
        versions = list(post.get("versions") or [])
        try:
            pick = int(version)
        except (TypeError, ValueError):
            pick = -1
        if not 0 <= pick < len(versions):
            return None, None, (f"прежних слов у реплики №{post_id} столько нет: "
                                f"просят версию №{pick + 1}, а их {len(versions)}")
        if not str(versions[pick].get("content") or "").strip():
            return None, None, "в этой версии слов не было — сказать нечего"
        return post, pick, ""

    def switch_words(self, post: dict, pick: int) -> str:
        """Поставить реплике слова из её летописи: беда — словами.

        Нынешние слова при этом не пропадают: они ложатся в ту же летопись —
        тем же порядком, что и при правке. Список растёт только вперёд, и ДАМП
        читает его так же (см. parse_dump), поэтому по версиям можно ходить
        в обе стороны, а в файле остаются обе записи (см. dump_new_version).
        """
        versions = list(post.get("versions") or [])
        if not 0 <= int(pick) < len(versions):
            return (f"прежних слов у реплики №{post.get('id')} столько нет: "
                    f"просят версию №{int(pick) + 1}, а их {len(versions)}")
        was = versions[int(pick)]
        if not str(was.get("content") or "").strip():
            return "в этой версии слов не было — сказать нечего"
        post["versions"] = remember_version(post)
        post["content"] = str(was.get("content") or "")
        post["content_html"] = text.markdown_to_html(post["content"])
        # Вместе со словами возвращается и приложенное к ним: версия — это то,
        # чем реплика была целиком, и картинка тех слов — тоже (см. version_view)
        post["attachments"] = [dict(one) for one in was.get("attachments") or []]
        self.conversation_history = self.history_from_posts()
        dump_new_version(post)
        return ""

    def replay_aim(self, post_id, scope: str = "post") -> int:
        """Номер реплики, с которой начнётся переигровка (0 — не нашлось).

        Нужен странице: после переигровки показывают именно эту реплику,
        с раскрытым ходом (см. page.openTurnWhenArrives).
        """
        scope = str(scope or "post").strip().lower()
        if scope == "show":
            return int(self.posts[0].get("id") or 0) if self.posts else 0
        index = self.index_of(post_id)
        if index is None:
            return 0
        if scope in ("round", "act"):
            act = int(self.posts[index].get("round") or 0)
            index = next((place for place, one in enumerate(self.posts)
                          if int(one.get("round") or 0) == act), index)
        return int(self.posts[index].get("id") or 0)

    def plan_feed_replay(self, post_id, scope=None) -> tuple:
        """План переигровки по заказу: (план, главная реплика, беда).

        Переигрывают реплику, акт целиком или весь спектакль — по нынешним
        словам (см. replay_plan_for, replay_scope). Прежние ответы при этом
        никуда не деваются: они остаются в «прежних» (см. add_post,
        held_versions), а в ленте встаёт новый ответ — и страница переключается
        на него (см. page.openTurnWhenArrives).

        Ленту здесь не обрывают: заказ может прийти и на чужом ходу, и сказанное
        должно быть видно в ней до самого конца этого хода — а оборвёт её тот,
        кто и вправду начнёт переигровку (см. replay_show).
        """
        plan, _held, problem = self.replay_plan_for(post_id, scope, hold=False)
        if problem:
            return None, 0, problem
        # Показывать надо ту реплику, за которую взялись (см. page.openTurnWhenArrives)
        return plan, self.replay_aim(post_id, scope), ""

    def edit_post(self, post_id, content, attachments=None) -> tuple:
        """Изменить реплику человека: слова другие, а место — то же.

        Ответы, которые шли после неё, отвечали на прежние слова: чтобы модель
        ответила на новые, её реплику переигрывают (см. replay_plan_for). Сама
        же правка делает ленту и разговор честными: в них стоят новые слова.

        attachments — приложенное к реплике: None значит «не трогать» (так её
        правят, не заглядывая в картинки), а список — «вот что теперь приложено»,
        вплоть до пустого: картинку к своей реплике можно и убрать. Прежние
        картинки от этого не пропадают — они остаются при своей версии и
        возвращаются вместе со словами (см. version_view, switch_words).

        В ДАМПе реплика остаётся и прежней, и новой: файл дописывается, а
        читается последняя (см. dump_replayed), и данные новых картинок уезжают
        вместе с ней (см. attachment_section).

        Возвращает (реплика, беда): беда не пустая — править нечего.
        """
        post, problem = self.post_to_edit(post_id, content, attachments)
        if problem:
            return None, problem
        said = str(content or "").strip()
        # Прежние слова и прежнее приложенное остаются летописью — и в ДАМПе,
        # и в ленте: к ним можно вернуться (см. version_view, revert_version)
        post["versions"] = remember_version(post)
        post["content"] = said
        post["content_html"] = text.markdown_to_html(said)
        if attachments is not None:
            post["attachments"] = [dict(one) for one in attachments]
        self.conversation_history = self.history_from_posts()
        dump_new_version(post)
        print(f"✍️  Реплика человека №{post['id']} изменена: "
              f"{len(said)} знаков, приложено {len(post.get('attachments') or [])}")
        return post, ""

    def revert_version(self, post_id, version_index) -> tuple:
        """Вернуть реплике прежние слова — те, что лежат в её летописи.

        Возврат — это та же правка, только слова берутся не из формы, а из
        «прежних слов» (см. version_view): место и номер реплики остаются её
        же, а в ленте и в разговоре снова стоят прежние слова. Нынешние слова
        при этом не пропадают — они ложатся в ту же летопись, — поэтому по
        версиям можно ходить в обе стороны, а в ДАМПе остаются обе записи
        (см. dump_new_version).

        У ответа модели это выбор лучшего из её ответов: переигранная реплика
        помнит прежние, и вернуть можно любую — а переигрывать заново и не надо,
        ответ уже был сказан (см. replay_plan_for). У реплики человека то же
        самое возвращает его же слова, сказанные раньше.

        Возвращает (реплика, беда): беда не пустая — возвращать нечего.
        """
        problem = self.check_order("version", post_id=post_id, version=version_index)
        if problem:
            return None, problem
        picked, pick, _problem = self.version_pick(post_id, version_index)
        if _problem:
            return None, _problem
        problem = self.switch_words(picked, pick)
        if problem:
            return None, problem
        who = "человека" if picked.get("model_used") == "human" else "модели"
        print(f"↩️  Реплика {who} №{picked['id']} вернулась к версии "
              f"{pick + 1}: {len(picked['content'])} знаков")
        return picked, ""

    def show_report(self) -> dict:
        """Итог спектакля: сколько сыграно, сколько заново и во что обошлось.

        Считается по самой ленте, а не по памяти о ходе: реплика знает свой акт
        и цену своего хода (см. summary["spent"]), поэтому итог переживает и
        переигровку, и возврат спектакля из ДАМПа. Показывает его страница под
        занавесом (см. page.curtainReportHtml) — вместе с ценой по актам.

        Переигранное — двумя числами, потому что они про разное: «с прежними
        словами» видно и в ленте, и в ДАМПе (у сыгранной заново реплики прежние
        слова остаются летописью), а число заходов известно только про этот
        запуск (см. replay_show): сам ДАМП о заходах молчит.
        """
        acts = {}
        for post in self.posts:
            number = int(post.get("round") or 0)
            act = acts.setdefault(number, {"round": number, "turns": 0, "human": 0,
                                           "spent": 0.0, "priced": 0})
            if post.get("model_used") == "human":
                act["human"] += 1
            else:
                act["turns"] += 1
            # Цена хода — со счёта ключа, а не по тарифам (см. _note_money):
            # у реплики человека её нет вовсе, и такой ход в счёт не идёт
            spent = float(((post.get("turn") or {}).get("spent") or 0.0))
            if spent:
                act["spent"] += spent
                act["priced"] += 1
        for act in acts.values():
            act["spent"] = round(act["spent"], 2)
        return {
            "posts": len(self.posts),
            "turns": sum(act["turns"] for act in acts.values()),
            "acts": [acts[number] for number in sorted(acts)],
            "with_versions": sum(1 for post in self.posts
                                 if list(post.get("versions") or [])),
            "again": len(self.replayed_posts),
            "replays": int(self.replays or 0),
            # Что из сказанного знает свою цену: по этому видно, что итог
            # по актам полный, а не «по тем ходам, где успели посчитать»
            "priced": sum(act["priced"] for act in acts.values()),
            "spent": round(float(self.spent or 0.0), 2),
            # И картинки: спектакль — это ещё и то, что модели видели. Числа
            # те же, что в сайдбаре (см. pictures_report): итог и под занавесом,
            # и на ходу считается одним и тем же счётом
            "pictures": pictures_report(),
        }

    def new_show(self):
        """«Новый спектакль»: новые имена и характеры, настройки, сцена и тема — те же."""
        guidelines = list(self.moderator_guidelines)
        judge_rules = list(self.judge_rules)
        static_instructions = list(self.static_instructions)
        # Тема переживает и «Новый спектакль»: сброс темы — это полный сброс,
        # а не новый состав той же труппы
        topic = self.topic
        # Приложенное к теме — часть темы, а не труппы: картинку в новом спектакле
        # придётся показать заново (модель её не помнит), поэтому копия, а не она
        # сама: правка одного спектакля не должна менять отметки другого
        topic_attachments = [dict(one) for one in self.topic_attachments or []]
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
        self.topic_attachments = topic_attachments
        # «Новый спектакль» — это новая сцена: прежние реплики в ленту больше
        # не вернутся ни сейчас, ни после перезапуска (см. forget_play)
        forget_play()
        self.load_new_cast()

    # ------------------------------------------------------------
    # Работа с постами
    # ------------------------------------------------------------

    def add_post(self, display_name, model_used, content, round_num,
                 search_count=0, search_queries=None,
                 is_moderator=False, is_judge=False, gender="male", thinking="",
                 sketch="", turn=None, dump=True, attachments=None,
                 public_judge=None, replay=False):
        """Создать пост.

        dump=False — ход уже писался в ДАМП по ходу дела (см. open_dump_turn):
        дописывать его целиком второй раз — значит удвоить ход в файле.

        replay=True — эта реплика переиграна: в ДАМПе она встаёт тем же номером,
        что и прежняя, и отметка об этом ложится до неё (см. dump_replayed).
        Нужно реплике человека: у модели отметку пишет сам ход.
        """
        avatar_url = self.avatars.get(display_name)
        avatar_emoji = self.avatar_emojis.get(display_name, "📣")

        role = role_of(is_moderator, is_judge)

        if is_judge and public_judge is None:
            # Опция роли берётся у того, кто говорит сейчас: само место с этого
            # мгновения уже может уйти из состава (см. judge_is_public)
            speaker = next((p for p in self.runtime_participants
                            if p.get("display_name") == display_name), None)
            public_judge = judge_is_public(speaker) if speaker else None

        post = create_post(display_name, model_used, content, round_num,
                          avatar_url, avatar_emoji, search_count, search_queries, role,
                          gender, thinking, sketch, turn, attachments, public_judge)
        self.posts.append(post)

        # Прежние слова этой реплики отложены переигровкой (см. hold_from): они
        # остаются летописью и едут странице блоком «прежние слова»
        # (см. version_view). Это отдельно от отметки в ДАМПе: у человека она
        # пишется ниже и здесь же, а у модели — самим ходом (см. handle_ai_turn)
        was = self.held_versions.pop(int(post.get("id") or 0), None)
        if was is not None:
            post["versions"] = remember_version(was)

        if content.strip() or attachments:
            # Приложенная картинка — часть реплики: и в истории диалога она
            # лежит рядом с текстом (см. _format_history), а не живёт отдельно
            self.conversation_history.append({
                "display_name": display_name,
                "content": content,
                "is_moderator": is_moderator,
                "is_judge": is_judge,
                # Слышали ли эти слова остальные — запоминается вместе с ними:
                # ушедшего судью спрашивать будет уже не у кого (см. _judge_was_heard)
                "public_judge": public_judge,
                "round": round_num,
                "attachments": list(attachments or []),
            })
        # Размышления в память спектакля не идут (иначе следующая модель прочитала
        # бы чужой черновик мыслей как сказанное вслух), а в ДАМП — идут: там
        # хронология спектакля, и её читают после занавеса, а не посреди разговора
        if dump:
            if replay:
                dump_replayed([post["id"]])
            save_dump_entry(post, turn)
        return post

    # ------------------------------------------------------------
    # Отчёты ходов: что именно вошло в запрос и что происходило
    # ------------------------------------------------------------

    def remember_turn(self, post_id: int, payload: dict) -> None:
        """Запомнить отчёт о ходе — до конца спектакля.

        Держим все ходы текущего спектакля, а не последние несколько: отчёт
        нужен ровно тогда, когда в нём что-то понадобилось посмотреть, а это
        может быть и первая реплика («что мы вообще отправили модели?»).
        Память это не обременяет: после «Нового спектакля» список начинается
        заново, как и ДАМП на диске, а в файлы отчёты не пишутся — там лежит
        их же текст, собранный для чтения (см. save_dump_entry).
        """
        self.turn_log[int(post_id)] = payload

    def turn_report(self, post_id: int) -> dict:
        """Отчёт о ходе по номеру реплики (None — отчёта нет).

        Отдаётся копия с готовыми шагами: у каждого — время словами, а «перо»,
        которым ход писался в ДАМП, странице не нужно (это функция, и в JSON
        ей делать нечего).
        """
        payload = self.turn_log.get(int(post_id))
        if payload is None:
            return None
        view = {key: value for key, value in payload.items() if key != "sink"}
        view["steps"] = [_step_view(step) for step in (payload.get("steps") or [])]
        # А слова о картинках — нынешние, а не те, что были в тот миг: запись
        # сделана тогда, когда картинка только собиралась уехать, и «кому уехало»
        # в неё тогда ещё не попало (см. commit_shown_pictures, message_pictures)
        view["messages"] = [{**one,
                             "pictures": message_pictures_now(one)}
                            for one in (payload.get("messages") or [])]
        return view

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
    # Часы хода: сколько ждать
    # ------------------------------------------------------------

    def start_turn_clock(self, limit: int = 0, deadline: float = None) -> None:
        """Завести часы хода: по ним сайдбар считает, сколько ещё ждать.

        limit — сколько секунд дано ходу изначально, deadline — до какого
        времени (time.monotonic) он дойдёт с надбавками за поиски. Срок есть
        только у облачного хода: у местной модели его нет вовсе, и тогда
        видно одно «думает столько-то» (см. cloud.turn_deadline).
        """
        self.turn_started = time.monotonic()
        self.turn_limit = int(limit or 0)
        self.turn_deadline = deadline
        self.turn_extra = 0

    def extend_turn_clock(self, seconds: int) -> None:
        """Поиск состоялся — ходу прибавлено времени, и это видно в сайдбаре.

        Именно прибавка, а не новый срок: иначе «осталось» прыгало бы вверх
        на каждой поисковой попытке и не значило бы ничего (см. per_search_seconds).
        """
        seconds = int(seconds or 0)
        if self.turn_deadline is not None:
            self.turn_deadline += seconds
        self.turn_extra += seconds

    def stop_turn_clock(self) -> None:
        """Ход кончился: часы гасим — сайдбар не должен показывать чужое время."""
        self.turn_started = None
        self.turn_deadline = None
        self.turn_extra = 0

    def turn_elapsed(self):
        """Сколько секунд уже идёт ход (None — хода нет)."""
        if self.turn_started is None:
            return None
        return max(0.0, time.monotonic() - self.turn_started)

    def turn_left(self):
        """Сколько секунд осталось ходу по сроку (None — без предела)."""
        if self.turn_started is None or self.turn_deadline is None:
            return None
        return max(0.0, self.turn_deadline - time.monotonic())

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

    def _judge_was_heard(self, post: dict, public_judges: set,
                         judge_names: set) -> bool:
        """Слышат ли этот вердикт посторонние — по реплике, а не по составу.

        Судья ещё в составе — решает его нынешняя опция: анонимного не слышит
        никто (см. _get_history_for). А ушедшего из состава в нынешних опциях
        нет, и записанное в реплике говорит, слышали ли её тогда: иначе его
        прежние вердикты задним числом стали бы неслышными, и разговор у соседей
        поменялся бы на ходу (см. TestTheOneWhoLeftTheCast).
        """
        name = post.get("display_name", "")
        if name in judge_names:
            return name in public_judges
        return bool(post.get("public_judge"))

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
        # Публичные судьи — те, чьё слово слышат остальные. Пока место в составе,
        # считаем по текущему составу: сделав судью анонимным, его прошлые
        # вердикты тоже закрываются. А ушедшего из состава там уже нет, и о нём
        # говорит сама реплика (см. _judge_was_heard)
        judge_names = {p.get("display_name") for p in self.runtime_participants
                       if p.get("is_judge")}
        public_judges = {
            p.get("display_name") for p in self.runtime_participants
            if p.get("is_judge") and judge_is_public(p)
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
                        and not self._judge_was_heard(post, public_judges, judge_names):
                    continue
            result.append(post)
        return result

    # ------------------------------------------------------------
    # Формат истории в messages
    # ------------------------------------------------------------

    def _format_history(self, viewer_name: str, history: list, model: str = "",
                        image_keys: dict = None) -> list:
        """История — сообщениями для модели.

        model — та, которой эти сообщения уедут: она решает, что из приложенного
        уже показано ей раньше, а что едет впервые (см. images_shown).
        image_keys — необязательная посуда: в неё ложится, какие картинки
        в какие сообщения приложены, — по ней этот ход и отметит их
        показанными (см. commit_shown_pictures).
        """
        messages = []
        for post in history:
            speaker = post.get("display_name", "")
            speaker_norm = speaker.lower().replace(" ", "_")
            content = post.get("content", "")
            before = len(messages)

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

            # Приложенные к реплике картинки едут вместе с ней: у Ollama они
            # живут полем images рядом с текстом. Кому их не читать — решает
            # ollama_api.messages_for: там известна модель, которой всё это
            # уезжает, а здесь — только история
            #
            # Каждой модели картинка уезжает один раз: показанную она уже
            # видела, а платить за неё заново на каждом ходу — не за что.
            # Показанное не пропадает из реплики совсем: вместо самой картинки
            # остаётся строка о ней (см. images_already_shown_note)
            shown = self.images_shown.get(model, set()) if model else set()
            fresh, stale = [], []
            for one in post.get("attachments") or []:
                url = str((one or {}).get("url") or "")
                (stale if url in shown else fresh).append(one)
            # Только то, что правда прочиталось: пропавший файл — это не
            # картинка, и показывать о ней строку нечего
            pairs = post_image_pairs(fresh)
            if pairs and len(messages) > before:
                messages[-1]["images"] = [payload for _url, payload in pairs]
                if image_keys is not None:
                    image_keys[id(messages[-1])] = [url for url, _payload in pairs]
            if stale and len(messages) > before:
                messages[-1]["content"] += "\n\n" + images_already_shown_note(stale)
                # В консоли — один раз на модель и картинку: иначе эта же
                # строка повторялась бы на каждом ходу до конца спектакля
                for one in stale:
                    url = str((one or {}).get("url") or "")
                    if (model, url) in _IMAGES_NOT_RESENT:
                        continue
                    _IMAGES_NOT_RESENT.add((model, url))
                    name = str((one or {}).get("name") or url.rsplit("/", 1)[-1])
                    print(f"  🖼  {model} уже видел картинку «{name}» — "
                          f"повторно не отправляю, только строку о ней")
        return messages

    def topic_picture_message(self, model: str, image_keys: dict = None) -> dict:
        """Сообщение о приложенной к теме картинке — или None, если её нечего показывать.

        Собирается оттуда же, откуда уезжает приложенное к репликам, и по тем же
        правилам (см. _format_history): каждой модели — один раз, а показанным
        картинка становится только после удачного хода (см. commit_shown_pictures),
        поэтому не успевшая ответить получит её на своём следующем ходу. Дальше
        о картинке не говорят вовсе: она показана, и разговор идёт своим ходом —
        так же, как у картинки из реплики, только там вместо самой картинки
        остаётся строка о ней, а у темы о ней говорит сам системный промпт.

        Модели без зрения картинка не едет: тему она читает и так, а строка
        о невидимой картинке была бы только шумом (см. ollama_api.messages_for).
        """
        if not self.topic_attachments:
            return None
        shown = self.images_shown.get(model, set()) if model else set()
        fresh = [one for one in self.topic_attachments
                 if str((one or {}).get("url") or "") not in shown]
        if not fresh:
            return None
        # Спрашивать про зрение раньше не о чем: пока картинка не свежа, показывать
        # её некому (см. takes_images — у облачной это запрос за деньги)
        if not ollama_api.takes_images(model):
            return None
        pairs = post_image_pairs(fresh)
        if not pairs:
            return None
        names = {str((one or {}).get("url") or ""):
                 str((one or {}).get("name") or str((one or {}).get("url") or "")
                     .rsplit("/", 1)[-1])
                 for one in fresh}
        urls = [url for url, _payload in pairs]
        listed = ", ".join(f"«{names.get(url) or url}»" for url in urls)
        one = len(pairs) == 1
        message = {
            "role": "user",
            "content": ("🖼 К теме обсуждения "
                        + ("приложена картинка " if one else "приложены картинки: ")
                        + listed
                        + (" — посмотри на неё: она относится к теме, " if one
                           else " — посмотри на них: они относятся к теме, ")
                        + "о которой пойдёт разговор."),
            "name": "topic",
            "images": [payload for _url, payload in pairs],
        }
        if image_keys is not None:
            image_keys[id(message)] = urls
        return message

    def remember_pictures_to_send(self, model: str, messages: list,
                                  image_keys: dict) -> None:
        """Запомнить, какие картинки уедут этой модели на этом ходу.

        Показанными они станут только в конце хода (см. commit_shown_pictures):
        запросов в ходе несколько (попытка, поиск, повтор после него), и во всех
        картинка должна быть — иначе модель увидела бы её в первом запросе
        и потеряла во втором. А обрезанная история в счёт не идёт: выброшенное
        сообщение до модели не доехало, и показанным его считать нельзя.
        """
        # Прежняя запись стирается всегда: она про другой ход, и удачный ход
        # этого мог бы отметить показанным то, что в нём не уехало вовсе
        self.images_pending.pop(model, None)
        # И прежняя запись про ход без приложения — тоже: она осталась бы,
        # если тот ход кончился ошибкой (см. cloud.forget_pictures_dropped)
        cloud.forget_pictures_dropped(model)
        sending = {url for message in messages
                   for url in (image_keys or {}).get(id(message), [])}
        if sending:
            self.images_pending[model] = sending

    def commit_shown_pictures(self, model: str) -> None:
        """Ход состоялся — его картинки считаются показанными этой модели.

        И только той, кто их правда читает: незрячей модели приложенное
        не уезжает вовсе (см. ollama_api.messages_for), и строка «уже показана»
        была бы в ней враньём.

        И только если они вправду уехали: шлюз бывает не согласен с приложенным,
        и реплика уходит без него (см. cloud.take_pictures_dropped). Отметить
        такую картинку показанной — значит больше никогда её не послать,
        а модели сказать «ты её видела» про то, чего она не видала.
        """
        sending = self.images_pending.pop(model, None)
        dropped = cloud.take_pictures_dropped(model)
        if sending and not dropped and ollama_api.takes_images(model):
            self.images_shown.setdefault(model, set()).update(sending)
            # И в ДАМП: картинка уехала — вот какою (см. dump_note_picture_travel).
            # И только про уехавшее: то, что осталось в стороне, файл не поминает
            dump_note_picture_travel(sending)

    def forget_shown_pictures(self) -> None:
        """Всё приложенное — снова невиданное: и в памяти, и в показанном.

        Нужно там, где перед моделью совсем другой разговор: ход нового
        спектакля и все, что к нему притрагивается (см. start_show, forget_play).
        А продолжение прежнего — не новый разговор, и память ему возвращает
        история (см. restore_shown_pictures).
        """
        self.images_shown = {}
        self.images_pending = {}

    def spoken_count(self) -> int:
        """Сколько реплик уже сказано — по истории хода и по ленте, что больше.

        Историю читает ход (см. _format_history), а ленту — возврат спектакля
        (см. restore_shown_pictures), и числа у них совпадают не всегда: спектакль
        вернулся из ДАМПа — история хода ещё пуста, а лента полна
        (см. load_play_from_dump), а тему и приложенное к ней правят как раз перед
        «Доиграть». Берём большее: ошибиться в меньшую сторону — решить, что
        картинка уже показана, и не показать её никому.
        """
        return max(len(self.conversation_history), len(self.posts))

    def forget_topic_pictures(self) -> None:
        """Приложенное к теме — снова невиданное: тему сменили, и разговор с ней другой.

        Тема — это про что спектакль: с новой темой модели не слышали приложенного
        к ней вовсе, поэтому картинка темы снова свежая у каждой — и каждая получит
        её по разу (см. topic_picture_message). Приложенное к репликам это не
        трогает: оно принадлежит своим репликам, и смена темы их не переписывает.

        Отметка `since` становится той репликой, на которой тему сменили: кто
        говорил раньше, тот картинку с новой темой не застал и после возврата
        спектакля получит её заново (см. restore_shown_pictures).
        """
        urls = {str((one or {}).get("url") or "")
                for one in self.topic_attachments or []}
        urls.discard("")
        if not urls:
            return
        for model in list(self.images_shown):
            left = set(self.images_shown[model]) - urls
            if left:
                self.images_shown[model] = left
            else:
                del self.images_shown[model]
        # И ход, начатый до смены темы, картинку показанной не отметит: он про
        # прежний разговор, а с новым она ещё не уезжала (см. commit_shown_pictures)
        for model in list(self.images_pending):
            left = set(self.images_pending[model]) - urls
            if left:
                self.images_pending[model] = left
            else:
                del self.images_pending[model]
        # Отметка — сколько реплик уже сказано (см. spoken_count): кто говорил
        # раньше неё, тот картинку с новой темой не застал
        now = self.spoken_count()
        for one in self.topic_attachments or []:
            if isinstance(one, dict):
                one["since"] = now

    def restore_shown_pictures(self, history: list = None) -> None:
        """Вспомнить по истории, кто из моделей уже видел приложенное.

        Ход модели — это и её ответ на картинку: если после реплики с картинкой
        эта модель ещё говорила, значит картинка до неё дошла и она её видела
        (см. _format_history) — повторно показывать её нечего, и в контексте
        останется только строка о ней. А тому, чей ход картинку не застал
        (или оборвался на ней смертью процесса), она уедет заново: не увидеть
        картинку он мог только так.

        Нужно продолжению прежнего спектакля — и из ДАМПа, и из архива:
        сама история говорит, кому что показано, и отдельного хранилища
        для этого не нужно (см. start_show).
        """
        history = list(self.conversation_history if history is None else history)
        # Где в истории говорил каждый: по последнему слову видно, застал ли
        # ход модели картинку (модератор и живой участник картинок не читают,
        # и в показанном им делать нечего)
        last_spoken = {}
        for index, post in enumerate(history):
            last_spoken[str(post.get("display_name") or "")] = index
        last_of_model = {}
        for participant in self.runtime_participants:
            model = str((participant or {}).get("model") or "")
            if not model or model == "human":
                continue
            said = last_spoken.get(str((participant or {}).get("display_name") or ""))
            if said is not None:
                # Мест с одной моделью бывает и два: «уже говорил кто-то из них»
                # и значит «эта модель картинку видела» — так же, как на ходу
                # (см. commit_shown_pictures: показанное помнится моделью)
                last_of_model[model] = max(said, last_of_model.get(model, -1))
        shown = {}
        for index, post in enumerate(history):
            urls = [str((one or {}).get("url") or "") for one in post.get("attachments") or []]
            urls = [url for url in urls if url]
            if not urls:
                continue
            for model, said in last_of_model.items():
                if said > index:
                    shown.setdefault(model, set()).update(urls)
        # Приложенное к теме — не к реплике: показывать его начали в тот момент,
        # когда в ленте было `since` реплик (см. set_topic_attachments). Значит,
        # его видел тот, чьё слово идёт после этой отметки, — и после возврата
        # спектакля видно то же, что было на ходу. У картинки, приложенной до
        # начала, отметка 0: её видел каждый, кто вообще говорил
        for one in self.topic_attachments or []:
            url = str((one or {}).get("url") or "")
            if not url:
                continue
            since = _as_int((one or {}).get("since"))
            for model, said in last_of_model.items():
                if said >= since:
                    shown.setdefault(model, set()).update([url])
        self.images_shown = shown
        self.images_pending = {}
        count = sum(len(urls) for urls in shown.values())
        if count:
            print(f"  🖼  Прежние картинки в памяти: {count} показанных — "
                  f"повторно не поедут, останутся строками о них")

    # ------------------------------------------------------------
    # Блоки системного промпта
    # ------------------------------------------------------------

    def _substitute(self, text: str, participant: dict, other_names: list) -> str:
        text = text.replace("{ИМЯ}", participant.get("display_name", ""))
        text = text.replace("{СОБЕСЕДНИКИ}", ", ".join(other_names))
        text = text.replace("{ТЕМА}", self.topic)
        return text

    def rules_of_communication(self) -> list:
        """Правила общения, которые сейчас говорятся моделям.

        Выключенное правило на паузе: оно ждёт своего часа в редакторе и в файле,
        но спектаклю не говорится (см. rules_in_force). Пустой список правил —
        это «как в settings.py», а все выключенные — это «правил нет»: второе
        режиссёр выбрал сам, и подсовывать ему дефолты обратно нельзя.
        """
        if not self.static_instructions:
            return list(settings.DEFAULT_STATIC_INSTRUCTIONS)
        return rules_in_force(self.static_instructions)

    def guidelines_in_force(self) -> list:
        """Руководства модератора, которые сейчас в силе (выключенные — нет)."""
        return rules_in_force(self.moderator_guidelines)

    def _base_rules_block(self, participant: dict) -> list:
        other_names = [
            n for n in self._get_participants_list()
            if n != participant.get("display_name")
        ]
        if participant.get("is_judge"):
            rules = rules_in_force(self.judge_rules or settings.DEFAULT_JUDGE_RULES)
        else:
            rules = self.rules_of_communication()
        return [self._substitute(r, participant, other_names) for r in rules]

    def _guidelines_block(self) -> list:
        guidelines = self.guidelines_in_force()
        if not guidelines:
            return []
        return [
            "ПРАВИЛА ОТ РУКОВОДИТЕЛЯ ДИАЛОГА (обязательны к исполнению):\n"
            + "\n".join(f"• {g}" for g in guidelines)
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

    # Тот самый текстовый вызов, который узнаёт и выполняет tooltext.take_calls:
    # он же годится как образец для модели, которой инструмент не выдан
    SEARCH_CALL_EXAMPLE = 'search:web_search{query: "твой запрос"}'

    def _search_block(self, participant: dict) -> list:
        """Правила поиска для модели — с обоими числами из настроек.

        Минимум — чтобы модель искала до первого слова, а не после (см.
        SEARCH_BEFORE_REPLY). Максимум назван не зря: модель, не знающая
        потолка, просит поиск снова и снова — а ей отказывают молча,
        и со стороны это выглядит как задумавшаяся модель (см. MAX_SEARCHES).

        А вот способ поиска бывает разным. Если инструмент этой модели не
        отправляется (шлюз его не принял, или его нет у местной модели),
        говорить «поиск не проси» нельзя: именно это загнало судью на
        qwen3.8-flash в двухминутную петлю — правила просили поиск, а способа
        не было ни одного, и ход кончился обрывом по CLOUD_TURN_LIMIT. Вместо
        запрета такой модели даётся рабочий способ: просьба словами, её узнаёт
        и выполняет код хода (см. tooltext). Числа поиска при этом остаются
        в силе — и минимум, и потолок (см. ollama_api.takes_tools_now).
        """
        if not settings.ENABLE_SEARCH:
            return []
        min_text = (f" Сделай минимум {settings.MIN_SEARCHES} поиск(ов) перед ответом."
                    if settings.MIN_SEARCHES > 0 else "")
        max_searches = max(1, int(settings.MAX_SEARCHES))
        max_text = (f" За один ход разрешено не больше {max_searches} поиск(ов): "
                    f"израсходовал их — говори по тому, что уже нашлось.")
        if not ollama_api.takes_tools_now(participant.get("model", "")):
            return [
                "Инструмент поиска тебе сейчас не выдан протоколом, но поискать "
                "ты можешь: напиши отдельной строкой ровно так: "
                f"{self.SEARCH_CALL_EXAMPLE} — приложение выполнит поиск "
                "и вернёт тебе найденное, а ты ответишь по нему. Сама строка "
                "из реплики убирается: в посте спектакля её не будет. "
                "Источники не выдумывай: чего не нашёл — то говори от себя."
                + min_text + max_text
            ]
        return [
            "Если есть сомнения в фактах или мнениях - используй поиск для уточнения. "
            "При поиске НЕ указывай год." + min_text + max_text
        ]

    def get_system_prompt(self, participant: dict) -> str:
        is_judge = participant.get("is_judge", False)
        blocks = []
        blocks.extend(self._base_rules_block(participant))
        blocks.extend(self._guidelines_block())
        if not is_judge:
            blocks.extend(self._moderator_intro_block())
        blocks.extend(self._personal_instruction_block(participant))
        blocks.extend(self._search_block(participant))
        return "\n\n".join(b for b in blocks if b)

    # ------------------------------------------------------------
    # Сборка сообщений для модели
    # ------------------------------------------------------------

    def build_messages_for_ai(self, participant: dict, round_num: int,
                              report: dict = None) -> list:
        """Сообщения для этой модели на этом ходу.

        report — необязательная посуда: если её передали, в неё ляжет отчёт
        обрезки истории (чьё окно, сколько было и что выброшено). Так снимок
        «что вошло в запрос» получается из тех же чисел, которыми история
        и резалась, а не из второго, отдельного расчёта.
        """
        name = participant.get("display_name", "")
        name_norm = name.lower().replace(" ", "_")
        is_judge = participant.get("is_judge", False)
        # Кому едут эти сообщения: по модели видно, что из приложенного она уже
        # видела (см. images_shown), а image_keys собирает то, что приложено
        # к этому ходу — по нему ход и отметит картинки показанными
        model = participant.get("model", "")
        image_keys = {}

        system_prompt = self.get_system_prompt(participant)
        messages = [{"role": "system", "content": system_prompt, "name": "system"}]

        # ---- Судья ----
        if is_judge:
            # Что судья слышит, решает опция роли: всё обсуждение или только этот акт
            viewer_options = role_options(participant)
            history = self._get_history_for(participant, mode="participants_only",
                                            round_num=round_num)
            history_messages = self._format_history(name, history, model,
                                                    image_keys)
            trimmed, trim_report = text.trim_history_with_report(
                history_messages, text.estimate_tokens(system_prompt),
                model=participant.get("model", ""))
            # Картинка темы — перед историей: она вводит разговор, а не отвечает
            # на чужую реплику (см. topic_picture_message)
            topic_message = self.topic_picture_message(model, image_keys)
            if topic_message is not None:
                messages.append(topic_message)
            messages.extend(trimmed)
            self.remember_pictures_to_send(model, messages, image_keys)
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
        history_messages = self._format_history(name, history, model, image_keys)
        # Окно — той модели, которая будет говорить: у облачного оно своё,
        # иначе история сцены режется по олламовским 7 тысячам токенов
        trimmed, trim_report = text.trim_history_with_report(
            history_messages, text.estimate_tokens(system_prompt),
            model=participant.get("model", ""))
        # Картинка темы — первым сообщением, сразу после системного промпта:
        # она о теме, а не о чьей-то реплике, и место её — перед разговором
        topic_message = self.topic_picture_message(model, image_keys)
        if topic_message is not None:
            messages.append(topic_message)
        messages.extend(trimmed)
        self.remember_pictures_to_send(model, messages, image_keys)
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
                # Реплика собеседника здесь НЕ цитуется повторно: она уже уехала
                # строкой истории чуть выше (см. _format_history), и раньше
                # модель читала одно и то же сообщение дважды — один раз как
                # реплику сцены, второй — в этом задании. Каждое слово сцены
                # должно уезжать в модель ровно один раз: дубли искажают и вес
                # истории, и то, как модель читает разговор
                if len(participant_posts) == 1:
                    messages.append({
                        "role": "user",
                        "content": (
                            f'{last_speaker} только что высказался — его реплика '
                            f'выше, в истории диалога. Как {name}, ты тоже '
                            f'начинаешь обсуждение. Ответь {last_speaker} '
                            f'и вырази свою позицию по теме.'
                        ),
                        "name": name_norm,
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": (
                            f'{last_speaker} только что высказался — его реплика '
                            f'выше, в истории диалога. Как {name}, ответь ему и '
                            f'другим участникам. '
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

    def _note_money(self, journal: dict, before, before_error: str = "") -> float:
        """Цена хода по остатку на ключе — и шаг об этом в хронологию.

        Тарифов шлюза приложение не знает и знать не может: они в его каталоге,
        в рублях за 1 000 000 токенов, и меняются без спроса. Зато остаток шлюз
        отдаёт сам, и разница «до хода» и «после хода» — это не оценка, а факт
        со счёта: размышления, поиски и кэш в неё входят так же, как в списание.

        Возвращает цену хода (None — не знаем: шлюз остатка не дал, разрешения
        нет, или остаток вырос — пополнение посреди хода). Ход из-за этого
        не останавливается и не падает: не знать — допустимо, выдумывать — нет.
        """
        if not cloud.balance_url():
            return None
        if before is None:
            # Шлюз остатка не дал: сказать об этом надо один раз за спектакль,
            # а не на каждом ходу — иначе в хронологии утонет всё остальное
            if before_error and not self.money_told:
                self.money_told = True
                detail = (f"цену ходов показать не могу — {before_error}")
                print(f"  💰 {detail}")
                cloud.journal_push(journal, {"kind": "money", "t": time.time(),
                                             "text": detail})
            return None
        after, error = cloud.balance(force=True)
        first = cloud.balance_number(before)
        second = cloud.balance_number(after)
        if first is None or second is None:
            cloud.journal_push(journal, {
                "kind": "note", "t": time.time(),
                "text": ("цену хода не узнать: остаток на ключе прочитать не удалось"
                         + (f" — {error}" if error else "")),
            })
            return None

        spent = cloud.balance_spent(before, after)
        if spent is None:
            text = (f"баланс ключа: до хода {money(first)} → после {money(second)} — "
                    f"списания за этот ход не видно (счёт не изменился или был "
                    f"пополнен)")
            print(f"  💰 Остаток ключа: {money(second)}, а списания за ход не видно")
        else:
            text = (f"баланс ключа: до хода {money(first)} → после {money(second)} — "
                    f"за этот ход списано {money(spent)}")
            print(f"  💰 За ход списано {money(spent)} (остаток {money(second)})")
            self.spent = float(self.spent or 0.0) + spent
        cloud.journal_push(journal, {"kind": "money", "text": text, "t": time.time()})
        return spent

    # ------------------------------------------------------------
    # Ход AI
    # ------------------------------------------------------------

    def handle_ai_turn(self, participant: dict, round_num: int, on_draft=None,
                       replay: bool = False) -> tuple:
        self.current_action = "thinking"
        # По этой метке реплика назовёт, сколько длилась её генерация: время —
        # такая же плата за ход, как токены и поиски, и морочить им голову
        # секундомером в руке не надо (см. refresh_turn_report)
        started = time.time()
        # Отчёт обрезки приходит из той же сборки сообщений, что и сами
        # сообщения: отдельного пересчёта для показа нет
        trim_report = {}
        messages = self.build_messages_for_ai(participant, round_num, report=trim_report)
        # Снимок делается ДО хода и копией: ask_model дописывает в этот же список
        # результаты поиска прямо на месте, и к концу хода «что вошло» было бы
        # уже не тем, с чего ход начался
        sent = copy.deepcopy(messages)
        # Журнал хода: сюда ляжет всё, что происходит по порядку — запросы с их
        # входом и выводом, поиски с формулировкой и весом найденного,
        # размышления модели. Тот же журнал дописывается в ДАМП, причём каждое
        # событие уходит в файл в тот момент, когда случилось
        journal = {"steps": []}
        # Остаток на ключе — ДО хода: цену хода даёт разница остатков, а не
        # тарифы, которых мы не знаем (см. cloud.balance и _note_money). Нет
        # настроенного запроса баланса — нет и цены: токены всё равно точные.
        # И только для облачного участника: у местной модели платить не за что,
        # и спрашивать остаток на её ходу значило бы два запроса впустую
        cloud_turn = cloud.is_cloud_model(participant.get("model", ""))
        money_before, money_error = (cloud.balance(force=True)
                                     if cloud_turn and cloud.balance_url()
                                     else (None, ""))
        # Отчёт собирается ДО запроса к модели: ДАМП надо открыть и записать
        # «что вошло в запрос» раньше, чем модель начнёт думать — иначе у хода,
        # зациклившегося на две минуты, в файле не осталось бы вообще ничего
        turn = build_turn_report(participant, round_num, sent, [], trim_report,
                                 topic=self.topic, steps=journal["steps"])
        # Переигранная реплика встаёт тем же номером, что и прежняя: номер — это
        # место в ленте. Прежняя запись о ней остаётся в ДАМПе летописью, а
        # читается последняя (см. dump_replayed)
        if replay:
            dump_replayed([len(self.posts) + 1])
        open_dump_turn(len(self.posts) + 1, turn, journal)

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
                report=journal,
                # Сайдбару — «Ищет: …» прямо на ходу и часы: сколько уже
                # думает и сколько осталось (см. start_turn_clock)
                show_session=self,
            )
        finally:
            # Черновик закрываем при любом выходе, в том числе при ошибке: иначе
            # недописанная реплика осталась бы висеть в ленте навсегда
            if draft is not None:
                draft.finish()
            # И часы хода гасим тут же: модель уже не думает, а по этим часам
            # сайдбар считает, сколько ей осталось (чтобы после хода там
            # не висело чужое время)
            self.stop_turn_clock()

        # Ход дошёл до ответа — приложенное к нему считается показанным этой
        # модели: второй раз за ту же картинку платить нечем (см.
        # commit_shown_pictures). При ошибке сюда не дойдёт, и картинка
        # поедет заново — это и нужно: модель её так и не увидела
        self.commit_shown_pictures(participant.get("model", ""))

        # Размышления, не попавшие в хронологию шагами (модель без потока, шлюз,
        # отдавший размышления одним куском): мысли оплачены, и это единственный
        # след того, чем модель занималась, — терять его нельзя.
        # И делается это ДО сводки: иначе шаг есть, а в счёте «размышлений N»
        # в ленте его нет — цифра и содержимое хода расказывали разное
        thoughts = draft.thinking_full() if draft else ""
        turn["thinking"] = thoughts
        if thoughts and not any(step.get("kind") == "thought"
                                for step in turn["steps"]):
            cloud.journal_thought(journal, thoughts, replace=True)

        # То, что стало известно к концу хода: сколько он дописал сам и сколько
        # раз просил поиск. Шаги при этом уже в отчёте — журнал у них общий
        spent = self._note_money(journal, money_before, money_error) if cloud_turn else None
        refresh_turn_report(turn, messages[len(sent):], search_count, journal, spent,
                            seconds=max(0.0, time.time() - started))

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
            # Мысли, сказанные по дороге к ответу: в ленте они внутри того же
            # блока о ходе, в ДАМПе — текстом. Черновик держит их для этого
            thinking=draft.thinking_full() if draft else "",
            # А это прежняя версия реплики, от которой модель отказалась по пути
            # (обычно — чтобы сначала поискать): тем же блоком, чтобы зритель
            # мог дочитать то, что мелькнуло в ленте и пропало
            sketch=draft.sketch if draft else "",
            # А это полный отчёт о ходе: у этой реплики его можно раскрыть
            # и посмотреть, что именно прочитала модель
            turn=turn,
            # Ход уже лёг в ДАМП по событию — второй раз его писать не надо
            dump=False,
        )
        # Ответ едет в отчёте вместе с остальным: в ленте он и так есть, а вот
        # в ДАМПе ход должен заканчиваться тем, чем он кончился
        turn["answer"] = response
        close_dump_turn(post, turn)
        self.remember_turn(post["id"], turn)
        self.current_action = None
        # Пауза — для страницы, а не для модели: реплика уже в ленте, а браузеру
        # надо успеть её показать, иначе ходы сливаются в один мигающий поток
        # (см. settings.TURN_PAUSE)
        if settings.TURN_PAUSE:
            time.sleep(settings.TURN_PAUSE)
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


def set_participant_emoji(name: str, emoji: str) -> str:
    """Поставить участнику другой эмодзи-аватар — из ленты или из состава.

    Эмодзи — свойство места в составе, поэтому правка идёт в состав и сохраняется
    в файле пульта: в ленте и на странице состава это одно и то же лицо
    (см. post_view), и поменянное лицо видно у всех реплик участника —
    и у тех, что уже сказаны.
    """
    emoji = str(emoji or "").strip()
    if emoji not in settings.AVATAR_EMOJIS:
        return "Такого эмодзи в наборе нет"
    for place in session.runtime_participants:
        if place.get("display_name") == str(name or ""):
            place["avatar_emoji"] = emoji
            session.sync_cast_media()
            save_theatre_settings("эмодзи-аватар участника")
            return ""
    return f"Участника «{name}» в составе нет"


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


def report_cast_vision(participants: list) -> list:
    """Спросить про картинки у всех моделей состава — до первого хода.

    Режиссёр должен знать заранее, кому приложенное уедет, а не узнавать это
    на сцене: слепая модель получила бы отказом посреди хода. Спрашивают
    и в пульте, при загрузке состава (см. page.autoVision), — но спектакль
    может играть и с закрытой страницей, поэтому тот же вопрос задаётся
    ещё раз здесь, в самом начале потока, и здесь он задаётся **заново**:
    прежние ответы забываются (см. cast_vision, again=True). Модели меняются
    между запусками, а ошибка в этом стоит весь спектакль: зрячая модель,
    записанная слепой, не получила бы ни одной картинки.
    """
    checked = ollama_api.cast_vision(participants, again=True)
    if checked:
        print(f"  👁  {ollama_api.vision_words(checked)}")
        # И почему решено именно так — одной строкой: у местной модели это
        # приметы в метаданных, у облачной — пробный запрос, а бывает и догадка
        # по имени, когда спросить не вышло (см. vision_reasons). Без этой
        # строки «картинки не читает» не разобрать на «так и есть» и «мы
        # не спросили»
        reasons = ollama_api.vision_reasons(checked)
        if reasons:
            print(f"  👁  почему: {reasons}")
    return checked


def report_topic_picture(participants: list) -> None:
    """Кому уедет приложенное к теме — словами, как и про зрение состава.

    Картинка темы уезжает не всем и не всегда: без зрения она не едет вовсе,
    а показанную модель помнит и второй раз не получает (см.
    topic_picture_message). Сказать об этом заранее — то же, что и про само
    зрение (см. report_cast_vision): иначе «приложил, а реакции нет» выглядит
    как поломка, а не как слепая модель.
    """
    if not session.topic_attachments:
        return
    names = ", ".join(f"«{one.get('name') or one.get('url')}»"
                     for one in session.topic_attachments)
    readers = []
    for participant in participants or []:
        model = str((participant or {}).get("model") or "")
        if model and model != "human" and ollama_api.takes_images(model):
            readers.append((participant or {}).get("display_name") or model)
    if readers:
        print(f"  🖼  К теме приложено: {names} — уедет в первом акте: "
              + ", ".join(readers))
    else:
        print(f"  🖼  К теме приложено: {names} — но читать её некому: "
              f"картинок не читает ни одна модель состава")


def merged_held(first: dict, second: dict) -> dict:
    """Отложенное двух переигровок — одним: без потерь и без повторов.

    Каждая переигровка откладывает свою часть ленты (см. hold_from), и части
    эти пересекаются: вторая переигровка могла начаться и раньше, и позже
    первой. Соединяем по номеру реплики: под одним номером лежит одна и та же
    реплика, и вторая запись вернее первой — она считалась позже.
    """
    posts = {}
    for one in (first or {}).get("posts") or []:
        posts[int(one.get("id") or 0)] = one
    for one in (second or {}).get("posts") or []:
        posts[int(one.get("id") or 0)] = one
    turns = dict((first or {}).get("turns") or {})
    turns.update((second or {}).get("turns") or {})
    return {"posts": [posts[key] for key in sorted(posts)], "turns": turns}


def run_debate_thread(topic: str, on_post=None, on_draft=None, replay: dict = None):
    """
    Играет спектакль акт за актом, пока режиссёр не опустит занавес.

    on_post — как сообщить о новой реплике (обычно это Socket.IO из web.py).
    on_draft — как сообщить о том, что реплика пишется прямо сейчас: пока
    облачная модель говорит, в ленте растёт черновик (см. _StreamingReply).
    Здесь про Flask не знают: спектакль просто отдаёт готовый пост тому, кто
    его позвал.

    replay — переигровка сказанного (см. session.replay_plan_for): с какого
    места лента уже оборвана, сколько реплик играется заново и что ждёт
    возвращения в ленту. Спектакль отвечает на те же слова заново — по теме
    и правилам, которые действуют сейчас.
    """
    print(f"🎬 Поток дебатов запущен для темы: {topic}")
    # Хронология ходов уйдёт в ДАМП: после занавеса ленты уже не будет,
    # а по ДАМПу можно разобрать любой ход — от промпта до токенов.
    # Продолжение спектакля пишется в тот же файл: прежние реплики —
    # это его же начало, и сносить их, чтобы записать заново, нечего
    start_dump(topic, keep=session.resumed)
    print(f"  🗒  ДАМП спектакля: {settings.DUMP_FILE}"
          + (" (продолжение дописывается в тот же файл)" if session.resumed
             else " (пишется заново на каждый спектакль)"))
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

    # Кому уедет приложенное — выясняется до первого хода, и заново: прежние
    # ответы про зрение при этом забываются (см. report_cast_vision, cast_vision).
    # Об этом говорят и пульт, и эта строка, а моделей без зрения больше не
    # застают врасплох. Больше нигде этот вопрос сам не задаётся: при правке
    # состава страница спрашивает только про новых (см. page.autoVision)
    report_cast_vision(runtime_participants)
    # И про картинку темы — тем же порядком: приложил режиссёр — сразу видно,
    # кому она уедет, а кому нет (см. report_topic_picture)
    report_topic_picture(runtime_participants)

    print("\n🎭 Используем подготовленный грим и костюмы...")
    for participant in runtime_participants:
        display_name = participant.get("display_name", "")
        if display_name and display_name in session.avatars:
            print(f"  ✅ {display_name}: готов к выходу на подмостки")
        else:
            print(f"  ⚠️  {display_name}: грим не подготовлен (будет эмодзи)")
    
    # Ход, оборванный смертью процесса, переспрашивается первым делом: прежние
    # реплики целы, а в этом ходу говорящий не сказал ничего (см. plan_replay),
    # и дальше разговор идёт уже с его репликой — так цепочка и восстанавливается
    asked_again = session.replay_broken_turn(on_draft=on_draft)
    if asked_again and on_post:
        on_post(asked_again)

    # Продолжение спектакля идёт дальше с того же акта, а не с первого: прежние
    # реплики уже в ленте и уже прочитаны моделями, и акт 1 на свежую голову
    # в середине разговора — это не нумерация, а путаница (см. session.resumed)
    round_num = session.current_round if session.resumed else 0
    # А переигровка начинается с того акта, в котором стоит переигранная реплика:
    # акт 1 посреди разговора значил бы то же самое (см. replay_plan_for)
    if replay:
        round_num = max(0, int(replay.get("round") or 1) - 1)
    # Сколько реплик осталось переиграть и от чего считать: переигровка кончается
    # сама, на том же числе реплик, что было, — играется сказанное заново,
    # а не продолжение спектакля (см. session.replay_plan_for)
    again_left = int(replay.get("posts") or 0) if replay else 0
    again_base = len(session.posts)
    again_done = False

    def enough() -> bool:
        """Сказано ли уже всё переигранное: по числу реплик, а не по акту."""
        return bool(replay) and len(session.posts) - again_base >= again_left

    def serve_order() -> bool:
        """Заказ ленты, если он есть: True — заказана переигровка.

        Зовётся между ходами: под идущим ходом лента занята, и правку из другого
        потока встала бы посреди незаконченной записи (см. session.take_order).
        Сделанный заказ тут же виден и странице: она узнаёт о нём состоянием
        (см. session.feed_note, status_payload).
        """
        if session.feed_order is None:
            return False
        problem = session.take_order()
        if problem:
            print(f"  ⚠️  Заказ ленты не сделан: {problem}")
        if session.replay_next is None:
            return False
        print("  ⏹  Заказана переигровка — этот ход уступает ей место")
        return True

    try:
        while True:
            # Заказы ленты (правка, возврат версии, переигровка) разбираются
            # между ходами: спектакль из-за них не останавливают (см.
            # session.order_feed)
            if serve_order():
                again_done = True
                break
            # Переигранное сказано — занавес: следующей была бы уже вторая
            # половина спектакля, а её никто не просил (см. replay_plan_for)
            if enough():
                print("  ⏹  Переигранное сказано — опускаю занавес")
                again_done = True
                break
            round_num += 1
            session.current_round = round_num
            print(f"\n🎭 Акт {round_num}")
            
            # Состав перечитываем на каждом акте: режиссёр мог убрать место или
            # добавить новое прямо на ходу, и этот акт играется уже новым составом
            act_cast = list(session.runtime_participants)
            print(f"  🎭 Персонажи: {[p.get('display_name', '') + ' (' + p.get('model', '') + ')' for p in act_cast]}")
            
            # С какого места состава начинается акт: у переигровки это тот, чья
            # реплика переигрывается, а не первый в очереди
            first = 0
            if replay and round_num == int(replay.get("round") or 0):
                first = next((place for place, one in enumerate(act_cast)
                              if one.get("display_name") == replay.get("name")), 0)
            
            for participant in act_cast[first:]:
                # И посреди акта — тот же разбор заказов: правку просят, когда
                # удобно режиссёру, а не к концу круга
                if serve_order():
                    again_done = True
                    break
                # И посреди акта — тем же счётом: переигранное могло кончиться
                # на любом месте очереди
                if enough():
                    print("  ⏹  Переигранное сказано — опускаю занавес")
                    again_done = True
                    break
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
                    session.pending_attachments = []
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
                        
                        # Правку ленты можно заказать и пока ждём человека:
                        # делать её, кроме нас, некому, а ход от неё не пропадает
                        if (session.feed_order is not None
                                and session.feed_order.get("kind") != "replay"):
                            serve_order()
                            continue
                        # Заказанная переигровка — другое дело: она забирает
                        # ленту себе, и этот ход ей уступает (см. serve_order)
                        if serve_order():
                            again_done = True
                            break

                        current_message = session.moderator_message
                        if current_message is not None:
                            session.waiting_for_human = False
                            # Приложения забираются вместе с репликой — и только
                            # раз: следующий ход человека соберёт свои
                            attachments = session.pending_attachments
                            session.pending_attachments = []

                            if current_message.strip() or attachments:
                                post = session.add_post(
                                    display_name=participant.get("display_name", ""),
                                    model_used="human",
                                    content=current_message,
                                    round_num=round_num,
                                    search_count=0,
                                    search_queries=[],
                                    is_moderator=participant.get("is_moderator", False),
                                    is_judge=participant.get("is_judge", False),
                                    gender=participant.get("gender", "male"),
                                    attachments=attachments,
                                    # Своё слово в переигранном спектакле человек
                                    # говорит заново: в ДАМПе оно встаёт тем же
                                    # номером, и отметка идёт до него
                                    replay=bool(replay),
                                )
                                print(f"🎬 {participant.get('display_name', '')}: {current_message[:50]}")
                                if on_post:
                                    on_post(post)
                            else:
                                print(f"🎬 {participant.get('display_name', '')} пропустил действие")
                            
                            session.moderator_message = None
                            session.current_action = None
                            break
                    
                    if session.moderator_finished or again_done:
                        break
                else:
                    # В памяти оставляем только того, кто сейчас говорит
                    # (модель могла быть заменена режиссёром на ходу)
                    ollama_api.unload_other_show_models(participant.get("model", ""), show_model_names())
                    
                    session.current_action = "thinking"
                    
                    session.handle_ai_turn(participant, round_num, on_draft=on_draft,
                                           replay=bool(replay))
                    
                    post = session.posts[-1] if session.posts else None
                    if post and on_post:
                        on_post(post)
                    
                    session.current_action = None
                    time.sleep(0.5)
            
            if session.moderator_finished or again_done:
                break
    except Exception as e:
        # Не даём потоку умереть тихо: иначе интерфейс навсегда остаётся
        # в состоянии "идёт спектакль", а кнопки перестают реагировать.
        print(f"❌ Спектакль прерван из-за ошибки: {e}")
        traceback.print_exc()
    finally:
        # Заказ, пришедший к самому концу спектакля, не теряется: делать его
        # больше некому, а лента свободна (см. session.take_order)
        if session.feed_order is not None:
            serve_order()
        # Заказанная на ходу переигровка: занавес не опускается — сказанное
        # играется заново, и уже другим потоком (см. session.order_feed).
        # Отложенное тогда нужно ей: возвращать его сейчас — значит задвоить ленту
        next_plan = session.take_replay_next()
        # Отложенное возвращается в ленту — и при ошибке тоже: переигранное уже
        # заняло свои номера, а всё, чего переигровка не коснулась, спектаклю
        # терять незачем (см. return_held)
        if replay and not next_plan:
            back = session.return_held(replay.get("held") or {})
            print(f"  ↩️  Переигровка кончена, прежних реплик вернулось: {back}")
            # Вернувшееся возвращается и зрителю: страница узнаёт о репликах
            # событиями (см. publish_post), а об отложенном не сообщает никто —
            # и вернувшиеся реплики ждали бы перезагрузки страницы: рассылка
            # состояния идёт без реплик, а опрос к занавесу останавливается
            # (см. applyStatus)
            if back and on_post:
                for post in session.posts[-back:]:
                    on_post(post)
        session.current_participant = None
        session.current_action = None
        if next_plan:
            # Переигровку заказали на ходу: спектакль не опускает занавес,
            # а играет сказанное заново (см. session.order_feed). Лента
            # откладывается здесь же — заказ приходит планом, а не обрывом
            # (см. replay_show), и первым делом с ней прощается нынешний круг
            print("  🔁 Переигровка на ходу: спектакль играет заново, не останавливаясь")
            session.replay_show(next_plan)
            next_plan["held"] = merged_held(replay.get("held") if replay else None,
                                           next_plan.get("held"))
            thread = threading.Thread(target=run_debate_thread,
                                      args=(session.topic or topic,),
                                      kwargs={"on_post": on_post, "on_draft": on_draft,
                                              "replay": next_plan})
            thread.daemon = True
            thread.start()
        else:
            # Всегда завершаем спектакль явно, при любом выходе из цикла - в том
            # числе при ошибке, чтобы клиент разблокировал кнопки.
            session.finished = True
            session.running = False
            # Занавес — и итог словами: это первое, что спрашивают у отыгранного
            # спектакля. Страница показывает то же самое (см. show_report)
            report = session.show_report()
            print(f"  🎭 Итог: {report_words(report)}")
            by_acts = report_acts_words(report)
            if by_acts:
                print(f"  💰 по актам: {by_acts}")
            
            if settings.UNLOAD_AFTER_DEBATE:
                for model in show_model_names():
                    ollama_api.unload_model(model)
