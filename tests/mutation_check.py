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
"""

import contextlib
import importlib.util
import io
import shutil
import sys
import tempfile
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

# Что скопировать в проверочную папку: приложение, тесты, точка входа и то,
# на что тесты смотрят (клиент Socket.IO, рендерер формул, иконка). Точка входа
# тоже нужна: иначе из копии нельзя проверить, что она ведёт в то же приложение.
COPY_DIRS = ("aitheatre", "tests", "static")
COPY_FILES = ("favicon.ico", "ollama_debate_web.py")

# Заголовок бага -> список правок: (файл, что заменить, на что заменить).
# Каждый заголовок — реальная ошибка из истории проекта.
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
         '    if not error and uses_tools and "tools" not in body:',
         '    if not error and uses_tools and "tools" not in body and False:'),
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
         "            and search_count < settings.MIN_SEARCHES and takes_tools_now(model))",
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
    "мысли хода снова теряются вместе с черновиком": [
        ("aitheatre/show.py",
         '            thinking=draft.thinking_full() if draft else "",',
         '            thinking="",'),
    ],
    "размышления снова не попадают в стенограмму": [
        ("aitheatre/show.py",
         "        save_thinking_entry(post)",
         "        pass"),
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
         '    if "max_completion_tokens" in text and "max_tokens" in body:',
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
         "        reserve = int(CLOUD_MAX_TOKENS or 0) or int(OPTIONS.get(\"num_predict\", 0) or 0)",
         "        reserve = int(OPTIONS.get(\"num_predict\", 0) or 0)"),
    ],
    "стенограмма снова растёт без конца": [
        ("aitheatre/show.py",
         "    dropped = trim_thinking_log()",
         "    dropped = 0"),
    ],
    "снимок запроса снимают в конце хода, а не в начале": [
        ("aitheatre/show.py",
         "        sent = copy.deepcopy(messages)",
         "        sent = messages"),
    ],
    "сводка снимка не доезжает до поста": [
        ("aitheatre/show.py",
         '        "prompt": (prompt or {}).get("summary"),',
         '        "prompt": None,'),
    ],
    "забытый снимок не говорит об этом в старом посте": [
        ("aitheatre/show.py",
         '                forgotten["summary"]["stored"] = False',
         '                forgotten["summary"]["stored"] = True'),
    ],
    "снимки запросов запоминаются без конца": [
        ("aitheatre/show.py",
         "        overflow = len(self.prompt_log) - max(0, int(settings.PROMPT_KEEP_TURNS))",
         "        overflow = 0"),
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
    "маршрут снимка переименовали, а страница спрашивает старый": [
        ("aitheatre/web.py",
         "@app.route('/api/post/<int:post_id>/prompt')",
         "@app.route('/api/post/<int:post_id>/prompt_text')"),
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
}


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


def run_suite(project_dir: Path):
    """Прогон набора против указанной копии проекта. Возвращает имена упавших."""
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
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    finally:
        sys.path.remove(str(project_dir))
    caught = sorted({test.id().split(".")[-1]
                     for test, _ in result.failures + result.errors})
    return total, caught


def main():
    ok = True

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        copy_project(project_dir, {})
        total, caught = run_suite(project_dir)
        if caught:
            print(f"[эталон] неповреждённый код, тестов {total}: ПАДАЮТ {caught}")
            ok = False
        else:
            print(f"[эталон] неповреждённый код, тестов {total}: OK")
        print()

        print("Подбрасываем баги по одному и смотрим, кто их поймает:")
        print("-" * 68)

        original = {relative: (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                    for patches in BUGS.values() for relative, _, _ in patches}

        for title, patches in BUGS.items():
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

    print()
    print("-" * 68)
    print("ИТОГ:", "все баги ловятся" if ok else "есть слепые места")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
