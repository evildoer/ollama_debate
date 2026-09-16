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
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import cloud
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
    """Забыть сохранённый пульт, оставив тему.

    Нужен полному сбросу: если файл оставить как есть, следующий запуск вернул бы
    то, от чего режиссёр только что отказался. Удалить файл целиком нельзя —
    вместе с ним ушла бы тема, а её не сбрасывает даже полный сброс (см.
    reset_to_defaults): тема — про сюжет, а не про труппу, и в settings.py её нет.
    Поэтому в файле остаётся одна тема, а состав и правила следующего запуска
    соберутся заново из settings.py — ровно как при первом запуске.
    """
    try:
        settings.SETTINGS_FILE.write_text(
            json.dumps({"version": 2, "topic": session.topic or ""},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError as e:
        print(f"  ⚠️  Не сохраняется {settings.SETTINGS_FILE.name}: {e}")


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
    """Когда событие началось и когда кончилось — одной строкой."""
    start = clock(step.get("t"))
    end = clock(step.get("t_end")) if step.get("t_end") else ""
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
            weight += f" · наш счёт ≈{numbers_word(estimate)} токенов"
    else:
        weight = f"ввод {numbers_word(step.get('tokens_in'))}"
        if estimate:
            weight += f" (наш счёт ≈{numbers_word(estimate)})"
        weight += f" · вывод {numbers_word(step.get('tokens_out'))} токенов"
    parts = [weight]
    if step.get("reasoning_tokens"):
        parts.append(f"из них размышлений {numbers_word(step['reasoning_tokens'])}")
    if step.get("finish_reason"):
        # Код ответа без перевода читается как код: рядом с ним — его смысл
        meaning = finish_meaning(step["finish_reason"])
        parts.append(f"конец: {step['finish_reason']}" + (f" ({meaning})" if meaning else ""))
    parts.append("инструмент поиска отправлен" if step.get("tools")
                 else "без инструмента поиска")
    return ", ".join(parts)


# Каким значком помечать строку хронологии: шаг — он и есть шаг, а вот заминки
# и отказы должны отличаться от обычного запроса с первого взгляда
STEP_MARKS = {"refused": "⛔", "force": "🔍", "silence": "⚠️", "note": "·",
              "money": "💰"}


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
    available = ("без предела (CLOUD_NUM_CTX = 0)" if budget.get("available") is None
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
        line += (f". Обрезка выбросила {summary.get('removed_messages')} сообщ. "
                 f"({numbers_word(summary.get('removed_tokens'))} токенов) — "
                 f"самое раннее перечислено ниже")
    else:
        line += ". Обрезка ничего не тронула: сцена влезла в окно целиком"
    return line + "\n"


def dump_turn_header(post_id: int, turn: dict) -> str:
    """Начало записи о ходе: кто говорит, чьим окном мерено, что вошло в запрос.

    Пишется ДО запроса к модели (см. open_dump_turn): у хода, который оборвался
    на середине, в файле должно остаться начало, а не пустота. Раздела
    «дописано ходом» здесь нет нарочно: дописанное — это результаты поисков
    и напоминания, и все они видны в хронологии со своим весом. В двух местах
    одни и те же данные — это не полнота, а каша (см. build_turn_report).
    """
    who = turn.get("who") or {}
    budget = turn.get("budget") or {}
    summary = turn.get("summary") or {}
    out = [f"\n## {post_id} · {who.get('time')} · {who.get('name')} · "
           f"{who.get('model')} · {who.get('role_name') or who.get('role')} "
           f"· Акт {who.get('round')}\n"]
    out.append(f"**Кто:** {who.get('name')} · {who.get('model')} · "
               f"{who.get('role_name') or who.get('role')} · {who.get('time')}\n")
    out.append(window_line(budget))
    out.append(history_line(summary, budget))
    out.append("### Что вошло в запрос к модели\n")
    for index, message in enumerate(turn.get("messages") or [], 1):
        out.append(message_line(index, message))
        if message.get("content"):
            out.append(quoted(message.get("content")))
    if summary.get("removed_messages"):
        out.append("\n**Что выбросила обрезка (самое раннее):**")
        for gone in turn.get("removed") or []:
            out.append(f"- {gone.get('speaker')} · {numbers_word(gone.get('tokens'))} токенов · "
                       f"{gone.get('preview')}")
    out.append("\n### Хронология\n")
    return "\n".join(out) + "\n"


def message_line(index: int, message: dict) -> str:
    """Одно сообщение запроса одной строкой — с объяснением, если текста нет.

    Ноль токенов у сообщения — не пустое место, а сигнал: по протоколу OpenAI
    просьба о поиске приходит пустым текстом плюс полем `tool_calls`. Поэтому
    у такого сообщения названы и его поля со значениями, и что они значат.
    """
    weight = (f"{numbers_word(message.get('tokens'))} токенов"
              if int(message.get("tokens") or 0) else "0 токенов текста")
    line = (f"- №{index} · {message.get('role')} · "
            f"{message.get('name') or '—'} · {weight}")
    if message.get("note"):
        line += f"\n  - {message['note']}"
    return line


def dump_turn_tail(post: dict, turn: dict) -> str:
    """Конец записи о ходе: что дописало приложение, чем ход кончился, сколько шёл."""
    out = []
    summary = turn.get("summary") or {}
    if summary.get("seconds") is not None:
        out.append(f"\n**Ход длился:** {duration_words(summary['seconds'])}"
                   f" — от начала обрезки истории до готовой реплики\n")
    if turn.get("added"):
        out.append("\n### Что приложение дописало в запрос по ходу дела\n")
        out.append("Полных текстов здесь нет нарочно: найденное стоит в хронологии "
                   "выше, вместе с формулировкой запроса и своим весом.\n")
        for index, message in enumerate(turn["added"], 1):
            out.append(message_line(index, message))
    if post.get("sketch"):
        out.append("\n### Сказано раньше — прежняя версия реплики\n")
        out.append(quoted(post["sketch"]))
    out.append("\n### Реплика\n")
    out.append(quoted(post.get("content")))
    return "\n".join(out) + "\n"


def dump_human_markdown(post: dict) -> str:
    """Ход живого участника: отправлять никуда нечего, но в хронологии он есть."""
    return (f"\n## {post.get('id')} · {post.get('timestamp')} · {post.get('display_name')} "
            f"· {post.get('model_used')} · {post.get('role_name')} "
            f"· Акт {post.get('round')}\n\n"
            f"Реплика человека: никуда не отправлялась, ни токенов, ни поиска.\n\n"
            + quoted(post.get("content")) + "\n")


# Открытый файл ДАМПа и событие, которое сейчас дописывается: держим их между
# вызовами, потому что ход пишется по частям — от запроса к запросу и от куска
# размышлений к куску (см. dump_step_sink)
_DUMP = {"handle": None, "thought": None}


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
            with open(settings.DUMP_FILE, "a", encoding="utf-8") as one:
                one.write(text)
            return
        handle.write(text)
        if force:
            handle.flush()
    except Exception as e:
        print(f"  ⚠️  Не сохраняется ход в {settings.DUMP_FILE.name}: {e}")


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
        if piece:
            # Без force=False: файл читают глазами прямо во время хода
            dump_write(piece)
        return
    if _DUMP.get("thought") is not None:
        # Размышления кончились: отделяем их от следующего события — и тут же
        # исправляем их строку, потому что итоговый вес и время окончания
        # стали известны только сейчас (см. dump_fix_step)
        finished = _DUMP["thought"]
        _DUMP["thought"] = None
        dump_write("\n")
        dump_fix_step(finished)
    dump_write(step_markdown(step) + "\n")


def dump_fix_step(step: dict) -> None:
    """Исправить в ДАМПе строку события, у которого появились итоговые числа.

    Живое письмо и правка тут не спорят. Строка запроса появляется в файле
    в тот миг, когда запрос ушёл, — тогда у неё нет ни времени окончания, ни
    чисел вендора: ждать с записью нельзя, иначе у оборванного хода не осталось
    бы следа. А показывать устаревшее — врать: «1 токенов» у мыслей, текст
    которых на семьсот токенов, читается как ошибка. Поэтому, когда итог
    известен, файл переписывает ту же строку: метка начала у каждого шага своя,
    по ней строка и находится.
    """
    if step.get("kind") not in ("ask", "thought"):
        return
    if step.get("kind") == "thought" and not step.get("t_end"):
        # Мысль, пришедшая одним куском, до сих пор оставалась без времени
        # окончания: дописывать её больше некому, и этот миг и есть конец
        step["t_end"] = time.time()
    when = clock(step.get("t"))
    if not when:
        return
    marker = f"- {when} ·"
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
    for index, line in enumerate(lines):
        if not line.startswith(marker):
            continue
        lines[index] = step_markdown(step, with_text=False) + "\n"
        temp = path.with_name(path.name + ".fix")
        try:
            temp.write_text("".join(lines), encoding="utf-8")
            temp.replace(path)
        except Exception as e:
            print(f"  ⚠️  Не исправляется строка в {path.name}: {e}")
        break
    if was_open:
        dump_reopen()


def dump_reopen() -> None:
    """Открыть ДАМП дальше на дописывание — после того как строка исправлена."""
    try:
        _DUMP["handle"] = open(settings.DUMP_FILE, "a", encoding="utf-8")
    except Exception as e:
        _DUMP["handle"] = None
        print(f"  ⚠️  Не открывается {settings.DUMP_FILE.name}: {e}")


def open_dump_turn(post_id: int, turn: dict, report: dict) -> None:
    """Начать запись о ходе и отдать журналу «перо»: события пишутся сразу.

    report["sink"] — это и есть перо (см. cloud.journal_push): каждое событие
    хода улетает в файл в тот момент, когда случилось. Файл открыт всё время
    хода, поэтому и оборванный ход остаётся в ДАМПе.
    """
    _DUMP["thought"] = None
    dump_reopen()
    dump_write(dump_turn_header(post_id, turn))
    if report is not None:
        report["sink"] = dump_step_sink
        # Перо пишет событие сразу, а «правка» доводит его строку, когда числа
        # наконец известны (см. dump_fix_step): у запроса это время окончания
        # и числа вендора, у размышлений — итоговый вес
        report["fix"] = dump_fix_step


def close_dump_turn(post: dict, turn: dict) -> None:
    """Закончить запись о ходе: реплика, прежняя её версия — и закрыть файл."""
    if _DUMP.get("thought") is not None:
        # Мысль оказалась последним событием хода: её строка тоже ждёт итога
        finished = _DUMP["thought"]
        _DUMP["thought"] = None
        dump_write("\n")
        dump_fix_step(finished)
    dump_write(dump_turn_tail(post, turn))
    handle = _DUMP.get("handle")
    _DUMP["handle"] = None
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


def start_dump(topic: str) -> None:
    """Начать ДАМП спектакля — заново, а не дописать к прежнему.

    Файл переписывается целиком на каждом спектакле: ДАМП — про последний
    спектакль, а не летопись. Иначе он рос бы вечно, а читают его всегда ради
    разбора свежего случая — так и было со стенограммой, которая никого не
    чистилась и после десятков спектаклей стала мегабайтами.
    """
    close_dump_turn({}, {})     # на всякий случай: прошлый файл больше не наш
    when = time.strftime("%d.%m.%Y %H:%M")
    header = (
        f"# ДАМП · {when} · {(topic or '').strip() or 'без темы'}\n\n"
        f"Хронология последнего спектакля: что вошло в каждый запрос к модели,\n"
        f"что она попросила, что ей принесли и что она сказала — со временем\n"
        f"и числами токенов.\n"
        f"Пишется по ходу дела, заново на каждый новый спектакль "
        f"(порт {settings.PORT}).\n\n"
        f"Как читать. **ввод** — сколько токенов было в этом запросе к модели;\n"
        f"**вывод** — сколько она вернула (размышления считаются выводом,\n"
        f"но в реплику не попадают); **наш счёт** — то, что театр посчитал сам\n"
        f"(tiktoken — тот же счётчик, каким мерится история), а число рядом —\n"
        f"счёт вендора, и они расходятся, потому что токенизаторы разные;\n"
        f"**вес найденного** — сколько токенов принёс поиск (они едут сверх\n"
        f"истории и оплачиваются тоже).\n"
    )
    try:
        settings.DUMP_FILE.write_text(header, encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Не открывается {settings.DUMP_FILE.name}: {e}")


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
_MESSAGE_FIELDS = ("tool_calls", "function_call", "tool_name", "tool_call_id")


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
        else:
            found.append(f"`{key}` = {value}")
    return found


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
        return ""
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
        "note": message_note(msg),
    }


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


def _steps_count(steps, kind: str) -> int:
    """Сколько в ходу было событий такого рода — по журналу, а не по догадке."""
    return sum(1 for step in (steps or []) if step.get("kind") == kind)


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
    turn["added"] = extra


def _step_view(step: dict) -> dict:
    """Событие хода в том виде, в каком его показывает страница.

    Время — словами: в JSON метка времени нечитаема, а в хронологии важно именно
    «когда» (см. clock). Технический признак «запись ещё открыта» наружу тоже
    не идёт — страница показывает закрытые записи.
    """
    view = {key: value for key, value in step.items() if key != "open"}
    view["clock"] = clock(step.get("t"))
    view["clock_end"] = clock(step.get("t_end")) if step.get("t_end") else ""
    return view


# Как называть роль по-русски: и в ленте, и в ДАМПе (см. dump_turn_header)
ROLE_NAMES = {
    "participant": "Участник",
    "moderator": "Модератор",
    "judge": "Судья",
}


def create_post(display_name: str, model_used: str, content: str, round_num: int, 
                avatar_url: str = None, avatar_emoji: str = None,
                search_count: int = 0, search_queries: list = None,
                role: str = "participant", gender: str = "male",
                thinking: str = "", sketch: str = "", turn: dict = None) -> dict:
    """Единая функция создания поста для любого участника (human или AI)"""
    if search_queries is None:
        search_queries = []
    
    # Определяем роль и соответствующую иконку
    role_icons = {
        "participant": "🎭",
        "moderator": "🎬",
        "judge": "⚖️"
    }
    
    role_names = ROLE_NAMES
    
    return {
        "id": len(session.posts) + 1,
        "display_name": display_name,
        "model_used": model_used,
        "avatar_url": avatar_url,
        "avatar_emoji": avatar_emoji or role_icons.get(role, "📣"),
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
        # Счёт за спектакль начинается заново: это про этот вечер, а не про
        # все, что были (см. _note_money)
        self.spent = 0.0
        self.money_told = False
        # Новый спектакль — новые реплики: отчёты прежних ходов к ним не подходят
        self.turn_log = {}
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
                 sketch="", turn=None, dump=True):
        """Создать пост.

        dump=False — ход уже писался в ДАМП по ходу дела (см. open_dump_turn):
        дописывать его целиком второй раз — значит удвоить ход в файле.
        """
        avatar_url = self.avatars.get(display_name)
        avatar_emoji = self.avatar_emojis.get(display_name, "📣")

        role = role_of(is_moderator, is_judge)

        post = create_post(display_name, model_used, content, round_num,
                          avatar_url, avatar_emoji, search_count, search_queries, role,
                          gender, thinking, sketch, turn)
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
        # бы чужой черновик мыслей как сказанное вслух), а в ДАМП — идут: там
        # хронология спектакля, и её читают после занавеса, а не посреди разговора
        if dump:
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

    def handle_ai_turn(self, participant: dict, round_num: int, on_draft=None) -> tuple:
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
    # Хронология ходов уйдёт в ДАМП: после занавеса ленты уже не будет,
    # а по ДАМПу можно разобрать любой ход — от промпта до токенов
    start_dump(topic)
    print(f"  🗒  ДАМП спектакля: {settings.DUMP_FILE} (пишется заново на каждый спектакль)")
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
