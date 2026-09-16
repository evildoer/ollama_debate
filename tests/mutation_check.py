"""
Проверка самих тестов: ловят ли они настоящие баги.

Зачем это нужно. Тест, который не падает ни при каком баге, ничего не охраняет,
но при этом создаёт ложное чувство защиты. Здесь мы по одному возвращаем в код
ошибки, которые раньше искали руками, и смотрим, кто из тестов их поймает.

Приложение разбито на модули (пакет aitheatre/), поэтому у каждого бага указан
файл, в котором он живёт: раньше баг был просто «в файле», теперь — в конкретном
месте. Копируется весь проект целиком (приложение, тесты, static/), правки
вносятся в копию, рабочие файлы не меняются. Ollama и спектакль не запускаются.

Запуск из папки проекта:

    venv/Scripts/python.exe tests/mutation_check.py

А если правка затронула только одно место, незачем ждать весь набор: слова
в аргументах — это часть имени бага, и подбрасываются только совпавшие

    venv/Scripts/python.exe tests/mutation_check.py молчание

По умолчанию баги подбрасываются ВРАЗУБЕДИТЕЛЬНО: каждый — в своём процессе,
сразу несколько за раз, и прогон копии останавливается на первом же упавшем
тесте. Для вопроса «ловит ли набор эти баги» этого хватает: упал хоть один —
ловят. Ждать сорок прогонов по сорок секунд подряд было незачем.

    venv/Scripts/python.exe tests/mutation_check.py --full

— это прежний дотошный порядок: по одному, без остановки на первом падении,
с полным списком поймавших тестов у каждого бага. Нужен, когда важно не «ловит
ли», а «кто именно ловит»: например, чтобы убрать задвоенную проверку.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Отчёту нужны те же эмодзи (✅/❌), что и приложению, а русская консоль Windows
# работает в cp1251 и роняет печать с UnicodeEncodeError. Приложение себя так
# защищает при импорте; здесь импорта приложения нет, поэтому — тот же приём.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Метка вердикта в выводе процесса-исполнителя: по ней общий прогон и узнаёт,
# чем кончился подброшенный баг (см. run_together). Без метки вердикта в выводе
# нет — значит процесс кончился не отчётом, и это надо назвать вслух
VERDICT_MARK = "@@ВЕРДИКТ@@"

# Что скопировать в проверочную папку: приложение, тесты, точка входа и то,
# на что тесты смотрят (клиент Socket.IO, рендерер формул, иконка). Точка входа
# тоже нужна: иначе из копии нельзя проверить, что она ведёт в то же приложение.
COPY_DIRS = ("aitheatre", "tests", "static")
COPY_FILES = ("favicon.ico", "ollama_debate_web.py")

# Заголовок бага -> список правок: (файл, что заменить, на что заменить).
# Каждый заголовок — реальная ошибка из истории проекта.
#
# И каждая заготовка — именно поломка, а не переименование. Правка, меняющая
# только надпись (синоним вместо синонима: «вход» вместо «ввода», «на глаз»
# вместо «наш счёт»), поведения не меняет: тест на неё однажды покраснеет
# и не расскажет ни о каком баге — а набор от таких заготовок только пухнет.
# Выбор слов живёт в README и в комментариях; здесь — то, что ломает поведение.
BUGS = {
    "ссылку на настройку оставили без имени модуля": [
        ("aitheatre/ollama_api.py",
         '"options": options or settings.OPTIONS,',
         '"options": options or OPTIONS,'),
    ],
    "формулы снова съедает markdown": [
        ("aitheatre/text.py",
         "return MATH_SEGMENT_RE.sub(stash, text), stashed",
         "return text, stashed"),
    ],
    "судью снова закрепили за одним характером": [
        ("aitheatre/show.py",
         "        character = draw_character()",
         '        character = "pedant" if template.get("is_judge") '
         "else draw_character()"),
    ],
    "место в составе снова опознаётся по номеру, а не по себе": [
        ("aitheatre/show.py",
         '        entry = by_id.get(str(raw.get("cast_id") or ""))',
         "        _index = incoming.index(raw)\n"
         "        entry = cast[_index] if _index < len(cast) else None"),
    ],
    "роль на месте ставят, не снимая прежнюю": [
        ("aitheatre/show.py",
         '    participant["is_moderator"] = role == "moderator"\n'
         '    participant["is_judge"] = role == "judge"',
         '    if role == "moderator":\n'
         '        participant["is_moderator"] = True\n'
         '    if role == "judge":\n'
         '        participant["is_judge"] = True'),
    ],
    "убранный участник всё равно выходит на сцену": [
        ("aitheatre/show.py",
         "                if not still_in_cast(participant, session.runtime_participants):",
         "                if False:"),
    ],
    "«Новый спектакль» забывает сцену": [
        ("aitheatre/show.py",
         "        scene = [dict(place) for place in self.scene] if self.scene else None",
         "        scene = None"),
    ],
    "сцена не возвращается после перезапуска": [
        ("aitheatre/show.py",
         '            scene = sanitize_scene(data.get("scene"))',
         "            scene = []"),
    ],
    "чужая роль из файла становится ролью на сцене": [
        ("aitheatre/show.py",
         '                 "role": role if role in CAST_ROLES else "participant"}',
         '                 "role": role}'),
    ],
    "правила судьи остаются у того, кто судьёй быть перестал": [
        ("aitheatre/show.py",
         '            elif was_role == "judge" \\\n'
         '                    and own.strip() == settings.DEFAULT_JUDGE_INSTRUCTION.strip():',
         "            elif False:"),
    ],
    "судья снова видит все прошлые акты, хотя опция говорит иное": [
        ("aitheatre/show.py",
         '                        and round_num is not None and post.get("round") != round_num:',
         "                        and False:"),
    ],
    "слово судьи снова слышит только он сам": [
        ("aitheatre/show.py",
         '                if is_judge and post.get("display_name") != viewer_name \\\n'
         '                        and post.get("display_name") not in public_judges:',
         '                if is_judge and post.get("display_name") != viewer_name:'),
    ],
    "чужое значение опции роли проходит как своё": [
        ("aitheatre/show.py",
         "                if value not in allowed:",
         "                if False:"),
    ],
    "опции прежней роли остаются у места с новой ролью": [
        ("aitheatre/show.py",
         '    if not ROLE_OPTIONS.get(role):\n'
         '        entry.pop("role_options", None)\n'
         "        return",
         "    if not ROLE_OPTIONS.get(role):\n"
         "        return"),
    ],
    "облачная модель снова ищется в Ollama": [
        ("aitheatre/ollama_api.py",
         '    local = [m for m in required if not cloud.is_cloud_model(m)]\n'
         '    remote = [m for m in required if cloud.is_cloud_model(m)]',
         '    local = list(required)\n'
         "    remote = []"),
    ],
    "ключ шлюза уехал в текст ошибки, а тот — в ленту": [
        ("aitheatre/cloud.py",
         "            return None, GatewayError(hide_key(_error_text(e.code, e.reason, detail)), e.code)",
         "            return None, GatewayError(_error_text(e.code, e.reason, detail), e.code)"),
    ],
    "русское имя отправителя снова уезжает на шлюз": [
        ("aitheatre/cloud.py",
         '"messages": openai_messages(messages, keep_names=send_message_names()),',
         '"messages": openai_messages(messages, keep_names=True),'),
    ],
    "облачный круг поиска снова без идентификаторов вызовов": [
        ("aitheatre/cloud.py",
         '            if calls:\n'
         '                item["tool_calls"] = calls',
         '            if False:\n'
         '                item["tool_calls"] = calls'),
    ],
    "строку из .env, переехавшую в settings.py, снова применяют молча": [
        ("aitheatre/cloud.py",
         "    for name in CLOUD_MOVED_TO_SETTINGS:\n        if _mentions(key, name):\n",
         "    for name in CLOUD_MOVED_TO_SETTINGS:\n        if _mentions(key, name):\n"
         "            os.environ.setdefault(name, raw_value)\n"),
    ],
    "в подсказке о переезде снова стоит единица вместо True": [
        ("aitheatre/cloud.py",
         '    if value.lower() in ("1", "true", "yes", "on", "да", "вкл"):\n'
         '        return "True"',
         '    if value.lower() in ("1", "true", "yes", "on", "да", "вкл"):\n'
         '        return value'),
    ],
    "про неограниченное окно снова молчат, хотя за него платят": [
        ("aitheatre/text.py",
         "        _warn_about_paying_for_the_whole_scene(total_tokens)\n",
         ""),
    ],
    "отказ модели от инструмента поиска снова не запоминается": [
        ("aitheatre/cloud.py",
         "    if not error and tools_stripped:",
         "    if not error and tools_stripped and False:"),
    ],
    "тёмная сцена снова не красит карточку инструкции": [
        ("aitheatre/page.py",
         "        body.dark .instr-card { background: #141414; }\n",
         ""),
    ],
    "карточка места в тонкой настройке снова белая на тёмной сцене": [
        ("aitheatre/page.py",
         "                + '<div class=\"index-card\" data-participant-index=\"' + idx + '\" style=\"border:1px solid '",
         "                + '<div data-participant-index=\"' + idx + '\" style=\"border:1px solid '"),
    ],
    "поток шлюза снова читается как обычный ответ": [
        ("aitheatre/cloud.py",
         "            if streaming:\n"
         "                return _read_stream(response, on_delta, on_thought, deadline)",
         "            if False:\n"
         "                return _read_stream(response, on_delta, on_thought, deadline)"),
    ],
    "предел времени на ход снова не соблюдается": [
        ("aitheatre/cloud.py",
         "        if not whole and deadline is not None and time.monotonic() >= deadline:",
         "        if False:"),
    ],
    "числа характера снова уезжают за диапазон вендора": [
        ("aitheatre/cloud.py",
         "        result[openai_key] = _within_cloud_limits(openai_key, value, model)",
         "        result[openai_key] = value"),
    ],
    "поле, которого вендор не знает, снова не убирается": [
        ("aitheatre/cloud.py",
         "    named = named_field(text)",
         '    named = ""'),
    ],
    "убранное поле снова уезжает на следующем ходу": [
        ("aitheatre/cloud.py",
         "    for field in dropped_params(model):\n        payload.pop(field, None)",
         "    for field in ():\n        payload.pop(field, None)"),
    ],
    # Из жизни: два экземпляра садились на порт 5000 молча, браузер открывал
    # старый — и казалось, что театр «помнит» старый спектакль
    "занятый порт снова выглядит свободным": [
        ("aitheatre/web.py",
         '        probe.bind(("0.0.0.0", int(port)))',
         '        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n'
         '        probe.bind(("0.0.0.0", int(port)))'),
    ],
    "о занятом порте снова говорят без номеров процессов": [
        ("aitheatre/web.py",
         '        lines.extend(f"      taskkill /PID {pid} /F" for pid in holders)',
         '        lines.append("      taskkill /PID <номер> /F")'),
    ],
    "порт снова ищется по вхождению в адрес": [
        ("aitheatre/web.py",
         '        if not parts[1].endswith(marker) or parts[3].upper() != "LISTENING":',
         '        if marker not in parts[1] or parts[3].upper() != "LISTENING":'),
    ],
    "порт из командной строки снова не читается": [
        ("aitheatre/web.py",
         "    port = port_from_argv(sys.argv[1:])",
         "    port = int(settings.PORT)"),
    ],
    "у экземпляров снова одна общая папка": [
        ("aitheatre/settings.py",
         "    return INSTANCE_ROOT / f\"port-{int(PORT if port is None else port)}\"",
         "    return INSTANCE_ROOT"),
    ],
    "временные файлы снова лежат в корне проекта": [
        ("aitheatre/settings.py",
         'INSTANCE_ROOT = PROJECT_ROOT / ".theatre"',
         "INSTANCE_ROOT = PROJECT_ROOT"),
    ],
    "сцена снова читается до выбора папки экземпляра": [
        ("aitheatre/show.py",
         "# Глобальный экземпляр сессии\nsession = DebateSession()",
         "load_theatre_settings()\n\n# Глобальный экземпляр сессии\nsession = DebateSession()"),
    ],
    "поиск снова требуют после ответа, а не до него": [
        ("aitheatre/ollama_api.py",
         "        require_search = force_tool_use or (\n"
         "            iteration == 0 and settings.SEARCH_BEFORE_REPLY\n"
         "            and search_count < min_searches and takes_tools_now(model))",
         "        require_search = force_tool_use"),
    ],
    "поиск снова просят у модели, которая инструмент не принимает": [
        ("aitheatre/ollama_api.py",
         "                    and takes_tools_now(model)):",
         "                    and MODELS_TOOLS_SUPPORT.get(model, False)):"),
    ],
    "текст ошибки снова считают сказанной репликой": [
        ("aitheatre/ollama_api.py",
         "        if not tool_calls and is_answer(content):",
         "        if not tool_calls and content and content.strip():"),
    ],
    # Из жизни: модель искала охотно, а слов не сказала ни одного — и ход
    # кончался «Модель не дала ответ», хотя искать было уже нечего
    "модель снова молчит после поиска без просьбы сказать реплику": [
        ("aitheatre/ollama_api.py",
         "            if ((content or \"\").strip() == \"\" and search_count > 0\n"
         "                    and forced_attempts < max_forced_attempts):",
         "            if False:"),
    ],
    "просьба сказать реплику снова привязана к MIN_SEARCHES": [
        ("aitheatre/ollama_api.py",
         "            if ((content or \"\").strip() == \"\" and search_count > 0\n"
         "                    and forced_attempts < max_forced_attempts):",
         "            if ((content or \"\").strip() == \"\" and search_count > 0\n"
         "                    and search_count < min_searches\n"
         "                    and forced_attempts < max_forced_attempts):"),
    ],
    "молчащую модель снова просят без счёта попыток": [
        ("aitheatre/ollama_api.py",
         "                    and forced_attempts < max_forced_attempts):",
         "                    and forced_attempts >= 0):"),
    ],
    "мысли хода снова теряются вместе с черновиком": [
        ("aitheatre/show.py",
         '            thinking=draft.thinking_full() if draft else "",',
         '            thinking="",'),
    ],
    "сметённая реплика снова пропадает из ленты": [
        ("aitheatre/show.py",
         "        if replace:\n            self.keep_sketch()",
         "        if replace:\n            self.sketch = \"\""),
    ],
    "набросок снова не доходит до готового поста": [
        ("aitheatre/show.py",
         '            sketch=draft.sketch if draft else "",',
         '            sketch="",'),
    ],
    "черновик реплики снова идёт без разметки": [
        ("aitheatre/page.py",
         "postText.innerHTML = draft.content_html || escapeHtml(draft.content || '');",
         "postText.textContent = draft.content || '';"),
    ],
    "блок наброска снова перекрывает блок мыслей": [
        ("aitheatre/page.py",
         "element.querySelector('.post-thinking:not(.post-sketch)')",
         "element.querySelector('.post-thinking')"),
    ],
    "состояние снова не рассылается по сокету": [
        ("aitheatre/web.py",
         "    socketio.emit('status_update', status_payload(with_posts=False))",
         "    return"),
    ],
    "состояние по сокету и опросу снова расходятся": [
        ("aitheatre/web.py",
         "    response = jsonify(status_payload(last_post_count))",
         "    _only_by_poll = status_payload(last_post_count)\n"
         "    _only_by_poll['только_в_опросе'] = True\n"
         "    response = jsonify(_only_by_poll)"),
    ],
    "счётчик реплик снова перескакивает мимо несказанного поста": [
        ("aitheatre/page.py",
         "            if (data.posts_included && typeof data.total_posts === 'number') {",
         "            if (typeof data.total_posts === 'number') {"),
    ],
    "опрос снова частый, когда сокет жив": [
        ("aitheatre/page.py",
         "                                       socket && socket.connected ? POLL_FALLBACK_MS : POLL_MS);",
         "                                       POLL_MS);"),
    ],
    "пульт снова раскрыт во время спектакля": [
        ("aitheatre/page.py",
         "            setAllSectionsCollapsed(phase !== 'setup');",
         "            setAllSectionsCollapsed(false);"),
    ],
    "раздел «Ваша реплика» снова захлопнут на своём ходу": [
        ("aitheatre/page.py",
         "            setSectionCollapsed('turnSection', state !== 'your');",
         "            setSectionCollapsed('turnSection', true);"),
    ],
    "реплика из сокета снова приходит дважды": [
        ("aitheatre/page.py",
         "                if (post.id && post.id <= lastPostCount) return;",
         "                if (false) return;"),
    ],
    "части вызова инструмента снова читаются как готовый вызов": [
        ("aitheatre/cloud.py",
         '        if function.get("name"):\n            call["name"] += str(function["name"])',
         '        if function.get("name"):\n            call["name"] = str(function["name"])'),
    ],
    "новые модели OpenAI снова получают старое имя поля": [
        ("aitheatre/cloud.py",
         '    if "max_tokens" in body and (named == "max_tokens" or "max_completion_tokens" in text):',
         "    if False:"),
    ],
    "размышления модели снова читаются как обычный текст": [
        ("aitheatre/cloud.py",
         "    for key in _THINKING_KEYS:",
         "    for key in ():"),
    ],
    "мысли в ленте снова уезжают целиком": [
        ("aitheatre/show.py",
         "        if len(text) <= self.THOUGHT_SHOWN:",
         "        if True:"),
    ],
    "блок мыслей снова показывает саму реплику": [
        ("aitheatre/show.py",
         '                        "thinking": self.thought_tail(),',
         '                        "thinking": self.text,'),
    ],
    "черновик реплики снова не закрывается, когда ход сорвался": [
        ("aitheatre/show.py",
         "            if draft is not None:\n                draft.finish()",
         "            if draft is not None and draft.finished:\n                draft.finish()"),
    ],
    "молчащий шлюз снова отвечает сырым текстом ошибки": [
        ("aitheatre/cloud.py",
         "        except TimeoutError:\n"
         "            # Своё сообщение вместо «The read operation timed out»: по сырому\n"
         "            # тексту нельзя понять ни сколько ждали, ни что с этим делать\n"
         "            return None, GatewayError(\n"
         '                f"модель не ответила за {seconds} с: шлюз не прислал данные. "\n'
         '                f"«Думающим» и большим моделям нужно больше времени — "\n'
         '                f"увеличьте CLOUD_TIMEOUT в .env")\n',
         ""),
    ],
    "числа характеров снова уезжают на шлюз": [
        ("aitheatre/cloud.py",
         "    if send_params():",
         "    if True:"),
    ],
    "после 429 запрос не повторяется": [
        ("aitheatre/cloud.py",
         "            if e.code == 429 and attempt + 1 < attempts:",
         "            if False:"),
    ],
    "остальные 4xx повторяются и продлевают паузу по ключу": [
        ("aitheatre/cloud.py",
         "            return None, GatewayError(hide_key(_error_text(e.code, e.reason, detail)), e.code)",
         "            if attempt + 1 < attempts:\n"
         "                time.sleep(0)\n"
         "                continue\n"
         "            return None, GatewayError(hide_key(_error_text(e.code, e.reason, detail)), e.code)"),
    ],
    "у шлюза снова требуют поиск, которого он не получал": [
        ("aitheatre/ollama_api.py",
         "        MODELS_TOOLS_SUPPORT[model] = cloud.send_tools()",
         "        MODELS_TOOLS_SUPPORT[model] = True"),
    ],
    "пульт снова молчит про модель живого места": [
        ("aitheatre/page.py",
         "                entry.model = String(pick(`model-${idx}`, p.model) || '').trim();",
         "                entry.model = p.model === 'human' ? '' : String(pick(`model-${idx}`, p.model) || '').trim();"),
    ],
    "живое место снова нельзя вернуть из человека": [
        ("aitheatre/show.py",
         '            if u["model"]:\n                entry["model"] = u["model"]',
         '            if u["model"] and entry.get("model") != "human":\n                entry["model"] = u["model"]'),
    ],
    "префикс пульта уехал на шлюз вместо имени модели": [
        ("aitheatre/cloud.py",
         '    if name.startswith(settings.CLOUD_MODEL_PREFIX):\n'
         '        return name[len(settings.CLOUD_MODEL_PREFIX):]',
         "    if False:\n"
         "        return name"),
    ],
    "запрос к шлюзу снова ведётся через локальный прокси": [
        ("aitheatre/cloud.py",
         "    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))",
         "    opener = urllib.request.build_opener()"),
    ],
    "реплика снова уезжает в шлюз через локальный прокси": [
        ("aitheatre/cloud.py",
         "    # запрос, а часто и сломать. Поэтому свой открыватель вообще без прокси.\n"
         "    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))",
         "    # запрос, а часто и сломать. Поэтому свой открыватель вообще без прокси.\n"
         "    opener = urllib.request.build_opener()"),
    ],
    "значения из .env стали пустыми": [
        ("aitheatre/cloud.py",
         "        parsed[key] = value",
         '        parsed[key] = ""'),
    ],
    "поле модели снова строгий список, вписать облачную нельзя": [
        ("aitheatre/page.py",
         "                      + '<input type=\"text\" id=\"model-' + idx + '\" list=\"modelList\"'",
         "                      + '<select id=\"model-' + idx + '\" data-typing=\"no\"'"),
    ],
    "облачные модели пропали из списка в пульте": [
        ("aitheatre/page.py",
         "                cloudModels = data.cloud_models || [];",
         "                cloudModels = [];"),
    ],
    "одна пустая ошибка связи снова закрывает театр": [
        ("aitheatre/page.py",
         "                if (statusFailures < STATUS_FAILURES_BEFORE_CLOSED) return;",
         "                if (statusFailures < 999) return;"),
    ],
    "в разметку карточки подставляется имя, которого в странице нет": [
        ("aitheatre/page.py",
         "                const roleOptionsBlock = roleOptionKeys.length === 0 ? '' : ''",
         "                const unusedRoleOptionsBlock = roleOptionKeys.length === 0 ? '' : ''"),
    ],
    "новому месту не находится модель, когда соседа нет": [
        ("aitheatre/show.py",
         "    models, _error = ollama_api.fetch_ollama_models()",
         "    models, _error = None, None"),
    ],
    "пульт не отправляет опознавательное место": [
        ("aitheatre/page.py",
         "                    cast_id: p.cast_id || '',",
         "                    cast_id: '',"),
    ],
    "тема берётся только из поля формы": [
        ("aitheatre/web.py",
         '    topic = str(data.get("topic", "") or "").strip() or '
         '(show.session.topic or "").strip()',
         '    topic = str(data.get("topic", "") or "").strip()'),
    ],
    "top_k без проверки границ": [
        ("aitheatre/show.py",
         '            if key == "top_k" and number < 1:\n'
         '                # С нулём или отрицательным значением Ollama '
         'отклонит весь запрос\n'
         '                return f"{name}: «top_k» должен быть не меньше 1"',
         "            if False:\n"
         "                pass"),
    ],
    "характер судьи теряется при переименовании состава": [
        ("aitheatre/show.py",
         "            # Место уже играло: имя меняется вместе с историей для промптов,\n"
         "            # иначе модель считала бы прошлые реплики чужими\n"
         '            if u["name"] != entry.get("display_name"):',
         '            entry.pop("instruction", None)\n'
         '            if u["name"] != entry.get("display_name"):'),
    ],
    "имя и эмодзи выбираются без оглядки на занятые": [
        ("aitheatre/show.py",
         "    free = [value for value in dict.fromkeys(pool) if value not in used]",
         "    free = list(dict.fromkeys(pool))"),
    ],
    "пост не получает класс роли (полосы пропадают)": [
        ("aitheatre/page.py",
         "postDiv.className = `post post-role-${post.role || 'participant'}`",
         "postDiv.className = `post`"),
    ],
    "растущая реплика делается другим человеком, а не копией поста": [
        ("aitheatre/page.py",
         "element.className = `post post-role-${draft.role || 'participant'} streaming`;",
         "element.className = `post streaming`;"),
    ],
    "у роли нет своего цвета на тёмной сцене": [
        ("aitheatre/page.py",
         "        body.dark.role-marks .post.post-role-judge { border-left-color: #8f5cae; }\n",
         ""),
    ],
    "пульт снова теряет top_k, min_p и seed": [
        ("aitheatre/page.py",
         "        const PARAM_KEYS = ['temperature', 'top_p', 'top_k', 'min_p',\n"
         "                            'repeat_penalty', 'presence_penalty', "
         "'frequency_penalty', 'seed'];",
         "        const PARAM_KEYS = ['temperature', 'top_p', 'repeat_penalty', "
         "'presence_penalty', 'frequency_penalty'];"),
    ],
    "у участника пропало поле min_p": [
        ("aitheatre/page.py",
         "paramField('min_p', 'min_p', '0.01', 0, 1,",
         "paramField('hidden_min_p', 'min_p', '0.01', 0, 1,"),
    ],
    "страница снова тянет шрифты из интернета": [
        ("aitheatre/page.py",
         "        /* Внешних шрифтов здесь нет: вся страница рисуется системными Georgia и",
         "        @import url('https://fonts.googleapis.com/css2?family=Oswald:wght@400;700&display=swap');\n"
         "        /* Внешних шрифтов здесь нет: вся страница рисуется системными Georgia и"),
    ],
    "правила судьи не переживают «Новый спектакль»": [
        ("aitheatre/show.py",
         "        self.judge_rules = judge_rules",
         "        self.judge_rules = list(settings.DEFAULT_JUDGE_RULES)"),
    ],
    "новый характер оказался клоном «Педанта»": [
        ("aitheatre/settings.py",
         '        "params": {"temperature": 0.6, "top_p": 1.0, "min_p": 0.4, "top_k": 30,\n'
         '                   "repeat_penalty": 1.2, "presence_penalty": 0.4,\n'
         '                   "frequency_penalty": 0.3},',
         '        "params": {"temperature": 0.2, "top_p": 0.5, "repeat_penalty": 1.10,\n'
         '                   "presence_penalty": 0.05, "frequency_penalty": 0.05},'),
    ],
    "опечатка в имени параметра характера": [
        ("aitheatre/settings.py",
         '"min_p": 0.4, "top_k": 30', '"minp": 0.4, "top_k": 30'),
    ],
    "историю облачного участника снова режут олламовской меркой": [
        ("aitheatre/text.py",
         "    num_ctx, num_predict = settings.context_budget(model)",
         '    num_ctx, num_predict = settings.context_budget("")'),
    ],
    "облачную настройку снова читают из .env, а не из settings.py": [
        ("aitheatre/cloud.py",
         "    return bool(settings.ENABLE_SEARCH and settings.CLOUD_SEND_TOOLS)",
         "    return bool(settings.ENABLE_SEARCH and\n"
         '                (os.environ.get("CLOUD_SEND_TOOLS") or settings.CLOUD_SEND_TOOLS))'),
    ],
    "запас на ответ снова съедает узкое окно": [
        ("aitheatre/settings.py",
         "        return window, min(reserve, max(0, window - CONTEXT_SAFETY_MARGIN) // 2)",
         "        return window, reserve"),
    ],
    "говорящую модель снова не передают в обрезку истории": [
        ("aitheatre/show.py",
         "        trimmed, trim_report = text.trim_history_with_report(\n"
         "            history_messages, text.estimate_tokens(system_prompt),\n"
         "            model=participant.get(\"model\", \"\"))",
         "        trimmed, trim_report = text.trim_history_with_report(\n"
         "            history_messages, text.estimate_tokens(system_prompt),\n"
         '            model="")'),
    ],
    "просьбу о поиске, написанную текстом, снова отправляют в ленту как реплику": [
        ("aitheatre/ollama_api.py",
         "        content, text_queries = tooltext.take_calls(content)",
         "        content, text_queries = content, []"),
    ],
    "сырой вызов поиска снова уезжает в черновик ленты": [
        ("aitheatre/show.py",
         "        return tooltext.take_calls(self.text)[0]",
         "        return self.text"),
    ],
    "ответ инструмента снова шлют модели, не удержавшей протокол": [
        ("aitheatre/ollama_api.py",
         '        if all(tc.get("textual") for tc in tool_calls):',
         "        if False:"),
    ],
    "предел ответа облачной модели снова никуда не уходит": [
        ("aitheatre/cloud.py",
         "    if cap > 0 and cap_field not in dropped_params(model):",
         "    if False:"),
    ],
    "место под ответ у облака снова считают олламовским числом": [
        ("aitheatre/settings.py",
         "        reserve = max(0, int(CLOUD_MAX_TOKENS or 0))",
         "        reserve = (int(CLOUD_MAX_TOKENS or 0)\n"
         "                   or int(OPTIONS.get(\"num_predict\", 0) or 0))"),
    ],
    # Отказ по tool_choice — не повод отбирать у модели поиск: это разные
    # возможности, и судья на qwen остался без поиска именно из-за этой путаницы
    "отказ по tool_choice снова отбирает у модели сам инструмент": [
        ("aitheatre/cloud.py",
         "        return {key: value for key, value in body.items()\n"
         "                if key not in _TOOL_CHOICE_FIELDS}",
         "        return {key: value for key, value in body.items()\n"
         "                if key not in _TOOL_CHOICE_FIELDS and key != \"tools\"}"),
    ],
    "модели без инструмента снова запрещают искать": [
        ("aitheatre/show.py",
         '                f"{self.SEARCH_CALL_EXAMPLE} — приложение выполнит поиск "',
         '                ""'),
    ],
    # «assistant · 0 токенов» был верным сигналом и ни капли смысла: по протоколу
    # такой ход — пустой текст плюс поле tool_calls
    "пустое сообщение снова не объясняет, чем оно полно": [
        ("aitheatre/show.py",
         '        "note": message_note(msg),',
         '        "note": "",'),
    ],
    "код ответа вендора снова оставляют без перевода": [
        ("aitheatre/show.py",
         '        meaning = finish_meaning(step["finish_reason"])',
         '        meaning = ""'),
    ],
    # Цена хода — факт со счёта шлюза, а не оценка: тарифов мы не знаем,
    # а остаток шлюз отдаёт сам
    "цену хода снова считают по тарифам, которых мы не знаем": [
        ("aitheatre/cloud.py",
         "    spent = first - second\n    return spent if spent > 0 else None",
         "    spent = first - first\n    return spent if spent > 0 else None"),
    ],
    # У местной модели платить не за что: два запроса впустую на каждом её ходу
    # Поток без черновика в ленте приходил с одним только сборщиком размышлений:
    # получателя текста там не было, и в ленте появлялось «'NoneType' object is
    # not callable» — то есть наша ошибка выдавалась за поломку шлюза
    "поток снова зовёт получателя, которого нет": [
        ("aitheatre/cloud.py",
         "                if on_delta is not None:\n                    on_delta(piece, not parts)",
         "                on_delta(piece, not parts)"),
    ],
    "остаток снова спрашивают и на ходу местной модели": [
        ("aitheatre/show.py",
         '        cloud_turn = cloud.is_cloud_model(participant.get("model", ""))',
         '        cloud_turn = True'),
    ],
    "остаток на ключе снова читается один раз на ход": [
        ("aitheatre/show.py",
         '        after, error = cloud.balance(force=True)\n'
         '        first = cloud.balance_number(before)',
         '        after, error = before, ""\n'
         '        first = cloud.balance_number(before)'),
    ],
    "цена хода снова не доезжает до ленты": [
        ("aitheatre/show.py",
         '    summary["spent"] = spent',
         '    summary["spent"] = None'),
    ],
    "кругов хода снова жёстко восемь, а поисков разрешено больше": [
        ("aitheatre/ollama_api.py",
         "    max_iterations = max(8, min_searches + max_searches + max_forced_attempts + 2)",
         "    max_iterations = 8"),
    ],
    "снимок запроса снимают в конце хода, а не в начале": [
        ("aitheatre/show.py",
         "        sent = copy.deepcopy(messages)",
         "        sent = messages"),
    ],
    "из отчёта обрезки пропадает, что именно выброшено": [
        ("aitheatre/text.py",
         "        for msg, tokens in messages_with_tokens[:removed_count]",
         "        for msg, tokens in []"),
    ],
    "блок «что уехало» раскрывается сам, без клика": [
        ("aitheatre/page.py",
         '            return `<details class="post-thinking post-prompt" data-post-id="${post.id}">`',
         '            return `<details open class="post-thinking post-prompt" data-post-id="${post.id}">`'),
    ],
    "сохранённые имена участников снова разыгрывают заново": [
        ("aitheatre/show.py",
         '            cast = sanitize_cast(data.get("cast"))',
         "            cast = []"),
    ],
    "роль из сохранённого состава снова теряется": [
        ("aitheatre/show.py",
         '        set_cast_role(place, role if role in CAST_ROLES else cast_role(item))',
         '        set_cast_role(place, "participant")'),
    ],
    "правила общения и руководства снова не сохраняются": [
        ("aitheatre/show.py",
         '                "static_instructions": list(session.static_instructions or []),',
         '                "static_instructions": [],'),
    ],
    "личная инструкция снова остаётся у имени, а не у места": [
        ("aitheatre/show.py",
         '        instruction = str(participant.get("instruction", "") or "").strip()\n'
         '        if instruction:',
         '        instruction = str(participant.get("instruction", "") or "").strip()\n'
         '        if False:'),
    ],
    "полный сброс снова оставляет в файле весь сохранённый пульт": [
        ("aitheatre/show.py",
         '            json.dumps({"version": 2, "topic": session.topic or ""},',
         '            json.dumps({"version": 2, "topic": session.topic or "",\n'
         '                        "cast": [dict(place) for place in session.runtime_participants]},'),
    ],
    "полный сброс снова забывает тему": [
        ("aitheatre/show.py",
         '            json.dumps({"version": 2, "topic": session.topic or ""},',
         '            json.dumps({"version": 2, "topic": ""},'),
    ],
    "правка инструкций в редакторе снова не сохраняется": [
        ("aitheatre/web.py",
         "    # Редактор — часть режиссёрского пульта, поэтому его правки тоже переживают\n"
         "    # перезапуск: раньше сохранялись только правила судьи, а правила общения\n"
         "    # и руководства после перезапуска тихо возвращались к дефолтным\n"
         "    show.save_theatre_settings()\n",
         ""),
    ],
    "тема снова не сохраняется вместе с пультом": [
        ("aitheatre/show.py",
         '                "topic": session.topic or "",',
         '                "topic": "",'),
    ],
    "«Новый спектакль» снова забывает тему": [
        ("aitheatre/show.py",
         "        self.scene = scene\n        self.topic = topic",
         '        self.scene = scene\n        self.topic = ""'),
    ],
    "полный сброс снова сбрасывает тему": [
        ("aitheatre/show.py",
         "        self.scene = None\n        self.load_new_cast()",
         '        self.topic = ""\n        self.scene = None\n        self.load_new_cast()'),
    ],
    "кнопка полного сброса снова ведёт в никуда": [
        ("aitheatre/page.py",
         "            fetch('/api/settings/reset', {",
         "            fetch('/api/settings/clear', {"),
    ],
    # Тот самый баг из жизни: в шаблоне страницы `\n` написали как питоновский
    # эскейп — Python превратил его в настоящий перевод строки, строка JS
    # порвалась, и вместе с ней осыпалась вся страница: обработчики в разметке
    # остались, а функций у них уже не было.
    "эскейп в шаблоне снова рвёт строку JS вместе со страницей": [
        ("aitheatre/page.py",
         "будет забыт.\\\\n\\\\nТема останется",
         "будет забыт.\\n\\nТема останется"),
    ],
    # Тот самый ход из жизни (gpt-5-nano, «[Модель не дала ответ]» при трёх
    # выполненных поисках). Молчание объяснялось ничем, а последняя попытка
    # была копией неудавшейся: инструмент уезжал снова, и думающая модель
    # просила четвёртый поиск вместо того, чтобы сказать словами.
    "последняя попытка снова повторяет неудавшуюся вместе с инструментом": [
        ("aitheatre/ollama_api.py",
         "options=options, think=False, tools=False,",
         "options=options, think=False,"),
    ],
    "молчание хода снова объясняется ничем": [
        ("aitheatre/ollama_api.py",
         "return content or f\"[Модель не дала ответ] — {reason}\", search_count, search_queries",
         "return content or \"[Модель не дала ответ]\", search_count, search_queries"),
    ],
    "чем ответ кончился и сколько ушло в размышления — снова ниоткуда не видно": [
        ("aitheatre/cloud.py",
         "\"finish_reason\": str(choices[0].get(\"finish_reason\") or \"\"),",
         "\"finish_reason\": \"\","),
        ("aitheatre/cloud.py",
         "(\"reasoning_tokens\", details.get(\"reasoning_tokens\"))):",
         "(\"reasoning_tokens\", None)):"),
    ],
    # Из жизни: реплика собеседника уезжала в модель дважды — один раз строкой
    # истории, второй раз — целиком в задании «X только что сказал: "..."».
    "реплика собеседника снова едет в модель дважды": [
        ("aitheatre/show.py",
         "                            f'выше, в истории диалога. Как {name}, ты тоже '",
         "                            f'выше, в истории диалога. Ещё раз: '"
         " f'{last_post[\"content\"]}. Как {name}, ты тоже '"),
    ],
    # И то, ради чего всё затевалось: «откуда взялось это число» и «что
    # происходило по порядку» должны быть видны и в ленте, и в ДАМПе.
    "журнал хода снова не пополняется: числа и поиски неоткуда взять": [
        ("aitheatre/cloud.py",
         '    report.setdefault("steps", []).append(step)',
         "    pass"),
    ],
    # Сам отчёт собирается до запроса, а шаги в него попадают из журнала;
    # выброшенные шаги — это снова «ход был, а что в нём — не видно»
    "шаги хода снова не доезжают до отчёта у реплики": [
        ("aitheatre/show.py",
         '    turn["steps"] = steps',
         '    turn["steps"] = []'),
    ],
    # Живой ДАМП пишет строку события сразу и доводит её, когда числа наконец
    # известны: без этого строка запроса навсегда оставалась «числа токенов
    # вендор не сообщил», а у мыслей висел вес на момент начала
    "строка хода в ДАМПе снова остаётся без итоговых чисел": [
        ("aitheatre/show.py",
         '        report["fix"] = dump_fix_step',
         '        report["fix"] = None'),
    ],
    "ход снова не попадает в ДАМП": [
        ("aitheatre/show.py",
         "    dump_write(dump_turn_header(post_id, turn))",
         '    dump_write("")'),
    ],
    # Из жизни: судья на qwen3.8-flash две минуты размышлял, ход оборвался —
    # и в файле не оставалось ни строчки, потому что ДАМП писался целиком в конце
    "ДАМП снова пишется только в конце хода — у оборванного хода нет следов": [
        ("aitheatre/cloud.py",
         '    sink = report.get("sink")',
         "    sink = None"),
    ],
    "у запроса снова нет времени окончания": [
        ("aitheatre/cloud.py",
         '    entry["t_end"] = time.time()',
         '    entry["t_end"] = 0'),
    ],
    "вес найденного снова не называется": [
        ("aitheatre/ollama_api.py",
         '        "tokens": text.estimate_tokens(results),',
         '        "tokens": 0,'),
    ],
    # Из жизни: одна и та же мысль стояла в ленте 707 раз — все копии с одним
    # и тем же временем, потому что продолжение мысли клалось в журнал заново
    "размышления в кусках снова становятся сотней копий": [
        ("aitheatre/cloud.py",
         "    journal_push(report, last, piece, again=True)",
         "    journal_push(report, last, piece)"),
    ],
    "размышлений снова нет в хронологии: они отдельным блоком": [
        ("aitheatre/show.py",
         '    summary["thought_steps"] = sum(1 for step in steps if step.get("kind") == "thought")',
         '    summary["thought_steps"] = 0'),
    ],
    # Шапка хода — это способ прочитать отчёт глазами: число, потерянное
    # по дороге, превращает данные в пустоту там, где ответ есть
    "из шапки хода пропало число системного промпта": [
        ("aitheatre/show.py",
         '            f"{numbers_word(budget.get(\'system_tokens\'))} токенов, из сцены {scene} "',
         '            f"токенов, из сцены {scene} "'),
    ],
    "из шапки хода пропало число сообщений сцены": [
        ("aitheatre/show.py",
         '    scene = int(budget.get("messages_after") or 0)',
         "    scene = 0"),
    ],
    "ноль в запасе снова читается как «ноль токенов на ответ»": [
        ("aitheatre/show.py",
         '              else "на ответ ничего не зарезервировано '
         '(CLOUD_MAX_TOKENS = 0 — ответ не ограничиваем)")',
         '              else f"{numbers_word(reserve)} оставлено на ответ модели")'),
    ],
    "ноль в шапке снова затыкают общим числом сообщений": [
        ("aitheatre/show.py",
         '            "messages_after": _as_number(trim_report.get("messages_after"), len(messages)),',
         '            "messages_after": int(trim_report.get("messages_after") or len(messages)),'),
    ],
    # Из жизни: в промпте «сделай минимум поиск», а инструмента шлюз модели
    # не дал — и она дважды ушла в петлю размышлений вместо ответа
    "модель снова просят искать, не дав ей инструмента": [
        ("aitheatre/show.py",
         '        if not ollama_api.takes_tools_now(participant.get("model", "")):',
         "        if False:"),
    ],
    "обрыв хода снова валят на «без предела»": [
        ("aitheatre/cloud.py",
         '        note = (f"ход длился дольше {seconds:g} с (CLOUD_TURN_LIMIT, плюс время "',
         '        note = (f"ход длился дольше {seconds:g} с (CLOUD_TURN_LIMIT, 0 — без предела "'),
    ],
    # Из жизни: MAX_SEARCHES подняли до десяти, а срок хода считался внутри
    # каждого запроса — и у хода с десятью поисками срока не было вовсе
    "срок хода снова считают заново в каждом запросе": [
        ("aitheatre/ollama_api.py",
         "    deadline = cloud.turn_deadline() if cloud.is_cloud_model(model) else None",
         "    deadline = None"),
    ],
    # Из жизни: сайдбар не показывал ни сколько ход идёт, ни сколько осталось —
    # по нему нельзя было понять, ждать минуту или десять
    "часы хода снова не гаснут вместе с ходом": [
        ("aitheatre/show.py",
         "            self.stop_turn_clock()",
         "            pass"),
    ],
    "часы хода снова не помнят про надбавку за поиск": [
        ("aitheatre/ollama_api.py",
         "                    show_session.extend_turn_clock(cloud.per_search_seconds())",
         "                    pass"),
    ],
    "время хода снова не доезжает до реплики": [
        ("aitheatre/show.py",
         '    summary["seconds"] = None if seconds is None else round(float(seconds), 1)',
         '    summary["seconds"] = None'),
    ],
    # Из жизни: в свёрнутой строке стояло одно слово «заминок N», и было
    # непонятно, что за заминка: отказ в поиске сверх лимита или молчание модели
    "заминки снова свалены в одно непонятное число": [
        ("aitheatre/page.py",
         "            if (info.search_refusals) parts.push(`поиск сверх лимита ${info.search_refusals}`);\n"
         "            if (info.silences) parts.push(`⚠️ без ответа ${info.silences}`);",
         "            if (info.search_refusals + info.silences) parts.push(`заминок ${info.search_refusals + info.silences}`);"),
    ],
    "молчание модели снова не считают в сводке хода": [
        ("aitheatre/show.py",
         '    summary["silences"] = _steps_count(steps, "silence")',
         '    summary["silences"] = 0'),
    ],
    # Из жизни: в кабинете шлюза вход был втрое больше, чем «токенов на ввод»
    # в сводке хода — потому что поиск это ещё один круг, и вся история уезжает
    # к модели заново, а сводка считала только первый запрос
    "ввод хода снова считают по одному первому запросу": [
        ("aitheatre/show.py",
         '    summary["tokens_in_total"] = _input_tokens_total(steps)',
         '    summary["tokens_in_total"] = int(summary.get("tokens") or 0)'),
    ],
    "часы страницы снова молчат про надбавку": [
        ("aitheatre/page.py",
         "                if (data.turn_extra) {",
         "                if (false) {"),
    ],
    "состоявшийся поиск снова не даёт ходу времени": [
        ("aitheatre/ollama_api.py",
         "                if deadline is not None:\n"
         "                    deadline += cloud.per_search_seconds()",
         "                if False:\n"
         "                    deadline += cloud.per_search_seconds()"),
    ],
    "ДАМП снова растёт от спектакля к спектаклю": [
        ("aitheatre/show.py",
         '        settings.DUMP_FILE.write_text(header, encoding="utf-8")',
         '        open(settings.DUMP_FILE, "a", encoding="utf-8").write(header)'),
    ],
    "отчёты ходов снова забываются через несколько ходов": [
        ("aitheatre/show.py",
         "        self.turn_log[int(post_id)] = payload",
         "        self.turn_log = {int(post_id): payload}"),
    ],
    "числа поиска снова спрятаны в коде, а не в настройках": [
        ("aitheatre/ollama_api.py",
         "    return (max(0, _as_int(settings.MIN_SEARCHES)),\n"
         "            max(1, _as_int(settings.MAX_SEARCHES)),\n"
         "            max(1, _as_int(settings.SEARCH_MAX_RESULTS)),\n"
         "            max(0, _as_int(settings.MAX_SEARCH_ATTEMPTS)))",
         "    return (0, 3, 5, 2)"),
    ],
    "модель снова заказывает себе сколько результатов захочет": [
        ("aitheatre/ollama_api.py",
         "    asked = min(_as_int(max_results) or limit, limit)",
         "    asked = _as_int(max_results) or limit"),
    ],
    "потолок поисков снова не назван модели": [
        ("aitheatre/show.py",
         "        max_text = (f\" За один ход разрешено не больше {max_searches} поиск(ов): \"\n"
         '                    f"израсходовал их — говори по тому, что уже нашлось.")',
         '        max_text = ""'),
    ],
    "редактор правил снова спрятали за кнопкой": [
        ("aitheatre/page.py",
         '                        <div id="instructionsEditor">',
         '                        <div id="instructionsEditor" style="display:none;">'),
    ],
    "правила снова ждут нажатия при загрузке": [
        ("aitheatre/page.py",
         "        // Правила и инструкции нужны сразу, а не по нажатию: вкладка открыта\n"
         "        loadInstructionsForEdit();\n",
         "        // Правила и инструкции нужны сразу, а не по нажатию: вкладка открыта\n"),
    ],
    "цена спектакля снова пропадает на ходу человека": [
        ("aitheatre/page.py",
         'Ваш ход!</div>${spentLine(data)}',
         'Ваш ход!</div>'),
    ],
    "длинные тексты снова разворачиваются целиком": [
        ("aitheatre/page.py",
         ".then(data => { body.innerHTML = turnBodyHtml(data); clampLongTexts(body); })",
         ".then(data => { body.innerHTML = turnBodyHtml(data); })"),
    ],
    "свёртку снова считают без полей текста": [
        ("aitheatre/page.py",
         "            return Number(contentHeight) - Number(padding || 0) > line * CLAMPED_TEXT_LINES + 2;",
         "            return Number(contentHeight) > line * CLAMPED_TEXT_LINES + 2;"),
    ],
    "разделы отчёта снова слились в простыню без имён": [
        ("aitheatre/page.py",
         """            return `<section class="prompt-block">`
                + `<div class="prompt-block-title">${escapeHtml(title)}</div>`
                + (purpose ? `<div class="prompt-block-purpose">${escapeHtml(purpose)}</div>` : '')
                + inner + `</section>`;""",
         "            return inner;"),
    ],
    "назначение раздела снова пропало": [
        ("aitheatre/page.py",
         "                + (purpose ? `<div class=\"prompt-block-purpose\">${escapeHtml(purpose)}</div>` : '')",
         "                + ''"),
    ],
    "рамки разделов снова остались белыми на тёмной сцене": [
        ("aitheatre/page.py",
         "        body.dark .prompt-block { border-color: #2c2c2c; }",
         "        body.dark .prompt-block { border-color: #ffffff; }"),
    ],
    "файл и страница снова называют разделы по-разному": [
        ("aitheatre/show.py",
         '    "history": "### 🧭 Хронология хода: что происходило по порядку",',
         '    "history": "### Хронология",'),
    ],
}

# Разбор скрипта страницы сверяется с настоящим интерпретатором JavaScript,
# и если его в системе нет, поймать «в скрипт попал лишний знак» нечем.
if shutil.which("node"):
    BUGS["в скрипт страницы снова попал лишний знак"] = [
        ("aitheatre/page.py",
         "        function addCast() {",
         "        function addCast( {"),
    ]


def copy_project(destination: Path, patches):
    """Копирует приложение и тесты в отдельную папку и вносит правки в копию."""
    for name in COPY_DIRS:
        # Папка переиспользуется на каждом баге: копируем заново, а не поверх
        shutil.rmtree(destination / name, ignore_errors=True)
        shutil.copytree(PROJECT_ROOT / name, destination / name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for name in COPY_FILES:
        shutil.copy2(PROJECT_ROOT / name, destination / name)
    for relative, text in patches.items():
        (destination / relative).write_text(text, encoding="utf-8")


def forget_app_modules():
    """Сбрасывает всё уже загруженное, чтобы в следующий раз взялось из копии.

    Без точки входа в этом списке остался бы модуль предыдущей копии: папка уже
    удалена, а из памяти он бы ещё отвечал — и тест на точку входа падал бы
    на ровном месте, маскируя настоящие находки.
    """
    for name in [n for n in sys.modules
                 if n == "aitheatre" or n.startswith("aitheatre.")
                 or n in ("ollama_debate_web", "__main__",
                          "test_theatre", "tests", "tests.test_theatre")]:
        del sys.modules[name]


def run_suite(project_dir: Path, first_failure: bool = False):
    """Прогон набора против указанной копии проекта. Возвращает имена упавших.

    first_failure=True — останавливаться на первом же упавшем тесте: для вопроса
    «ловит ли набор этот баг» большего не нужно, а баг, которого ловит тест
    в конце набора, иначе заставил бы ждать весь набор (см. шапку и --full).
    """
    forget_app_modules()
    sys.path.insert(0, str(project_dir))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location(
                "test_theatre", project_dir / "tests" / "test_theatre.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules["test_theatre"] = module
            spec.loader.exec_module(module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
            total = suite.countTestCases()
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0,
                                             failfast=first_failure).run(suite)
    finally:
        sys.path.remove(str(project_dir))
    caught = sorted({test.id().split(".")[-1]
                     for test, _ in result.failures + result.errors})
    return total, caught


def run_one_bug(title, patches):
    """Один баг: своя копия проекта, свой прогон набора.

    Возвращает (имя бага, файлы, вердикт, кто поймал, сколько секунд).
    В своём процессе — потому что копий нужно сразу несколько (см. run_together),
    а модули приложения живут в памяти процесса по одному разу: второй копии
    взяться неоткуда (см. forget_app_modules).
    """
    started = time.time()

    def verdict(kind, files, who):
        return title, files, kind, who, time.time() - started

    files = sorted({relative for relative, _, _ in patches})
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            changed = {}
            for relative, old, new in patches:
                text = changed.get(relative)
                if text is None:
                    text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                if old not in text:
                    return verdict("miss", files, [f"не нашёл место в {relative}"])
                changed[relative] = text.replace(old, new, 1)
            copy_project(project_dir, changed)
            _, caught = run_suite(project_dir, first_failure=True)
    except Exception as error:
        # Копия не встала или набор упал не тестом — это тоже находка, и молчать
        # о ней нельзя: иначе баг отчитался бы как «нет вердикта»
        return verdict("error", files, [f"{type(error).__name__}: {error}"])
    return verdict("caught" if caught else "blind", files, caught)


def run_together(bugs, workers=None):
    """Прогон багов сразу несколькими процессами: по одному багу на процесс.

    Отдельным процессом, а не потоком и не пулом модуля multiprocessing: прогон
    набора подменяет модули приложения в памяти (см. forget_app_modules), и такое
    соседство в одном процессе не живёт. Своим же процессом проверка получает
    ровно то же, что имела бы, запусти её вручную, — и если процесс кончится
    не отчётом, общий прогон об этом скажет, а не промолчит.

    Итоги возвращаются в порядке заготовок, а не «кто первый успел»: иначе два
    прогона одного и того же набора выглядели бы по-разному.
    """
    items = list(bugs.items())
    if workers is None:
        # Процессов больше, чем ядер, и это не опечатка: прогон набора не только
        # считает, но и ждёт (шлюз-заглушка, потоки, диск), поэтому за половину
        # времени ловится простой ядра. Замер на этом наборе: 8 процессов — 163 с,
        # 16 — 110 с, 32 — 92 с. Больше ставить незачем: выигрыш кончается,
        # а машина занята по-настоящему (см. --workers)
        workers = (os.cpu_count() or 1) * 2
    workers = max(1, min(len(items), int(workers)))
    if workers == 1:
        return [run_one_bug(title, patches) for title, patches in items]

    # Номер заготовки считается по ПОЛНОМУ списку: процесс-исполнитель получает
    # его же и по нему ищет свой баг. Считать по отфильтрованному нельзя —
    # тогда проверялся бы один баг, а назывался бы в отчёте другой
    # (и ни одного бы такого случая не было без метки с именем в отчёте)
    numbers = {title: index for index, title in enumerate(BUGS)}
    self_path = str(Path(__file__).resolve())
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    running = {}          # номер заготовки — запущенный процесс
    next_to_start = 0

    def start(index):
        running[index] = subprocess.Popen(
            [sys.executable, self_path, "--bug", str(numbers[items[index][0]])],
            cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", env=environment)

    def fill():
        """Держать все места занятыми: освободилось — сразу следующий баг.

        Без этого окно схлопнулось бы до одного процесса: сбор идёт по порядку
        заготовок, а готовы они вразнобой.
        """
        nonlocal next_to_start
        while next_to_start < len(items) and len(running) < workers:
            start(next_to_start)
            next_to_start += 1

    fill()
    results = []
    # Ждём процессы в порядке заготовок — так и ответы идут по порядку, —
    # но работают они все сразу: место освободившегося сразу занимает следующий
    for index, (title, _patches) in enumerate(items):
        process = running.pop(index)
        output = process.communicate()[0] or ""
        results.append(verdict_of(output, title, process.returncode))
        fill()

    return results


def verdict_of(output: str, title: str, code: int):
    """Вердикт процесса-исполнителя по его выводу: имя бага, файлы, итог, кто поймал.

    Отчёт — одна строка JSON, а не строка с разделителями: имя бага и имена
    тестов — текст, и делить его по значкам значит однажды разделить не там.

    Метки в выводе нет — значит процесс кончился не отчётом (упал, убит),
    и это тоже находка: молчащий баг отчитался бы как «вердикта нет».
    """
    for line in reversed(output.splitlines()):
        if line.startswith(VERDICT_MARK):
            try:
                report = json.loads(line[len(VERDICT_MARK):])
            except ValueError:
                continue
            # Отчёт обязан быть про тот самый баг: имя едет в ответе именно
            # за этим — по одному номеру процесса-исполнитель и родитель могли бы
            # разойтись в списках, и оба об этом не узнали бы (см. run_together)
            if report.get("title") not in (None, title):
                return (title, [], "error",
                        [f"процесс ответил про другой баг: {report.get('title')}"], 0)
            return (title, list(report.get("files") or []),
                    str(report.get("verdict") or "error"),
                    list(report.get("who") or []),
                    float(report.get("seconds") or 0))
    tail = [line for line in output.strip().splitlines() if line.strip()][-1:]
    reason = tail[0] if tail else "пустой вывод"
    return title, [], "error", [f"процесс кончился кодом {code}: {reason}"], 0


def worker_main(index: int) -> int:
    """Процесс-исполнитель: один баг и одна строка вердикта в вывод.

    Запускается своей же командой (`--bug <номер>`) и ничего не знает ни о других
    багах, ни об общем прогоне: он получает копию проекта и отвечает одним
    вердиктом, а собирает из ответов общую картину родитель (см. run_together).
    """
    title, patches = list(BUGS.items())[index]
    title, files, verdict, who, seconds = run_one_bug(title, patches)
    report = json.dumps({"verdict": verdict, "files": files, "who": who[:5],
                         "title": title, "seconds": round(seconds, 1)},
                        ensure_ascii=False)
    print(f"{VERDICT_MARK}{report}")
    return 0


def selected(wanted):
    """Баги, которых коснулась правка: без аргументов — все, с ним — по слову в имени.

    Весь набор идёт минуты: на каждый баг — своя копия проекта и целый прогон.
    После правки в одном месте нужно не это, а «ловят ли новые проверки именно
    то, что я сломал», и ждать ради двух багов четверть часа незачем.
    """
    if not wanted:
        return BUGS
    words = [word.lower() for word in wanted]
    picked = {title: patches for title, patches in BUGS.items()
              if any(word in title.lower() for word in words)}
    for word in words:
        if not any(word in title.lower() for title in picked):
            print(f"⚠️  По слову «{word}» багов не нашлось")
    return picked


def main(wanted=(), full=False, workers=None):
    """Проверка заготовок: эталон, затем по багу. По умолчанию — быстро (см. шапку)."""
    started = time.time()
    ok = True
    bugs = selected(wanted)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # Эталон всегда идёт целиком: быстрый прогон останавливается на первом
        # упавшем тесте, и на неповреждённом коде он бы замолчал о втором.
        # А эталон нужен честный — «набор зелёный до правок»
        copy_project(project_dir, {})
        total, caught = run_suite(project_dir)
        if caught:
            print(f"[эталон] неповреждённый код, тестов {total}: ПАДАЮТ {caught}")
            ok = False
        else:
            print(f"[эталон] неповреждённый код, тестов {total}: OK")
        print()

        if full:
            print(f"Подбрасываем баги по одному и смотрим, кто их поймает ({len(bugs)}):")
        else:
            print(f"Подбрасываем баги сразу по нескольку ({len(bugs)}) и останавливаем\n"
                  f"набор на первом же упавшем тесте:")
        print("-" * 68)

        if full:
            original = {relative: (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                        for patches in bugs.values() for relative, _, _ in patches}

            for title, patches in bugs.items():
                files = sorted({relative for relative, _, _ in patches})
                changed, broken = {}, False
                for relative, old, new in patches:
                    text = changed.get(relative, original[relative])
                    if old not in text:
                        print(f"⚠️  {title}: не нашёл место в {relative} — проверка невозможна")
                        broken = True
                        ok = False
                        continue
                    changed[relative] = text.replace(old, new, 1)
                if broken:
                    continue

                copy_project(project_dir, changed)
                _, caught = run_suite(project_dir)
                mark = ", ".join(files)
                if caught:
                    print(f"✅ [{mark}] {title}")
                    print(f"     поймали: {', '.join(caught[:5])}")
                else:
                    print(f"❌ [{mark}] {title} — НЕ пойман ни одним тестом")
                    ok = False
        else:
            for title, files, verdict, who, seconds in run_together(bugs, workers):
                mark = ", ".join(files)
                spent = f" ({seconds:.1f} с)" if seconds else ""
                if verdict == "caught":
                    print(f"✅ [{mark}] {title}{spent}")
                    print(f"     поймали: {', '.join(who[:5])}")
                elif verdict == "blind":
                    print(f"❌ [{mark}] {title} — НЕ пойман ни одним тестом{spent}")
                    ok = False
                else:
                    print(f"⚠️  {title}: {who[0]}")
                    ok = False

    print()
    print("-" * 68)
    print("ИТОГ:", "все баги ловятся" if ok else "есть слепые места")
    print(f"Прогон занял {time.time() - started:.1f} с")
    return 0 if ok else 1


if __name__ == "__main__":
    # --full — прежний дотошный порядок (см. шапку): по одному, без остановки
    # на первом падении, со списком всех поймавших тестов
    # --bug <номер> — не человеку, а родителю: один баг и строка вердикта
    arguments = sys.argv[1:]
    if "--bug" in arguments:
        sys.exit(worker_main(int(arguments[arguments.index("--bug") + 1])))
    # --workers N — сколько процессов запускать сразу (по умолчанию — по числу ядер)
    pool_size, words, skip_value = None, [], False
    for word in arguments:
        if skip_value:            # это число — значение предыдущего ключа
            skip_value = False
            continue
        if word == "--workers":
            skip_value = True
            pool_size = int(arguments[arguments.index(word) + 1])
            continue
        if not word.startswith("--"):
            words.append(word)
    sys.exit(main(words, full="--full" in arguments, workers=pool_size))
