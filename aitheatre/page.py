"""Страница театра: разметка, стили и клиентский скрипт одной строкой.

Так задумано: страница отдаётся одним куском, без сборки, шаблонизатора и
внешних ресурсов — всё, что ей нужно (клиент Socket.IO, рендерер формул),
лежит в static/ рядом с проектом.
"""

# ============================================================
# СОВРЕМЕННЫЙ ДИЗАЙН
# ============================================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AI Театр</title>
    <link rel="icon" href="/favicon.ico" type="image/x-icon">
    <style>
        /* Внешних шрифтов здесь нет: вся страница рисуется системными Georgia и
           Courier New. Раньше здесь стоял @import с чужим сервером шрифтов —
           никто его не использовал, но браузер всё равно ждал ответа, прежде
           чем нарисовать пульт: без интернета или за прокси он просто висел */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Georgia, 'Times New Roman', Times, serif; background: #ffffff; min-height: 100vh; color: #000000; line-height: 1.6; font-size: 16px; }
        .layout { display: flex; min-height: 100vh; }
        .main-content { flex: 1; padding: 40px 60px; }
        .sidebar { width: 320px; background: #ffffff; border-left: 1px solid #000000; padding: 40px 30px; position: sticky; top: 0; height: 100vh; overflow-y: auto; }
        .container { width: 55%; margin: 0 auto; }
        .header { background: #ffffff; border-bottom: 1px solid #000000; padding: 40px 0 30px 0; margin-bottom: 40px; text-align: center; }
        .header-date { font-size: 14px; letter-spacing: 1px; margin-bottom: 20px; text-transform: uppercase; }
        .header h1 { font-family: Georgia, serif; font-size: 80px; color: #000000; margin-bottom: 15px; font-weight: normal; letter-spacing: 2px; font-variant: small-caps; }
        .header-subtitle { font-size: 16px; font-style: italic; font-weight: normal; border-top: 1px solid #000000; padding-top: 15px; margin-top: 15px; }
        /* Тема — обычный текст по левому краю: её часто пишут пунктами,
           а по центру многострочный список нечитаем */
        .header-topic { font-size: 19px; font-weight: normal; color: #000000; margin-top: 20px; padding: 24px 28px; border: 2px solid #000000; text-align: left; line-height: 1.7; min-height: 60px; white-space: pre-wrap; word-wrap: break-word; }
        .card { background: #ffffff; border: none; border-top: 1px solid #000000; border-bottom: 1px solid #000000; padding: 30px 0; margin-bottom: 40px; }
        .card h2 { font-family: Georgia, serif; font-size: 40px; margin-bottom: 30px; color: #000000; font-weight: normal; text-align: center; letter-spacing: 1px; }
        .participants-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 40px; margin-bottom: 30px; }
        .participant-card { border: none; border-top: 1px solid #000000; padding: 25px 0; background: #ffffff; }
        .avatar-container { display: flex; justify-content: center; margin-bottom: 20px; }
        .avatar-preview { width: 140px; height: 140px; border: 1px solid #000000; overflow: hidden; display: flex; align-items: center; justify-content: center; font-size: 70px; cursor: pointer; background: #ffffff; }
        .avatar-preview:hover { border: 2px solid #000000; }
        .avatar-preview img { width: 100%; height: 100%; object-fit: cover; filter: grayscale(100%); }
        .input-group { margin-bottom: 20px; }
        .input-group label { display: block; font-family: Georgia, serif; font-weight: normal; margin-bottom: 8px; color: #000000; font-size: 16px; text-transform: uppercase; letter-spacing: 1px; }
        .input-group input, .input-group textarea { width: 100%; padding: 10px; border: 1px solid #000000; font-size: 18px; background: #ffffff; color: #000000; font-family: Georgia, serif; }
        .input-group input:focus, .input-group textarea:focus { outline: none; border: 2px solid #000000; }
        .input-group textarea { resize: vertical; min-height: 80px; font-family: Georgia, serif; line-height: 1.6; }
        .btn { padding: 12px 30px; border: 1px solid #000000; font-family: Georgia, serif; font-size: 16px; font-weight: normal; cursor: pointer; margin-right: 15px; letter-spacing: 1px; text-transform: uppercase; }
        .btn-primary { background: #000000; color: #ffffff; }
        .btn-primary:hover { background: #333333; }
        .btn-secondary { background: #ffffff; color: #000000; }
        .btn-secondary:hover { background: #f5f5f5; }
        .btn:disabled { opacity: 0.3; cursor: not-allowed; }
        .sidebar-section { margin-bottom: 40px; padding-bottom: 30px; border-bottom: 1px solid #000000; }
        .sidebar-title { font-family: Georgia, serif; font-size: 14px; font-weight: normal; color: #000000; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 15px; }
        /* Разделы режиссёрского пульта: та же типографика, что у блоков сайдбара */
        .panel-section { border-top: 1px solid #000000; padding-top: 24px; margin-top: 28px; }
        .panel-heading { display: flex; align-items: baseline; gap: 12px; margin-bottom: 8px; }
        .panel-heading .num { font-family: 'Courier New', monospace; font-size: 13px; color: #999999; letter-spacing: 1px; }
        .panel-heading .name { font-size: 14px; text-transform: uppercase; letter-spacing: 2px; }
        .panel-note { font-size: 12px; color: #666666; line-height: 1.55; margin-bottom: 14px; }
        /* Поле сюжета: раньше было неотличимо от подписи — теперь заметное и на вырост */
        .topic-input { display: block; width: 100%; min-height: 170px; box-sizing: border-box; padding: 16px 18px; border: 1px solid #000000; font-family: Georgia, serif; font-size: 17px; line-height: 1.7; color: #000000; resize: vertical; }
        .topic-input:focus { outline: none; border: 2px solid #000000; }
        /* Разделы пульта сворачиваются: на ходу целый состав занимает экран зря */
        .panel-heading { cursor: pointer; user-select: none; }
        .panel-heading:hover .name { text-decoration: underline; }
        .panel-heading .caret { margin-left: auto; font-size: 12px; color: #888888; }
        .panel-body.collapsed { display: none; }
        /* Карточка личной инструкции участника: фон — из класса, а не из инлайна.
           Инлайн перебивал тёмную сцену, и имя участника было белым на белом */
        .instr-card { margin-bottom: 15px; padding: 10px; border-radius: 4px; background: #ffffff; }
        .instr-card.judge { background: #fafafa; }
        .instr-hint { font-size: 11px; color: #666666; margin-bottom: 5px; font-style: italic; }
        /* Карточка места в «Тонкой настройке» — тоже из класса: инлайн-фон
           проделал бы с её подписями то же самое (светлое на белом) */
        .index-card { margin-bottom: 18px; padding: 14px; background: #ffffff; }
        /* Ключевые слова аватара и кнопка поиска — одной строкой, рядом с аватаром */
        .keyword-row { display: flex; gap: 10px; align-items: stretch; }
        .keyword-row input { flex: 1; min-width: 0; }
        .status-bar { background: #ffffff; border: none; border-left: 3px solid #000000; color: #000000; padding: 15px 20px; font-size: 16px; margin-bottom: 20px; font-style: italic; line-height: 1.6; }
        .status-bar.active { border-left: 4px solid #000000; }
        .post { background: #ffffff; border: none; border-top: 1px solid #000000; padding: 40px 0; margin-bottom: 0; display: flex; gap: 30px; }
        .post-avatar { flex-shrink: 0; }
        .post-avatar img { width: 150px; height: 150px; object-fit: cover; border: 1px solid #000000; filter: grayscale(100%); }
        .post-avatar .emoji { width: 150px; height: 150px; background: #ffffff; border: 1px solid #000000; display: flex; align-items: center; justify-content: center; font-size: 75px; }
        .post-content { flex: 1; min-width: 0; }
        .post-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #000000; }
        .post-author { font-family: Georgia, serif; font-size: 36px; font-weight: normal; color: #000000; letter-spacing: 1px; }
        .post-model { font-size: 14px; color: #000000; font-family: Georgia, serif; margin-top: 5px; font-style: italic; }
        .post-time { font-size: 14px; color: #000000; font-style: italic; }
        .post-text { font-size: 18px; line-height: 1.8; color: #000000; word-wrap: break-word; overflow-wrap: break-word; text-align: justify; }
        .post-text p { margin-bottom: 15px; }
        .post-text p:last-child { margin-bottom: 0; }
        .post-text strong { font-weight: bold; }
        .post-text em { font-style: italic; }
        .post-text code { background: #ffffff; padding: 2px 6px; font-family: 'Courier New', monospace; border: 1px solid #000000; }
        .post-text ul, .post-text ol { margin: 15px 0; padding-left: 30px; }
        .post-text li { margin-bottom: 10px; line-height: 1.7; }
        .post-text li::marker { font-weight: bold; }
        /* Черновик реплики: пост уже есть, а текст ещё пишется. Мигающий курсор
           в конце строки и говорит, что реплика не кончилась */
        .post.streaming .post-text::after { content: '▍'; margin-left: 2px; animation: streamCaret 1s steps(2, start) infinite; }
        @keyframes streamCaret { to { visibility: hidden; } }
        /* Мысли модели: то, что она говорит сама с собой, пока не сказала вслух.
           Мелче и бледнее реплики: это не сказанное, а процесс. Свёрткой можно
           убрать их с глаз — но по умолчанию они видны, они уже оплачены */
        .post-thinking { margin: 0 0 18px 0; padding: 10px 16px; border-left: 2px solid #cccccc; background: #fafafa; font-size: 15px; line-height: 1.6; color: #666666; font-style: italic; }
        .post-thinking summary { cursor: pointer; font-style: normal; font-size: 11px; text-transform: uppercase; letter-spacing: 2px; color: #888888; }
        .post-thinking[open] summary { margin-bottom: 8px; }
        .post-thinking .thinking-text { white-space: pre-wrap; }
        /* Сказано раньше: прежняя версия этой же реплики, от которой модель ушла
           (обычно — чтобы сначала поискать). За неё заплачены те же токены,
           поэтому она остаётся в посте свёрнутым блоком, а не пропадает */
        .post-sketch { border-left-color: #e0d0b0; }
        .post-sketch .thinking-hint { font-size: 13px; margin-bottom: 8px; color: #9a9a9a; }
        body.dark .post-thinking { background: #141414; border-left-color: #4a4a4a; color: #a3a3a3; }
        body.dark .post-thinking summary { color: #8c8c8c; }
        body.dark .post-sketch { border-left-color: #4a4030; }
        body.dark .post-sketch .thinking-hint { color: #7a7a7a; }
        /* «Что вошло в запрос к модели»: снимок отправленного запроса — история, системный
           промпт, круги поиска и что из истории выброшено. Рядом с мыслями
           и наброском, но не про смысл реплики, а про счёт */
        .post-prompt { border-left-color: #b8cfe0; font-style: normal; }
        .post-prompt summary { color: #7d92a4; }
        .post-prompt .prompt-body { white-space: normal; }
        .prompt-line { font-size: 13px; line-height: 1.6; margin-bottom: 6px; color: #777777; }
        /* Шаг хода — запрос к модели, поиск или размышления: своя строка, чтобы хронология читалась сверху вниз */
        .prompt-step { font-size: 13px; line-height: 1.6; margin: 8px 0 0 0; color: #555555; }
        .prompt-clock { font-family: Consolas, monospace; font-size: 12px; color: #9a9a9a; margin-right: 4px; }
        .prompt-hint { font-size: 12px; color: #9a9a9a; }
        .prompt-msg { margin: 12px 0 0 0; }
        .prompt-msg-head { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #999999; }
        .prompt-role { display: inline-block; padding: 1px 6px; margin-right: 6px; border-radius: 3px; background: #e8e8e8; color: #555555; }
        .prompt-role-system { background: #ded3f0; }
        .prompt-role-assistant { background: #dbead4; }
        .prompt-tokens, .prompt-num { margin-left: 10px; }
        .prompt-text { white-space: pre-wrap; word-wrap: break-word; font-family: inherit; font-size: 13px; line-height: 1.55; margin: 6px 0 0 0; padding: 8px 10px; background: #ffffff; border: 1px solid #eeeeee; color: #333333; max-height: 340px; overflow: auto; }
        /* Длинный текст в отчёте хода (снимок запроса, найденное, размышления)
           виден тремя строками: дальше он не читается, а проматывается.
           Клик раскрывает целиком — текст никуда не девается, просто не раздувает
           пост на несколько экранов */
        .prompt-text.clamped { max-height: calc(3 * 1.55em); overflow: hidden; cursor: pointer; position: relative; }
        .prompt-text.clamped::after { content: '… ещё — клик, чтобы раскрыть'; position: absolute; right: 0; bottom: 0; padding: 0 4px; font-size: 11px; color: #9a9a9a; background: #ffffff; }
        .prompt-text.clamped.expanded { max-height: 340px; overflow: auto; cursor: auto; }
        .prompt-text.clamped.expanded::after { content: none; }
        /* Объяснение к сообщению: почему у него ноль токенов текста и что есть вместо */
        .prompt-note { font-size: 12px; line-height: 1.5; color: #8a6d3b; margin: 4px 0 0 0; }
        .prompt-note code { background: #f3efe6; padding: 0 3px; border-radius: 3px; }
        .prompt-removed { font-size: 13px; line-height: 1.5; color: #999999; }
        .prompt-removed b { color: #777777; }
        /* Разделы отчёта хода: у каждого своё имя и своя рамка. Без рамок четыре
           разные вещи («кто», «сколько было места», «что происходило», «что
           уехало в запрос») шли сплошным полотном одинаковых строк, и где
           кончается одно и начинается другое — видно не было (см. turnBlock) */
        .prompt-block { border: 1px solid #e6e6e6; border-radius: 6px; padding: 10px 12px 12px; margin: 0 0 12px; }
        .prompt-block:last-child { margin-bottom: 0; }
        .prompt-block-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; color: #7d92a4; margin-bottom: 6px; }
        .prompt-block-purpose { font-size: 12px; line-height: 1.5; color: #9a9a9a; margin: 0 0 8px; }
        body.dark .post-prompt { border-left-color: #35485c; }
        body.dark .post-prompt summary { color: #8399ad; }
        body.dark .prompt-line { color: #8a8a8a; }
        body.dark .prompt-step { color: #b0b0b0; }
        body.dark .prompt-clock { color: #7a7a7a; }
        body.dark .prompt-hint { color: #7a7a7a; }
        body.dark .prompt-msg-head { color: #8c8c8c; }
        body.dark .prompt-role { background: #2a2a2a; color: #b0b0b0; }
        body.dark .prompt-text { background: #101010; border-color: #262626; color: #cccccc; }
        body.dark .prompt-text.clamped::after { background: #101010; color: #7a7a7a; }
        body.dark .prompt-note { color: #c9a86a; }
        body.dark .prompt-note code { background: #2a2620; }
        body.dark .prompt-removed { color: #7a7a7a; }
        body.dark .prompt-removed b { color: #9a9a9a; }
        body.dark .prompt-block { border-color: #2c2c2c; }
        body.dark .prompt-block-title { color: #8399ad; }
        body.dark .prompt-block-purpose { color: #7a7a7a; }
        /* Формулы: LaTeX от сервера, MathML от браузера */
        .post-text .math { font-size: 1.05em; }
        .post-text .math-block { display: block; margin: 14px 0; text-align: center; }
        .post-text math { font-family: 'Cambria Math', 'Latin Modern Math', Georgia, serif; }
        .post-text .temml-error { color: #b00020; font-size: 0.9em; white-space: pre-line; }
        body.dark .post-text .temml-error { color: #ff6b6b !important; }
        
        /* Стили для ролей */
        .role-badge { 
            display: inline-block; 
            padding: 2px 8px; 
            border-radius: 3px; 
            font-size: 11px; 
            font-weight: bold; 
            margin-right: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .role-participant { 
            background: #e3f2fd; 
            color: #1976d2; 
            border: 1px solid #1976d2;
        }
        .role-moderator { 
            background: #fff3e0; 
            color: #f57c00; 
            border: 1px solid #f57c00;
        }
        .role-judge { 
            background: #f3e5f5; 
            color: #7b1fa2; 
            border: 1px solid #7b1fa2;
        }

        /* Цвет ролей в ленте: полоса слева у каждой реплики, тот же цвет, что у
           бейджа роли. Включается кнопкой «Цвет ролей» в подвале.
           Ленте нужны боковые отступы: у .post их не было (padding: 40px 0),
           поэтому цветная граница легла бы ровно на первую букву. */
        body.role-marks .post { padding-left: 35px; padding-right: 40px; }
        body.role-marks .post.post-role-participant { border-left: 5px solid #1976d2; }
        body.role-marks .post.post-role-moderator { border-left: 5px solid #f57c00; }
        body.role-marks .post.post-role-judge { border-left: 5px solid #7b1fa2; }

        
        .search-info { background: transparent; padding: 20px 0 0 0; margin-top: 25px; font-size: 14px; color: #000000; font-style: italic; border-top: 1px solid #000000; }
        .search-info strong { font-weight: normal; font-style: normal; text-transform: uppercase; letter-spacing: 2px; display: block; margin-bottom: 10px; font-size: 13px; }
        .search-query { display: inline; margin-right: 12px; }
        .search-query:not(:last-child)::after { content: " • "; }
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.95); cursor: pointer; }
        .modal-content { margin: auto; display: block; max-width: 90%; max-height: 90%; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); border: 1px solid #000000; filter: grayscale(100%); }
        .modal-close { position: absolute; top: 20px; right: 40px; color: white; font-size: 40px; font-weight: bold; cursor: pointer; }
        /* Меню эмодзи-аватара: то же затемнение, что у полного портрета,
           но внутри — набор значков, а не одна картинка */
        .emoji-menu { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #ffffff; border: 1px solid #000000; padding: 20px; max-width: 90vw; max-height: 80vh; overflow: auto; cursor: default; }
        .emoji-menu-title { font-size: 13px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 14px; }
        .emoji-menu-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(56px, 1fr)); gap: 8px; }
        .emoji-choice { display: flex; align-items: center; justify-content: center; width: 56px; height: 56px; font-size: 30px; border: 1px solid #000000; cursor: pointer; background: #ffffff; }
        .emoji-choice:hover { border-width: 2px; }
        .emoji-choice.current { background: #e8e8e8; border-width: 2px; }
        body.dark .emoji-menu { background: #101010; border-color: #3a3a3a; color: #e8e8e8; }
        body.dark .emoji-choice { background: #141414; border-color: #3a3a3a; }
        body.dark .emoji-choice.current { background: #242424; }
        .footer { text-align: center; color: #000000; padding: 30px 0; font-size: 14px; border-top: 1px solid #000000; margin-top: 40px; font-style: italic; letter-spacing: 1px; }
        /* Нулевой раздел (готовность): метка о проблемах видна и в свёрнутом виде */
        .ready-badge { margin-left: 8px; font-size: 11px; font-weight: bold; letter-spacing: 1px; color: #b00020; }
        /* Пояснение вместо поля реплики, когда не ваша очередь: блок остаётся на месте */
        .turn-note { font-size: 13px; font-style: italic; color: #666666; padding: 2px 0 4px 0; line-height: 1.6; }
        /* Числовые поля параметров: пустое поле серое, заполненное — чёрное */
        .param-input { border: 1px solid #cccccc; }
        .param-input:not(.filled) { color: #555555; }
        .param-input.filled { border-color: #000000; color: #000000; }

        /* ══ Тёмная сцена ═══════════════════════════════════════════════
           Класс dark на body перекрашивает интерфейс: чёрные элементы (рамки,
           заголовки, карточки) становятся светлыми, чтобы быть видными на
           тёмном фоне. Часть цветов интерфейс ставит инлайном — их приходится
           перебивать, поэтому дальше встречается !important. */
        body.dark { background: #0d0d0d; color: #e8e8e8; color-scheme: dark; }
        /* Один холст: фон крупных блоков совпадает с фоном страницы — точно так же, как
           в светлой теме, где всё белое и разделяют только линии. Стоит задать им другой
           фон — и блоки выглядят «отрезанными» по бокам, а текст упирается в край плашки */
        body.dark .header, body.dark .card, body.dark .post, body.dark .footer,
        body.dark .sidebar, body.dark .participant-card { background: #0d0d0d; border-color: #3a3a3a; }
        /* Плашки с внутренними отступами подсветить можно: текст в них не упирается в край */
        body.dark .status-bar { background: #171717; border-left-color: #e8e8e8; }
        body.dark .modal-content { border-color: #3a3a3a; }
        body.dark .header h1, body.dark .card h2, body.dark .header-subtitle, body.dark .header-date,
        body.dark .sidebar-title, body.dark .post-author, body.dark .post-text, body.dark .post-model,
        body.dark .post-time, body.dark .header-topic, body.dark .search-info, body.dark .footer,
        body.dark .panel-heading .name, body.dark .status-bar, body.dark .btn { color: #e8e8e8; }
        body.dark .header-topic, body.dark .panel-section, body.dark .post, body.dark .header,
        body.dark .footer, body.dark .sidebar, body.dark .sidebar-section,
        body.dark .search-info, body.dark .post-header, body.dark .participant-card,
        body.dark .post-avatar img { border-color: #3a3a3a; }
        body.dark input, body.dark textarea, body.dark select { background: #1c1c1c !important; color: #e8e8e8 !important; border-color: #5a5a5a !important; }
        body.dark input::placeholder, body.dark textarea::placeholder { color: #7d7d7d !important; }
        body.dark .btn-primary { background: #e8e8e8; color: #111111; }
        body.dark .btn-primary:hover { background: #cfcfcf; }
        body.dark .btn-secondary { background: #1c1c1c; color: #e8e8e8; }
        body.dark .btn-secondary:hover { background: #272727; }
        body.dark .avatar-preview, body.dark .post-avatar .emoji { background: #141414; border-color: #3a3a3a; }
        body.dark .avatar-preview:hover { border-color: #9a9a9a; }
        body.dark .post-avatar img, body.dark .avatar-preview img { filter: grayscale(100%) brightness(0.82); }
        body.dark .status-bar { border-left-color: #e8e8e8; }
        body.dark .post-text code { background: #1c1c1c; border-color: #4a4a4a; }
        body.dark .role-participant { background: #12283a; color: #79b8ff; border-color: #2f5a80; }
        body.dark .role-moderator { background: #33260f; color: #ffb066; border-color: #7a5520; }
        body.dark .role-judge { background: #281735; color: #c79ae0; border-color: #6a3f8a; }
        /* Полоса роли на тёмном: те же роли, но чуть приглушённее бейджа —
           широкую цветную полосу ярким цветом читать тяжелее */
        body.dark.role-marks .post.post-role-participant { border-left-color: #4d8fcc; }
        body.dark.role-marks .post.post-role-moderator { border-left-color: #cc8330; }
        body.dark.role-marks .post.post-role-judge { border-left-color: #8f5cae; }
        body.dark .param-input { border-color: #5a5a5a; }
        body.dark .param-input:not(.filled) { color: #a0a0a0; }
        body.dark .param-input.filled { border-color: #cfcfcf; color: #e8e8e8; }
        body.dark .turn-note { color: #a0a0a0; }
        body.dark .index-card { background: #141414; }
        body.dark .instr-card { background: #141414; }
        body.dark .instr-card.judge { background: #1c1c1c; }
        body.dark .instr-hint { color: #a3a3a3; }
        body.dark .ready-badge { color: #ff6b6b; }
        body.dark .panel-note { color: #a3a3a3; }
        body.dark .btn { border-color: #6f6f6f; }
        /* Текст сайдбара: цвет ему ставит разметка инлайном, поэтому красим по id.
           По атрибуту style здесь нельзя: блоки, которые JS переключает через display,
           браузер переписывает целиком и #000000 превращается в rgb(0, 0, 0) */
        body.dark #vramDisplay, body.dark #participantsDisplay,
        body.dark #moderatorInstructionsDisplay { color: #e8e8e8 !important; }
        body.dark #statusPlaceholder, body.dark #rulesDisplay,
        body.dark #randomizeHint { color: #a3a3a3 !important; }
        /* Остальные инлайновые чёрные подписи (те, что JS не трогает) */
        body.dark [style*="color:#000"] { color: #e8e8e8 !important; }
        /* Эти блоки интерфейс переключает через style.display, а браузер при этом
           переписывает весь атрибут style и превращает #666 в rgb(102,102,102),
           так что по аттрибуту их уже не поймать — красим по id */
        body.dark #modelsWarning { border-color: #ff6b6b !important; color: #ff8a8a !important; }
        body.dark #vramWarning { border-color: #e8c56b !important; color: #e8c56b !important; }
        body.dark #readyOk { color: #a3a3a3 !important; }
        /* Инлайновые плашки и пояснения внутри пульта */
        body.dark [style*="#ffffff"], body.dark [style*="#fafafa"],
        body.dark [style*="#f9f9f9"], body.dark [style*="#f5f5f5"] { background: #181818 !important; }
        body.dark [style*="#cccccc"], body.dark [style*="#ddd"] { border-color: #3a3a3a !important; }
        body.dark [style*="border:1px solid #000"], body.dark [style*="border:2px solid #000"],
        body.dark [style*="border:1px dashed #000"] { border-color: #6f6f6f !important; }
        body.dark [style*="border:1px solid #7b1fa2"] { border-color: #a06fc0 !important; }
        body.dark [style*="color:#666"], body.dark [style*="color:#555"],
        body.dark [style*="color:#888"] { color: #a3a3a3 !important; }
        body.dark [style*="color:#999"] { color: #8c8c8c !important; }
        body.dark [style*="color:#333"] { color: #c9c9c9 !important; }
        body.dark [style*="color:#b00020"] { color: #ff6b6b !important; }
        body.dark [style*="border:2px solid #b00020"] { border-color: #ff6b6b !important; }
        body.dark [style*="border:2px solid #b8860b"] { border-color: #e8c56b !important; }
        body.dark [style*="color:#b8860b"], body.dark [style*="color:#8a6d00"] { color: #e8c56b !important; }
        body.dark [style*="color:#7b1fa2"] { color: #c79ae0 !important; }
        body.dark [style*="color:#1976d2"] { color: #79b8ff !important; }
        body.dark [style*="color:#f57c00"] { color: #ffb066 !important; }
    </style>
</head>
<body>
    <div class="layout">
        <div class="main-content">
            <div class="container">
                <div class="header">
                    <div class="header-date" id="headerDate"></div>
                    <h1>AI Театр</h1>
                    <div class="header-subtitle">Спектакль нейросетей • Акт I</div>
                    <div class="header-topic" id="topicDisplay" style="display:none;"></div>
                </div>
                <!-- Единый режиссёрский пульт: та же форма служит и настройкой
                     спектакля, и пультом модератора на ходу -->
                <!-- Единый режиссёрский пульт: одна форма и для настройки, и для управления
                     на ходу. Разделы пронумерованы в порядке работы режиссёра. -->
                <div class="card" id="controlPanel">
                    <h2 id="controlPanelTitle">Режиссёрский пульт</h2>
                    <!-- Развернуть или свернуть весь пульт разом. Нужна именно на ходу:
                         спектакль начался — все разделы свёрнуты до заголовков, и этой
                         кнопкой видно всё сразу, не разбирая по одному -->
                    <div style="text-align:center;margin:-18px 0 6px 0;">
                        <button class="btn btn-secondary" id="sectionsToggle" onclick="toggleAllSections()" style="display:none;margin:0;padding:6px 15px;font-size:13px;">▾ Развернуть пульт</button>
                    </div>

                    <!-- Нулевой раздел: готовность к спектаклю. Раскрывается сам, когда
                         есть о чём предупредить, и сворачивается, когда всё в порядке -->
                    <div class="panel-section" id="sec-ready">
                        <div class="panel-heading"><span class="num">00</span><span class="name">Готовность</span><span class="ready-badge" id="readyBadge"></span></div>
                        <div style="display:flex;gap:15px;flex-wrap:wrap;align-items:center;margin-bottom:12px;">
                            <button class="btn btn-secondary" onclick="checkReadiness()" style="padding:6px 15px;font-size:13px;margin:0;">🔄 Проверить сейчас</button>
                            <span style="font-size:12px;color:#666;">Проверка идёт при загрузке страницы и при правках состава</span>
                        </div>
                        <div id="readyOk" style="font-size:13px;color:#666;font-style:italic;">Проверка ещё не проходила.</div>
                        <div id="modelsWarning" style="display:none;margin:0 0 14px 0;padding:14px 16px;border:2px solid #b00020;color:#b00020;font-size:15px;line-height:1.5;"></div>
                        <div id="vramWarning" style="display:none;margin:0;padding:14px 16px;border:2px solid #b8860b;color:#8a6d00;font-size:15px;line-height:1.5;"></div>
                    </div>

                    <div class="panel-section">
                        <div class="panel-heading"><span class="num">01</span><span class="name">Сюжет</span></div>
                        <div class="panel-note">Тема попадает в системные промпты следующих реплик. Менять можно и до спектакля, и на ходу. Она сохраняется вместе с составом — после перезапуска театра придумывать её заново не придётся, «Новый спектакль» её тоже не трогает, а убирает только «🧹 Полный сброс».</div>
                        <textarea id="topicInput" class="topic-input" rows="7" placeholder="Тема одной строкой или с пунктами — переносы строк сохраняются. Ctrl+Enter — применить." onkeydown="if (event.ctrlKey &amp;&amp; event.key === 'Enter') { event.preventDefault(); applyTopic(); }"></textarea>
                        <div style="margin-top:10px;">
                            <button class="btn btn-secondary" onclick="applyTopic()">🎯 Применить тему</button>
                            <span style="font-size:12px;color:#666;">Ctrl+Enter — применить не отрывая рук</span>
                        </div>
                    </div>

                    <div class="panel-section" id="sec-cast">
                        <div class="panel-heading"><span class="num">02</span><span class="name">Состав</span></div>
                        <div class="panel-note">Сцена: кто играет, в каком порядке и в какой роли. Правки действуют сразу и работают до и на ходу — уже сказанное не меняется. Порядок карточек — очередь реплик, поэтому участников можно переставлять, а место может быть любым из трёх ролей. Характер (температура и прочее) на каждый спектакль разыгрывается случайно. Всё собранное здесь сохраняется: после перезапуска театра вы вернётесь к этому же составу — с теми же именами, моделями и ролями, только с чистой историей.</div>
                        <div id="castEditor"></div>
                        <!-- Общие подсказки для поля модели всего состава: у шлюза
                             сотни моделей, и своя копия списка в каждой карточке
                             раздувала бы разметку в тысячи строк -->
                        <datalist id="modelList"></datalist>
                        <div style="display:flex;gap:15px;flex-wrap:wrap;align-items:center;margin-top:6px;">
                            <button class="btn btn-secondary" onclick="saveCast()" style="margin:0;">💾 Применить состав</button>
                            <button class="btn btn-secondary" onclick="addCast()" style="margin:0;" title="Добавить место в конец сцены: имя, эмодзи и профессия придумаются сами, а модель будет как у соседа. Потом место можно настроить как любое другое">➕ Добавить участника</button>
                            <button class="btn btn-secondary" onclick="randomizeCharacters()" style="margin:0;" title="Заново вытянуть случайный характер каждому ИИ-участнику — и судье тоже (числа, вписанные вручную, будут перезаписаны)">🎲 Разбросать характеры</button>
                            <button class="btn btn-secondary" onclick="resetEverything()" style="margin:0;" title="Чистый старт: тема, состав, общие правила, руководства модератора и правила судьи — из settings.py, а сохранённый пульт забывается">🧹 Полный сброс</button>
                            <span id="randomizeHint" style="font-size:12px;color:#666;"></span>
                        </div>
                    </div>

                    <div class="panel-section">
                        <div class="panel-heading"><span class="num">03</span><span class="name">Правила и инструкции</span></div>
                        <div class="panel-note">Общие правила общения, руководства модератора, правила судьи и личные инструкции участников. Работают одинаково до и во время спектакля и сохраняются вместе с составом — после перезапуска театра редактор откроется с тем же текстом. Личная инструкция принадлежит месту в составе, а не имени: «Новый спектакль» переименует участников, но инструкции оставит на своих местах.</div>
                        <!-- Вкладка и есть редактор: содержимое видно сразу, без
                             отдельной кнопки входа — та требовала лишнего нажатия -->
                        <div id="instructionsEditor">
                            <div style="font-size:13px;color:#333;margin-bottom:15px;padding:10px;background:#f9f9f9;border:1px solid #ddd;">
                                <strong>Доступные плейсхолдеры:</strong>
                                <code>{ИМЯ}</code> — имя текущего участника,
                                <code>{СОБЕСЕДНИКИ}</code> — остальные через запятую,
                                <code>{ТЕМА}</code> — тема обсуждения
                            </div>

                            <div style="margin-bottom:20px;">
                                <label style="display:block; font-weight:bold; margin-bottom:8px; font-size:14px;">Правила общения (для всех участников):</label>
                                <div id="staticInstructionsEditor"></div>
                                <button class="btn btn-secondary" onclick="addStaticInstructionEditor()" style="margin-top:10px; padding:6px 15px; font-size:14px;">➕ Добавить правило</button>
                            </div>

                            <div style="margin-bottom:20px;">
                                <label style="display:block; font-weight:bold; margin-bottom:8px; font-size:14px;">Руководства (указания модератора):</label>
                                <div id="moderatorMessagesEditor"></div>
                                <button class="btn btn-secondary" onclick="addModeratorMessageEditor()" style="margin-top:10px; padding:6px 15px; font-size:14px;">➕ Добавить руководство</button>
                            </div>

                            <div style="margin-bottom:20px;">
                                <label style="display:block; font-weight:bold; margin-bottom:8px; font-size:14px;">⚖️ Правила для роли судьи:</label>
                                <div id="judgeRulesEditor"></div>
                                <button class="btn btn-secondary" onclick="addJudgeRuleEditor()" style="margin-top:10px; padding:6px 15px; font-size:14px;">➕ Добавить правило судьи</button>
                                <div style="font-size:12px;color:#666;margin-top:10px;font-style:italic;">Личный системный промпт судьи — ниже, в блоке «Индивидуальные инструкции».</div>
                            </div>

                            <div style="margin-bottom:20px;">
                                <label style="display:block; font-weight:bold; margin-bottom:8px; font-size:14px;">Индивидуальные инструкции участников:</label>
                                <div id="participantInstructionsEditor"></div>
                            </div>

                            <button class="btn btn-primary" onclick="saveInstructions()" style="margin-top:10px;">💾 Применить изменения</button>
                        </div>
                    </div>

                    <!-- Блок «Ваша реплика» всегда на месте: раньше он исчезал, и нумерация
                         разделов прыгала с 03 сразу на 05. Меняется только содержимое —
                         поле реплики или пояснение, почему его сейчас нет -->
                    <div class="panel-section" id="turnSection">
                        <div class="panel-heading"><span class="num">04</span><span class="name" id="turnTitle">Ваша реплика</span></div>
                        <div id="turnNote" class="turn-note">Спектакль ещё не начат — поле появится, когда очередь дойдёт до вас.</div>
                        <div id="turnComposer" style="display:none;">
                            <div class="panel-note">Пустое сообщение = пропуск действия. Ctrl+Enter — отправить.</div>
                            <textarea id="moderatorInput" rows="4" style="width:100%; padding:12px; border:2px solid #000000; font-size:16px; font-family:Georgia,serif; margin-bottom:15px;" placeholder="Напишите реплику или оставьте пустым чтобы пропустить действие..." onkeydown="if (event.ctrlKey &amp;&amp; event.key === 'Enter') { event.preventDefault(); sendModeratorMessage(); }"></textarea>
                            <div style="display:flex; gap:15px; align-items:center;">
                                <button class="btn btn-primary" onclick="sendModeratorMessage()">Отправить</button>
                                <span style="font-size:12px;color:#666;font-style:italic;">Реплика станет постом от вашего имени</span>
                            </div>
                        </div>
                    </div>

                    <div class="panel-section">
                        <div class="panel-heading"><span class="num">05</span><span class="name">Управление спектаклем</span></div>
                        <div class="panel-note">«Завершить» опускает занавес, но не закрывает театр: после него можно собрать новый состав и играть дальше. Сервер останавливает «Покинуть театр».</div>
                        <div style="display:flex;gap:15px;flex-wrap:wrap;">
                            <button class="btn btn-primary" id="startBtn" onclick="startDebate()">🎭 Начать спектакль</button>
                            <button class="btn btn-secondary" id="finishBtn" onclick="finishDebate()" style="display:none;">⏹ Завершить спектакль</button>
                            <button class="btn btn-secondary" id="newBtn" onclick="newShow()" style="display:none;">🎭 Новый спектакль</button>
                        </div>
                    </div>
                </div>
                <div id="posts"></div>
                <div class="footer">
                    <button class="btn btn-secondary" id="rolesBtn" onclick="toggleRoleMarks()" title="Цветная полоса слева у реплик: участник — синяя, модератор — оранжевая, судья — сиреневая">🎨 Цвет ролей: вкл</button>
                    <button class="btn btn-secondary" id="themeBtn" onclick="toggleTheme()" title="Светлая и тёмная сцена">🌙 Тёмная сцена</button>
                    <button class="btn btn-secondary" onclick="shutdownServer()">Покинуть театр</button>
                </div>
            </div>
        </div>
        <div class="sidebar">
            <div class="sidebar-section">
                <div class="sidebar-title">Статус</div>
                <div id="statusBar" class="status-bar" style="display:none;"></div>
                <div id="statusPlaceholder" style="color:#666;font-size:13px;">Ожидание начала...</div>
            </div>
            <div class="sidebar-section">
                <div class="sidebar-title">Память GPU</div>
                <div id="vramDisplay" style="color:#000000;font-size:13px;line-height:1.6;">—</div>
            </div>
            <div class="sidebar-section">
                <div class="sidebar-title">Персонажи</div>
                <div id="participantsDisplay" style="color:#000000;font-size:13px;line-height:1.6;"></div>
            </div>
            <div class="sidebar-section">
                <div class="sidebar-title">Правила общения</div>
                <div id="rulesDisplay" style="color:#000000;font-size:12px;line-height:1.5;font-style:italic;"></div>
            </div>
            <div class="sidebar-section">
                <div class="sidebar-title">Инструкции от руководства</div>
                <div id="moderatorInstructionsDisplay" style="color:#000000;font-size:12px;line-height:1.5;font-weight:bold;"></div>
            </div>
        </div>
    </div>
    <div id="avatarModal" class="modal" onclick="closeAvatarModal()">
        <span class="modal-close">&times;</span>
        <img class="modal-content" id="avatarModalImg">
    </div>
    <div id="emojiModal" class="modal" onclick="closeEmojiPicker()">
        <div class="emoji-menu" onclick="event.stopPropagation()">
            <div class="emoji-menu-title" id="emojiMenuTitle"></div>
            <div class="emoji-menu-grid" id="emojiMenuGrid"></div>
        </div>
    </div>
    <!-- Клиент Socket.IO лежит рядом с проектом: свежие посты приходят сразу,
         а опрос /api/status остаётся страховкой -->
    <script src="/static/socket.io.min.js"></script>
    <!-- Формулы: Temml превращает LaTeX в MathML, который рисует сам браузер —
         ни картинок, ни шрифтов не нужно. Файл лежит рядом с проектом -->
    <script src="/static/temml.min.js"></script>
    <script>
        function escapeHtml(s) {
            return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
        }
        
        // Состав спектакля. У сервера он один и тот же и до старта, и на сцене,
        // поэтому страница не держит вторую (свою) копию настроек
        let cast = [];
        // Наборы эмодзи-аватаров приходят с сервера (см. show.set_participant_emoji):
        // меню должно предлагать только то, что действительно можно поставить
        let EMOJIS = {male: [], female: [], neutral: []};
        let models = [];               // скачанные модели Ollama для выбора в составе
        let cloudModels = [];          // модели облачного шлюза, с префиксом «cloud:»
        let cloudHint = '';            // почему облачных моделей нет: нет ключа или шлюз молчит
        let thinkingModels = [];       // из них те, что умеют размышлять (capabilities Ollama)
        let debateRunning = false;
        let pollInterval = null;
        let sectionsPhase = null;      // фаза пульта: настройка / спектакль идёт / занавес
        let turnSectionState = null;   // раскрыт ли раздел «Ваша реплика» на этом ходу
        let lastPostCount = 0;
        let instructionsTick = 0;
        let defaultJudgePrompt = '';  // им заполняется пустое поле промпта судьи
        let mySessionId = null;        // id текущей сессии; следим за сменой на сервере
        let showFinished = false;      // спектакль идёт или уже завершён (но не новый спектакль)
        let finishRequested = false;   // занавес заказан, ждём, пока модель доиграет реплику
        // Счётчик для строк, добавленных кнопками «➕»: индекс по длине контейнера
        // повторялся после удаления строки выше, и ❌ у новой строки удалял чужую
        let editorRowSeq = 0;
        
        // Разделы пульта сворачиваются со заголовка: обёртки расставляет этот вызов
        decoratePanelSections();

        // Состав и список моделей
        loadCast().then(() => { updatePanel(); updateSidebarParticipants(); tryRestoreSession(); });
        fetch('/api/models')
            .then(r => r.json())
            .then(data => {
                models = data.models || [];
                cloudModels = data.cloud_models || [];
                renderModelSuggestions();
                thinkingModels = data.thinking_models || [];
                if (data.error) console.warn('Список моделей недоступен: ' + data.error);
                // Облако без ключа — не ошибка, а «ещё не настроено»: скажем об этом
                // в разделе готовности, а не молчанием в списке моделей
                if (data.cloud && !data.cloud.configured) cloudHint = 'не настроено';
                else if (data.cloud && data.cloud.error) cloudHint = data.cloud.error;
                else cloudHint = '';
                renderCastEditor();
            })
            .catch(err => console.warn('Не удалось получить список моделей:', err));
        
        function loadCast() {
            return fetch('/api/participants', {cache: 'no-store'})
                .then(r => r.json())
                .then(data => {
                    cast = data.participants || [];
                    // Список характеров держит сервер: тот же набор он разыгрывает
                    // случайно при подъёме занавеса
                    if (data.characters) CHARACTERS = data.characters;
                    if (data.emojis) EMOJIS = data.emojis;
                    renderCastEditor();
                    renderModelsWarning(data.models_status);
                    renderVramWarning(data.vram_status);
                    syncReadinessSection();
                    const hint = document.getElementById('randomizeHint');
                    if (hint) {
                        hint.textContent = data.randomize_characters
                            ? 'на каждый спектакль характеры тянутся заново' : '';
                    }
                    if (data.topic) document.getElementById('topicInput').value = data.topic;
                    return data;
                })
                .catch(err => { console.error('Не удалось загрузить состав:', err); return {}; });
        }
        
        refreshMemory();  // сразу видно, что уже загружено в Ollama (могут быть чужие модели)
        // Правила и инструкции нужны сразу, а не по нажатию: вкладка открыта
        loadInstructionsForEdit();
        
        // ── Каналы связи ─────────────────────────────────────────────────
        // Основной канал — Socket.IO: лента приходит событием new_post, черновик —
        // stream_post, а состояние спектакля сервер сам шлёт раз в секунду
        // (status_update). Опрос /api/status остался подстраховкой: с сокетом он
        // редкий, без сокета — снова частый, как раньше. Раньше опрос был основным
        // и реплика появлялась с задержкой до трёх секунд.
        const POLL_MS = 3000;            // без сокета: опрос — единственный канал
        const POLL_FALLBACK_MS = 20000;  // с сокетом: редкая сверка на случай обрыва

        function stopPolling() {
            if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
        }

        function startPolling() {
            stopPolling();
            pollInterval = setInterval(updatePosts,
                                       socket && socket.connected ? POLL_FALLBACK_MS : POLL_MS);
        }

        let socket = null;
        if (typeof io === 'function') {
            try {
                socket = io();
                // Состояние пульта. Свежее опроса: «Ищет: …» видно почти сразу,
                // а не через три секунды. Пустое состояние не принимаем — иначе
                // до первого спектакля оно «оживило» бы пустой театр
                socket.on('status_update', data => {
                    if (!data.running && !data.finished && !data.total_posts) return;
                    applyStatus(data);
                });
                // Реплика приходит готовым постом: перепрашивать её незачем
                socket.on('new_post', post => {
                    if (debateRunning) addNewPosts([post]);
                });
                // Черновик приходит готовым и целиком: перепрашивать его незачем,
                // поэтому он не дёргает updatePosts, а рисуется сам
                socket.on('stream_post', draft => { if (debateRunning) upsertStreamPost(draft); });
                // Сокет отвалился — опрос становится частым, вернулся — снова редким
                socket.on('disconnect', () => { if (pollInterval) startPolling(); });
                socket.on('connect', () => { if (pollInterval) startPolling(); });
            } catch (e) {
                console.warn('Socket.IO недоступен, обновляемся опросом:', e);
                socket = null;
            }
        }
        
        // Восстановление активной сессии при загрузке страницы: спектакль идёт
        // (или уже отыгран) — возвращаемся к нему, а не начинаем новый.
        function tryRestoreSession() {
            fetch('/api/status?lastPostCount=0', {cache: 'no-store'})
                .then(r => r.json())
                .then(data => {
                    if (!data.session_id) return;
                    if (!data.running && !data.finished) return;
                    if (!data.running && !data.total_posts) return;

                    mySessionId = data.session_id;
                    debateRunning = true;
                    showFinished = !!data.finished;

                    setTopicDisplay(data.topic);
                    if (data.topic) document.getElementById('topicInput').value = data.topic;
                    document.getElementById('posts').innerHTML = '';

                    addNewPosts(data.new_posts);
                    lastPostCount = data.total_posts || lastPostCount;

                    if (data.running) startPolling();
                    updatePosts();
                })
                .catch(() => {});
        }

        // «Характер» — готовые наборы параметров генерации. Числа управляют не смыслом
        // реплик (его задаёт личная инструкция), а тем, насколько участник предсказуем,
        // склонен повторяться и размышляет ли перед ответом.
        // Список приходит с сервера: он же используется для случайного розыгрыша
        // характеров на новый спектакль, поэтому он один на всех.
        let CHARACTERS = {};
        const CUSTOM_CHARACTER = {label: '🎚 Свой — сам выберу', group: 'manual',
                                  hint: 'числа выставляются вручную в «Тонкой настройке»', params: {}};
        const CHARACTER_GROUPS = {balanced: 'Уравновешенные', extreme: 'С перекосом', manual: 'Вручную'};

        function characterInfo(key) {
            return (key && CHARACTERS[key]) ? CHARACTERS[key] : CUSTOM_CHARACTER;
        }

        // Числовые параметры, которыми управляет пульт. Список обязан совпадать
        // с PER_PARTICIPANT_OPTION_KEYS на сервере — за этим следит TestTuningPanel.
        // Раньше в нём было только пять чисел: top_k, min_p и seed можно было задать
        // в PARTICIPANTS, но из пульта их не было видно и не поменять.
        const PARAM_KEYS = ['temperature', 'top_p', 'top_k', 'min_p',
                            'repeat_penalty', 'presence_penalty', 'frequency_penalty', 'seed'];

        // Пересчитывает строку «Уйдёт в модель» по текущим полям формы — чтобы
        // не приходилось сохранять состав, чтобы понять, что применится
        function refreshEffective(idx) {
            const box = document.getElementById('effective-' + idx);
            if (!box) return;
            // Живому участнику числа не нужны: говорим это вместо списка параметров,
            // иначе после вписанного руками «human» строка «Уйдёт в модель» врала бы
            const modelEl = document.getElementById('model-' + idx);
            if (modelEl && modelEl.value.trim() === 'human') {
                box.innerHTML = '<strong>Живой участник:</strong> говорит сам — модель и числа '
                    + 'здесь ни при чём, их можно не трогать';
                return;
            }
            const defaults = (cast[idx] && cast[idx].model_defaults) || {};
            const parts = PARAM_KEYS.map(key => {
                const el = document.getElementById(key + '-' + idx);
                const raw = el ? el.value : '';
                const own = raw !== '';
                const value = own ? raw : (defaults[key] === undefined ? '—' : defaults[key]);
                return key + ' <strong>' + value + '</strong>'
                    + (own ? '' : ' <span style="color:#999;">(как в модели)</span>');
            });
            // Восемь параметров в одну строку не влезают: раскладываем по четыре
            const rows = [];
            for (let i = 0; i < parts.length; i += 4) rows.push(parts.slice(i, i + 4).join(' · '));
            box.innerHTML = '<strong>Уйдёт в модель:</strong><br>' + rows.join('<br>')
                + '<br>Пустое поле — параметр вообще не отправляется: действует значение из Modelfile модели.';
        }

        // «q1» и «q1:latest» — одна и та же модель
        function modelSupportsThinking(name) {
            if (!name || !thinkingModels.length) return false;
            const base = n => String(n).split(':')[0];
            return thinkingModels.some(m => base(m) === base(name));
        }

        // Характер просто заполняет поля — дальше числа можно править руками
        function applyPreset(idx) {
            const select = document.getElementById('preset-' + idx);
            const preset = characterInfo(select ? select.value : 'custom');
            const hint = document.getElementById('preset-hint-' + idx);
            if (hint) hint.textContent = preset.hint || '';

            Object.entries(preset.params || {}).forEach(([key, value]) => {
                const el = document.getElementById(key + '-' + idx);
                if (el) el.value = value;
            });

            // Набор задаёт и размышления, но «Свой» их не трогает; модель без
            // поддержки размышлений не получит think=true — показываем честно
            const thinkEl = document.getElementById('think-' + idx);                if (thinkEl && preset.think && preset.group !== 'manual') {
                    thinkEl.value = preset.think;
                    if (thinkEl.value === 'on' && thinkEl.dataset.supportsThinking === '0') {
                        thinkEl.value = 'off';
                    }
                }
                refreshParamStyles(idx);
                refreshEffective(idx);
        }

        // Поле с числом: заполненное — выделено, пустое — серое (параметр не отправляется).
        // Цвета заданы классами в CSS, чтобы тёмная сцена перекрашивала их вместе со всем
        function refreshParamStyles(idx) {
            PARAM_KEYS.forEach(key => {
                const el = document.getElementById(key + '-' + idx);
                if (el) el.classList.toggle('filled', el.value !== '');
            });
        }

        // Ручная правка числа: набор больше не подходит, помечаем «Свой»
        function onParamInput(idx) {
            const select = document.getElementById('preset-' + idx);
            if (select && select.value !== 'custom') {
                select.value = 'custom';
                const hint = document.getElementById('preset-hint-' + idx);
                if (hint) hint.textContent = CUSTOM_CHARACTER.hint;
            }
            refreshParamStyles(idx);
            refreshEffective(idx);
        }

        // Числа не мозолят глаза, пока их не спросят
        function toggleTuning(idx) {
            const box = document.getElementById('tuning-' + idx);
            const caret = document.getElementById('tuning-caret-' + idx);
            if (!box) return;
            const hidden = box.style.display === 'none';
            box.style.display = hidden ? 'block' : 'none';
            if (caret) caret.textContent = hidden ? ' ▾' : ' ▸';
        }

        // «🎲 Разбросать характеры»: новый случайный характер каждому ИИ-участнику.
        // Ту же лотерею сервер проводит сам при подъёме занавеса (RANDOMIZE_CHARACTERS).
        // Все равны — и роль тут не помеха, повторы разрешены
        function randomizeCharacters() {
            const keys = Object.keys(CHARACTERS).filter(k => k !== 'custom' && CHARACTERS[k].params
                && Object.keys(CHARACTERS[k].params).length);
            if (!keys.length) { alert('Список характеров не загружен — обновите страницу'); return; }
            cast.forEach((p, idx) => {
                if (p.model === 'human') return;
                const key = keys[Math.floor(Math.random() * keys.length)];
                const select = document.getElementById('preset-' + idx);
                if (select) select.value = key;
                const thinkEl = document.getElementById('think-' + idx);
                if (thinkEl && CHARACTERS[key].think) {
                    thinkEl.value = CHARACTERS[key].think === 'on' && thinkEl.dataset.supportsThinking === '0'
                        ? 'off' : CHARACTERS[key].think;
                }
                applyPreset(idx);
            });
        }

        // Состав — одна и та же форма и для настройки спектакля, и для правок на ходу
        // Выбор значения для селекта: «q1» и «q1:latest» — одна модель
        function selectIf(value, current) {
            return String(value) === String(current) ? 'selected' : '';
        }

        // Роли места. На сервере это два флага (модератор и судья друг друга
        // исключают), а в пульте — одно значение из списка: так режиссёру не надо
        // помнить, что бывший судья может остаться судьёй, став модератором
        const CAST_ROLES = [
            {value: 'participant', label: '🎭 Участник', badge: 'role-participant'},
            {value: 'moderator', label: '🎬 Модератор', badge: 'role-moderator'},
            {value: 'judge', label: '⚖️ Судья', badge: 'role-judge'},
        ];

        function roleOf(p) {
            if (p.role) return p.role;
            return p.is_moderator ? 'moderator' : (p.is_judge ? 'judge' : 'participant');
        }

        function roleInfo(role) {
            return CAST_ROLES.find(r => r.value === role) || CAST_ROLES[0];
        }

        // Та же роль целиком, что и на сервере: меняется место, а не флаг
        function setRoleLocally(p, role) {
            p.role = role;
            p.is_moderator = role === 'moderator';
            p.is_judge = role === 'judge';
        }

        // Опции роли — не выключатели, а положения: у каждой опции конечный набор
        // значений, и одно из них занято всегда. Пока они есть только у судьи,
        // и обе — про то, кто кого слышит: список должен совпадать с сервером,
        // за этим следит TestScenePanel.
        const ROLE_OPTIONS = {
            judge: {
                scope: [
                    {value: 'all', label: '⚖️ Всё обсуждение — текущий акт и прошлые'},
                    {value: 'act', label: '🎬 Только текущий акт'},
                ],
                publicity: [
                    {value: 'anonymous', label: '🤫 Анонимный — слышит только режиссёр'},
                    {value: 'public', label: '📣 Публичный — слышат все участники'},
                ],
            },
        };
        const ROLE_OPTION_DEFAULTS = {judge: {scope: 'all', publicity: 'anonymous'}};
        const ROLE_OPTION_NAMES = {scope: 'Что видит судья', publicity: 'Кто слышит судью'};
        const ROLE_OPTION_HINTS = {
            scope: 'Оценки за прошлые акты судья выносит заново каждый раз. «Только текущий акт» — судья оценивает лишь то, что сказано с его прошлого вердикта: это имеет смысл, когда в очереди есть кто-то до него, иначе оценивать ему нечего.',
            publicity: 'Анонимный судья говорит только для режиссёра: вердикт виден в ленте, но участники его не слышат — так спектакль игрался всегда. Публичный попадает в реплики остальных вместе с оценками: его можно похвалить, оспорить или обидеться.',
        };

        function roleOptionsOf(p) {
            const role = roleOf(p);
            const own = (p.role_options && typeof p.role_options === 'object') ? p.role_options : {};
            return Object.assign({}, ROLE_OPTION_DEFAULTS[role] || {}, own);
        }

        // Числа и имена, набранные в полях, живут в разметке, а перестановка и
        // удаление работают с массивом cast: перед ними переносим набранное
        // в массив — иначе перенос карточки стёр бы всё, что уже вписано,
        // но ещё не применено кнопкой «Применить состав»
        function syncCastFromForm() {
            const typed = collectCast();
            cast = cast.map((p, idx) => Object.assign({}, p, typed[idx]));
        }

        function moveCast(idx, delta) {
            const target = idx + delta;
            if (target < 0 || target >= cast.length) return;
            syncCastFromForm();
            cast.splice(target, 0, cast.splice(idx, 1)[0]);
            renderCastEditor();
        }

        function removeCast(idx) {
            if (cast.length <= 1) {
                alert('В составе должно остаться хотя бы одно место — иначе спектаклю некому играть');
                return;
            }
            syncCastFromForm();
            cast.splice(idx, 1);
            renderCastEditor();
            updateSidebarParticipants();
        }

        function setCastRole(idx, role) {
            syncCastFromForm();
            setRoleLocally(cast[idx], role);
            renderCastEditor();
            updateSidebarParticipants();
        }

        // Новое место придумывает сервер: у него те же списки имён, эмодзи
        // и профессий, что у жребия при подъёме занавеса
        function addCast() {
            fetch('/api/participants/draft', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({role: 'participant'})
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success || !data.participant) {
                    throw new Error(data.error || 'сервер не дал заготовку места');
                }
                syncCastFromForm();
                cast.push(data.participant);
                renderCastEditor();
                updateSidebarParticipants();
                // Место появляется в конце длинной сцены: показываем его,
                // иначе непонятно, сработала ли кнопка
                const card = document.querySelector('[data-participant-index="' + (cast.length - 1) + '"]');
                if (card) card.scrollIntoView({block: 'center', behavior: 'smooth'});
            })
            .catch(err => alert('❌ ' + err.message));
        }

        // Полный сброс: как первый запуск с пустой папкой экземпляра — состав,
        // общие правила, руководства модератора и правила судьи берутся из
        // settings.py, а сохранённый пульт забывается (иначе сброс пережил бы
        // только до перезапуска). Тема не трогается: она про сюжет, а не про
        // труппу, и придумывать её каждый сброс — работа, а не часть спектакля.
        function resetEverything() {
            if (!confirm('Полный сброс: состав, правила общения, руководства модератора и правила судьи вернутся к значениям из settings.py, а сохранённый пульт будет забыт.\\n\\nТема останется — её сброс не трогает.\\n\\nПродолжить?')) return;
            fetch('/api/settings/reset', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success) { alert('❌ ' + (data.error || 'не удалось сбросить пульт')); return; }
                cast = data.participants || [];
                renderCastEditor();
                updateSidebarParticipants();
                loadCast();
                // Поле темы не трогаем вовсе: на сервере она та же (сброс её
                // не касается), а в поле может лежать набранное, но ещё не
                // применённое — стирать чужой черновик незачем
                // Редактор показывает то, что сейчас на сервере: после сброса
                // в его полях должен быть заводской текст, а не старый
                loadInstructionsForEdit();
            })
            .catch(err => alert('❌ ' + err.message));
        }

        // Состав — одна и та же форма и для настройки спектакля, и для правок на ходу.
        // Параметры генерации видны сразу: у пустого поля подсказкой стоит значение из
        // OPTIONS, а под ними написано, что именно уйдёт в модель.
        function renderCastEditor() {
            const container = document.getElementById('castEditor');
            if (!cast.length) {
                container.innerHTML = '<div style="color:#666;font-style:italic;font-size:13px;">Состав пуст</div>';
                return;
            }
            container.innerHTML = cast.map((p, idx) => {
                const isHuman = p.model === 'human';
                const role = roleOf(p);
                const roleBadge = '<span class="role-badge ' + roleInfo(role).badge + '">'
                    + roleInfo(role).label + '</span>';
                // Рамка карточки показывает роль — тот же цвет, что у полосы судьи в ленте
                const borderColor = role === 'judge' ? '#7b1fa2' : '#000000';
                // Селекты роли и модели не тянутся на всю ширину, в отличие от полей
                const tightStyle = 'padding:6px;border:1px solid #000;font-family:Georgia,serif;font-size:13px;';

                const avatar = p.avatar_url
                    ? '<img src="' + escapeHtml(p.avatar_url) + '">'
                    : (p.avatar_emoji || '📣');
                const fieldLabel = text => '<label style="font-size:11px;text-transform:uppercase;letter-spacing:1px;">' + text + '</label>';
                const fieldStyle = 'width:100%;padding:7px;border:1px solid #000;font-family:Georgia,serif;font-size:14px;';

                // Модель есть у любого места, даже у живого участника: посадить
                // за стол человека («human») или вернуть ему модель — решение
                // режиссёра, и дверь должна открываться в обе стороны.
                // Поле — текст с подсказками (datalist), а не жёсткий список:
                // так вписать можно и облачную модель, и только что добавленную
                const modelField = fieldLabel(isHuman ? 'Модель — живой участник' : 'Модель')
                      + '<input type="text" id="model-' + idx + '" list="modelList"'
                      + ' value="' + escapeHtml(p.model || '') + '"'
                      + ' placeholder="— выберите модель — или впишите cloud:…"'
                      + ' style="' + fieldStyle + (isHuman ? 'background:#f3f3f3;' : '') + '"'
                      + ' onchange="refreshEffective(' + idx + ')"'
                      + ' title="Начните набирать — список отфильтруется. Облачную модель можно вписать целиком: cloud:вендор/модель'
                      + (cloudHint ? ' (облако: ' + cloudHint + ')' : '')
                      + '. «human» — живой участник: говорит сам, без модели и чисел'
                      + '">';

                const supportsThinking = modelSupportsThinking(p.model);
                const thinkValue = p.think || 'auto';
                const presetValue = CHARACTERS[p.preset] ? p.preset : 'custom';
                const defaults = p.model_defaults || {};

                // Одно числовое поле: пусто = параметр не отправляется вообще, и работает
                // значение из Modelfile модели. Подсказка сереньким показывает именно его,
                // даже если поле только что очистили
                // Класс filled, а не инлайновые цвета: так поле перекрашивается вместе
                // с темой (тёмная сцена иначе оставила бы чёрную рамку на чёрном)
                const paramField = (key, label, step, min, max, hint) => {
                    const value = (p[key] === undefined || p[key] === null) ? '' : p[key];
                    const fallback = defaults[key] === undefined ? 'как в модели' : defaults[key];
                    const range = (min === null ? '' : ' min="' + min + '"') + (max === null ? '' : ' max="' + max + '"');
                    return '<div>' + fieldLabel(label)
                        + '<input type="number" class="param-input' + (value === '' ? '' : ' filled') + '"'
                        + ' id="' + key + '-' + idx + '" step="' + step + '"' + range
                        + ' value="' + value + '" placeholder="' + fallback + '" title="' + hint + '"'
                        + ' oninput="onParamInput(' + idx + ')" style="width:100%;padding:6px;font-size:13px;">'
                        + '</div>';
                };

                // Список характеров приходит с сервера; сгруппирован, чтобы сразу
                // было видно, где «ровные» наборы, а где с перекосом
                const characterSelect = (() => {
                    const keys = Object.keys(CHARACTERS);
                    if (!keys.length) {
                        return '<option value="custom" selected>' + escapeHtml(CUSTOM_CHARACTER.label) + '</option>';
                    }
                    const groups = {};
                    ['custom'].concat(keys.filter(k => k !== 'custom')).forEach(key => {
                        const info = characterInfo(key);
                        const group = info.group || 'balanced';
                        (groups[group] = groups[group] || []).push(
                            '<option value="' + key + '" ' + selectIf(key, presetValue) + '>' + escapeHtml(info.label) + '</option>');
                    });
                    return Object.keys(groups).map(group =>
                        '<optgroup label="' + escapeHtml(CHARACTER_GROUPS[group] || group) + '">'
                        + groups[group].join('') + '</optgroup>').join('');
                })();

                const paramsBlock = isHuman ? '' : ''
                    + '<div style="margin-top:14px;padding-top:12px;border-top:1px dotted #cccccc;">'
                    +   '<div style="display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap;margin-bottom:12px;">'
                    +     '<div style="min-width:230px;">' + fieldLabel('Характер')
                    +       '<select id="preset-' + idx + '" onchange="applyPreset(' + idx + ')" style="' + fieldStyle + '"'
                    +         ' title="Готовый набор параметров генерации. Числа управляют тем, КАК участник говорит, а что он говорит — задаёт его личная инструкция. На новый спектакль характер разыгрывается случайно.">'
                    +         characterSelect
                    +       '</select>'
                    +     '</div>'
                    +     '<div style="min-width:220px;">' + fieldLabel('Размышления')
                    +       '<select id="think-' + idx + '" data-supports-thinking="' + (supportsThinking ? 1 : 0) + '" style="' + fieldStyle + '"'
                    +         ' title="Скрытое рассуждение модели перед ответом. Умеют не все модели — у остальных этот режим недоступен.">'
                    +         '<option value="auto" ' + selectIf('auto', thinkValue) + '>Авто (как в ENABLE_THINKING)</option>'
                    +         '<option value="off" ' + selectIf('off', thinkValue) + '>Выключены — отвечает сразу</option>'
                    +         '<option value="on" ' + selectIf('on', thinkValue) + (supportsThinking ? '' : ' disabled') + '>Включены — сначала думает</option>'
                    +       '</select>'
                    +     '</div>'
                    +     '<div id="preset-hint-' + idx + '" style="font-size:11px;color:#666;max-width:260px;padding-top:20px;line-height:1.5;">'
                    +       escapeHtml(characterInfo(presetValue).hint || '') + (supportsThinking ? '' : '<br>размышления этой модели недоступны')
                    +     '</div>'
                    +   '</div>'
                    // Числа спрятаны: в настройке они только мешают, а строка
                    // «Уйдёт в модель» ниже и так показывает, что уйдёт в Ollama
                    +   '<button class="btn btn-secondary" onclick="toggleTuning(' + idx + ')" style="padding:4px 12px;font-size:12px;margin:0 0 10px 0;">'
                    +     '⚙ Тонкая настройка<span id="tuning-caret-' + idx + '"> ▸</span></button>'
                    +   '<div id="tuning-' + idx + '" style="display:none;">'
                    +     '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;">'
                    +       paramField('temperature', 'Температура', '0.1', 0, 2,
                                'Насколько свободно выбираются слова: 0-0.3 предсказуемо, 0.8-1.2 живая речь, выше 1.5 текст рассыпается. Выше 2 не принимается.')
                    +       paramField('top_p', 'top_p', '0.05', 0, 1,
                                'Отсекает маловероятные слова: меньше — предсказуемее. Делает почти то же, что температура, поэтому крутить надо что-то одно.')
                    +       paramField('min_p', 'min_p', '0.01', 0, 1,
                                'Оставляет слова не ниже доли от самого вероятного: порог сам подстраивается под уверенность модели. 0.05 — лёгкая чистка, 0.2-0.3 — заметно строже. Единственный способ убрать мусор, не жертвуя высокой температурой.')
                    +       paramField('top_k', 'top_k', '1', 1, null,
                                'Сколько слов-кандидатов вообще рассматривать: 40 — как у большинства моделей, меньше 20 — заметно предсказуемее, больше 100 почти ничего не меняет.')
                    +       paramField('repeat_penalty', 'repeat_penalty', '0.05', 0, null,
                                'Штраф за повторы: 1.1-1.3 спасает от зацикливания, выше 1.6 ломает грамматику.')
                    +       paramField('presence_penalty', 'presence_penalty', '0.1', 0, null,
                                'Подталкивает к новым темам, а не к пересказу сказанного: 0.3-0.6.')
                    +       paramField('frequency_penalty', 'frequency_penalty', '0.1', 0, null,
                                'Режет частые слова, мягче чем repeat_penalty: 0.3-0.6.')
                    +       paramField('seed', 'seed', '1', null, null,
                                'Одно и то же число — один и тот же ответ при том же диалоге. Это не характер, а повторяемость: удобно сравнивать две модели на одной теме или вернуться к странной реплике. Пусто — каждый спектакль новый.')
                    +     '</div>'
                    +   '</div>'
                    // Содержимое дособерёт refreshEffective(idx) ниже: так строка
                    // не разойдётся с подсказками в самих полях
                    +   '<div id="effective-' + idx + '" style="font-size:11px;color:#666;margin-top:9px;line-height:1.6;"></div>'
                    + '</div>';

                // Опции роли — не числа и не характер: это «кто кого слышит», и они
                // есть только у тех ролей, где такие положения вообще бывают. Значение
                // по умолчанию показано выбранным, поэтому сразу видно, как роль ведёт
                // себя, если ничего не менять
                const roleOptionKeys = Object.keys(ROLE_OPTIONS[role] || {});
                const currentOptions = roleOptionsOf(p);
                const roleOptionsBlock = roleOptionKeys.length === 0 ? '' : ''
                    + '<div style="margin-top:14px;padding-top:12px;border-top:1px dotted #cccccc;">'
                    +   '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;">'
                    +     roleOptionKeys.map(key => {
                              const choices = ROLE_OPTIONS[role][key];
                              return '<div>' + fieldLabel(ROLE_OPTION_NAMES[key] || key)
                                + '<select id="' + key + '-' + idx + '" style="' + fieldStyle + '"'
                                +   ' title="' + escapeHtml(ROLE_OPTION_HINTS[key] || '') + '">'
                                +   choices.map(choice =>
                                      '<option value="' + choice.value + '" '
                                      + selectIf(choice.value, currentOptions[key]) + '>'
                                      + escapeHtml(choice.label) + '</option>').join('')
                                + '</select>'
                                + '<div style="font-size:11px;color:#666;margin-top:6px;line-height:1.5;">'
                                +   escapeHtml(ROLE_OPTION_HINTS[key] || '') + '</div>'
                                + '</div>';
                          }).join('')
                    +   '</div>'
                    + '</div>';

                return ''
                + '<div class="index-card" data-participant-index="' + idx + '" style="border:1px solid ' + borderColor + ';">'
                +   '<div style="display:flex;gap:15px;align-items:flex-start;">'
                +     '<div style="flex-shrink:0;">'
                +       '<div class="avatar-preview" id="avatar-preview-' + idx + '" style="width:96px;height:96px;font-size:46px;" onclick="openAvatarModal(' + idx + ')" title="Показать аватар целиком">' + avatar + '</div>'
                +     '</div>'
                +     '<div style="flex:1;min-width:0;">'
                +       '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;">'
                +         '<div>' + fieldLabel('Имя')
                +           '<input type="text" id="name-' + idx + '" value="' + escapeHtml(p.display_name || '') + '" placeholder="Введите имя" style="width:100%;padding:8px;border:1px solid #000;font-family:Georgia,serif;font-size:16px;">'
                +         '</div>'
                +         '<div>' + fieldLabel('Пол (влияет только на аватар)')
                +           '<select id="gender-' + idx + '" style="' + fieldStyle + '">'
                +             '<option value="male" ' + selectIf('male', p.gender) + '>♂ Мужской</option>'
                +             '<option value="female" ' + selectIf('female', p.gender) + '>♀ Женский</option>'
                +           '</select>'
                +         '</div>'
                +         '<div>' + modelField + '</div>'
                +       '</div>'
                // Ключевые слова и поиск аватара стоят одной строкой рядом с аватаром:
                // раньше кнопка была в подвале карточки, и её приходилось искать
                +       '<div style="margin-top:12px;">' + fieldLabel('Ключевые слова для аватара')
                +         '<div class="keyword-row">'
                +           '<input type="text" id="keywords-' + idx + '" value="' + escapeHtml(p.avatar_keywords || '') + '" placeholder="Например: дипломат женщина" style="' + fieldStyle + '">'
                +           '<button class="btn btn-secondary" id="search-avatar-' + idx + '" onclick="searchAvatar(' + idx + ')" style="padding:0 16px;margin:0;font-size:13px;white-space:nowrap;" title="Найти картинку по этим словам">🔍 Найти аватар</button>'
                +         '</div>'
                +       '</div>'
                +       paramsBlock
                +     '</div>'
                +   '</div>'
                +   roleOptionsBlock
                // Роль и очередь правятся прямо в карточке: верхняя карточка —
                // та, чья реплика прозвучит первой
                +   '<div style="margin-top:14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;">'
                +     roleBadge
                +     '<select id="role-' + idx + '" onchange="setCastRole(' + idx + ', this.value)" style="' + tightStyle + '"'
                +       ' title="Роль места: участник говорит по очереди, модератор ведёт обсуждение, судья оценивает в конце. Роль одна — назначив судью, прежняя снимается">'
                +       CAST_ROLES.map(r => '<option value="' + r.value + '" ' + selectIf(r.value, role) + '>'
                                      + escapeHtml(r.label) + '</option>').join('')
                +     '</select>'
                +     '<span style="margin-left:auto;display:flex;gap:6px;align-items:center;">'
                +       '<button class="btn btn-secondary" onclick="moveCast(' + idx + ', -1)" ' + (idx === 0 ? 'disabled' : '') + ' style="padding:4px 12px;margin:0;font-size:14px;" title="Раньше в очереди реплик">↑</button>'
                +       '<button class="btn btn-secondary" onclick="moveCast(' + idx + ', 1)" ' + (idx === cast.length - 1 ? 'disabled' : '') + ' style="padding:4px 12px;margin:0;font-size:14px;" title="Позже в очереди реплик">↓</button>'
                +       '<button class="btn btn-secondary" onclick="removeCast(' + idx + ')" ' + (cast.length <= 1 ? 'disabled' : '') + ' style="padding:4px 12px;margin:0;font-size:14px;" title="Убрать это место из состава">🗑</button>'
                +     '</span>'
                +   '</div>'
                + '</div>';
            }).join('');
            // У людей параметров нет, у моделей строка «Уйдёт в модель» собирается по полям
            cast.forEach((p, idx) => { if (p.model !== 'human') refreshEffective(idx); });
            renderModelSuggestions();
        }
        
        // Подсказки для поля модели — одни на весь состав. Поле теперь текст,
        // а не строгий список: так можно вписать модель, которой в подсказках нет
        // (например, только что добавленную на шлюзе). Облачные помечаем ☁️ —
        // по значку видно, что реплика уйдёт в интернет.
        function renderModelSuggestions() {
            const box = document.getElementById('modelList');
            if (!box) return;
            // «Живой участник» — такая же модель места, как любая другая: без этой
            // строки посадить за стол человека можно было только из файла настроек
            const option = name => '<option value="' + escapeHtml(name) + '">'
                + (name === 'human' ? '🧑 Живой участник — говорит сам'
                    : escapeHtml(cloudModels.includes(name) ? '☁️ ' + name : name)) + '</option>';
            const names = ['human'].concat(models, cloudModels);
            // Модель из PARTICIPANTS может быть с тегом: показываем и её, даже
            // если такого имени в списках нет
            cast.forEach(p => {
                if (p.model && p.model !== 'human' && !names.includes(p.model)) names.unshift(p.model);
            });
            box.innerHTML = names.map(option).join('');
        }
        
        function collectCast() {
            return cast.map((p, idx) => {
                const pick = (id, fallback) => { const el = document.getElementById(id); return el ? el.value : fallback; };
                const entry = {
                    // cast_id — это и есть место: по нему сервер понимает, кого
                    // переставили, кого убрали, а кого только что добавили
                    // (без него место считалось бы новым и получило бы новое имя)
                    cast_id: p.cast_id || '',
                    role: roleOf(p),
                    display_name: pick(`name-${idx}`, p.display_name).trim(),
                    gender: pick(`gender-${idx}`, p.gender),
                    avatar_keywords: pick(`keywords-${idx}`, p.avatar_keywords),
                    avatar_emoji: p.avatar_emoji,
                    avatar_url: p.avatar_url || null,
                };
                // Модель отправляем всегда, и «human» тоже: молчание клиента сервер
                // читал как «модель не менялась» — и место, которое режиссёр сделал
                // живым, молча возвращалось из модели прежней
                entry.model = String(pick(`model-${idx}`, p.model) || '').trim();
                if (p.model !== 'human') {
                    // Пустая строка = «как в OPTIONS»: сервер убирает такое поле у участника.
                    // Поля нет в пульте — не отправляем ничего, чтобы не стереть число
                    // из PARTICIPANTS нечаянно
                    PARAM_KEYS.forEach(key => {
                        const el = document.getElementById(`${key}-${idx}`);
                        if (!el) return;
                        entry[key] = el.value === '' ? null : el.value;
                    });
                    entry.think = pick(`think-${idx}`, p.think || 'auto');
                    entry.preset = pick(`preset-${idx}`, p.preset || 'custom');
                }
                // Опции роли — только те, что есть у нынешней роли: если место
                // перестало быть судьёй, чужих опций с собой не унесёт
                const optionKeys = Object.keys(ROLE_OPTIONS[roleOf(p)] || {});
                if (optionKeys.length) {
                    const current = roleOptionsOf(p);
                    entry.role_options = {};
                    optionKeys.forEach(key => {
                        const el = document.getElementById(`${key}-${idx}`);
                        entry.role_options[key] = el ? el.value : current[key];
                    });
                }
                return entry;
            });
        }
        
        function saveCast() {
            fetch('/api/participants', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({participants: collectCast()})
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success) { alert('❌ ' + (data.error ||'не удалось применить состав')); return; }
                cast = data.participants || cast;
                renderCastEditor();
                updateSidebarParticipants();
                loadCast();   // заодно обновляем проверки моделей и видеопамяти
            })
            .catch(err => { console.error('Ошибка правки состава:', err); alert('❌ ' + err.message); });
        }
        
        function renderModelsWarning(status) {
            const box = document.getElementById('modelsWarning');
            if (!box) return;
            const missing = status && status.missing ? status.missing : [];
            if (!status || (status.ok && missing.length === 0)) {
                box.style.display = 'none';
                box.innerHTML = '';
                return;
            }
            // Текст собирает сервер: причина бывает и местная, и облачная, и две
            // копии этой формулировки неизбежно разошлись бы
            const text = status.message
                || `В моделях что-то не так: ${missing.join(', ')}`;
            box.textContent = '⚠️ ' + text;
            box.style.display = 'block';
        }
        
        function searchAvatar(idx) {
            const keywords = (document.getElementById(`keywords-${idx}`)?.value || '').trim()
                || (cast[idx] ? cast[idx].display_name : '');
            if (!keywords) { alert('Сначала введите имя или ключевые слова для аватара'); return; }
            
            // Блокируем кнопку во время загрузки (ищем её по id: кнопка больше
            // не единственный элемент в строке с ключевыми словами)
            const btn = document.getElementById('search-avatar-' + idx) || event.target;
            if (btn.disabled) return;
            btn.disabled = true;
            btn.textContent = '⏳ Поиск...';
            
            const preview = document.getElementById(`avatar-preview-${idx}`);
            const fallbackEmoji = (cast[idx] && cast[idx].avatar_emoji) || '📣';
            preview.innerHTML = '⏳';
            
            fetch(`/api/avatar/${encodeURIComponent(keywords)}`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ keywords: keywords, participant_idx: idx })
            }).then(r => r.json()).then(data => {
                if (data.avatar_url) {
                    cast[idx].avatar_url = data.avatar_url;
                    preview.innerHTML = `<img src="${escapeHtml(data.avatar_url)}">`;
                } else {
                    preview.innerHTML = '❌';
                    setTimeout(() => preview.innerHTML = fallbackEmoji, 2000);
                }
                btn.disabled = false;
                btn.textContent = '🔍 Найти аватар';
            }).catch(err => { console.error('Ошибка поиска аватара:', err); preview.innerHTML = '❌'; setTimeout(() => preview.innerHTML = fallbackEmoji, 2000); btn.disabled = false; btn.textContent = '🔍 Найти аватар'; });
        }
        
        // Клик по аватару в составе: у картинки — полный размер, у эмодзи —
        // набор значков. Это и есть действие по умолчанию для эмодзи-аватара:
        // смешное лицо хочется поменять именно там, где на него смотришь
        function openAvatarModal(idx) {
            const u = cast[idx] && cast[idx].avatar_url;
            if (u) {
                document.getElementById('avatarModalImg').src = u;
                document.getElementById('avatarModal').style.display = 'block';
                return;
            }
            openEmojiPicker({index: idx});
        }
        function closeAvatarModal() { document.getElementById('avatarModal').style.display = 'none'; }

        // ── Меню эмодзи-аватара ──────────────────────────────────────────
        // Клик по эмодзи (в ленте или в составе) открывает набор значков.
        // В составе выбор только правит форму — применит его кнопка «Применить
        // состав». В ленте выбор уходит на сервер сразу: там идёт спектакль,
        // и «поменял, но не применил» значило бы, что лицо не сменилось.
        let emojiSpot = null;

        function emojiChoices(gender) {
            const own = gender === 'female' ? EMOJIS.female : EMOJIS.male;
            return [...new Set([...(own || []), ...(EMOJIS.neutral || [])])];
        }

        function openEmojiPicker(spot) {
            const person = typeof spot.index === 'number'
                ? cast[spot.index]
                : cast.find(p => p.display_name === spot.name);
            const name = (person && person.display_name) || spot.name || '';
            emojiSpot = person ? {index: cast.indexOf(person), name: name} : {name: name};
            const choices = emojiChoices(person && person.gender);
            const current = (person && person.avatar_emoji) || '';
            document.getElementById('emojiMenuTitle').textContent =
                choices.length ? `Аватар-эмодзи: ${name}` : `Набор эмодзи пуст — ${name}`;
            document.getElementById('emojiMenuGrid').innerHTML = choices.map(one =>
                `<div class="emoji-choice${one === current ? ' current' : ''}" data-emoji="${one}" title="${one}">${one}</div>`
            ).join('');
            document.getElementById('emojiModal').style.display = 'block';
        }

        function closeEmojiPicker() {
            emojiSpot = null;
            document.getElementById('emojiModal').style.display = 'none';
        }

        function chooseEmoji(emoji) {
            const spot = emojiSpot;
            if (!spot) return;
            const person = spot.name ? cast.find(p => p.display_name === spot.name) : null;
            if (person) person.avatar_emoji = emoji;
            // В составе — только форма: спектакль поменяет состав целиком,
            // и сохранится всё разом
            if (typeof spot.index === 'number' && !runningShow()) {
                closeEmojiPicker();
                renderCastEditor();
                return;
            }
            // Из ленты (и по ходу спектакля) — сразу на сервер
            fetch('/api/participant/emoji', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: spot.name, emoji: emoji})
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success) { alert('⚠️ ' + (data.error || 'не удалось сменить аватар')); return; }
                if (data.participants) cast = data.participants;
                if (typeof spot.index === 'number') renderCastEditor();
                else refreshAvatars(spot.name, emoji);
                updateSidebarParticipants();
            })
            .catch(err => alert('⚠️ ' + err.message))
            .finally(() => closeEmojiPicker());
        }

        function runningShow() {
            return debateRunning && !showFinished;
        }

        // Смена лица у всех реплик участника: реплики в ленте перерисовывать
        // незачем — они уже нарисованы, а лицо у них одно и то же
        function refreshAvatars(name, emoji) {
            document.querySelectorAll('[data-emoji-for]').forEach(spot => {
                if (spot.dataset.emojiFor === name) spot.textContent = emoji;
            });
        }
        
        // Тема в шапке: пустой рамки с прочерком быть не должно — пока темы нет,
        // блока просто не видно. Тема, применённая раньше, остаётся на месте,
        // даже если поле ввода потом очистили
        function setTopicDisplay(text) {
            const box = document.getElementById('topicDisplay');
            if (!box) return;
            const clean = String(text === undefined || text === null ? '' : text).trim();
            box.textContent = clean;
            box.style.display = clean ? 'block' : 'none';
        }

        // Тема: одна кнопка на обе стадии — и в настройке, и на ходу режиссёра
        function applyTopic() {
            const input = document.getElementById('topicInput');
            const topic = input.value.trim();
            if (!topic) { alert('Введите тему'); return; }
            
            fetch('/api/moderator/topic', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({topic: topic}) })
                .then(r => r.json())
                .then(data => {
                    if (!data.success) { alert('❌ ' + (data.error || 'не удалось сменить тему')); return; }
                    input.value = data.topic;
                    setTopicDisplay(data.topic);
                })
                .catch(err => { console.error('Ошибка смены темы:', err); alert('❌ ' + err.message); });
        }
        
        function startDebate() {
            // Пустое поле — не ошибка: тема могла быть применена раньше и жить на сервере.
            // Пересылаем её только если поле заполнено, иначе берём ту, что уже есть
            const topic = document.getElementById('topicInput').value.trim();
            document.getElementById('startBtn').disabled = true;
            
            // Сначала отправляем правки из формы, потом стартуем: состав живёт на сервере
            fetch('/api/participants', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({participants: collectCast()})
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success) throw new Error(data.error || 'не удалось применить состав');
                cast = data.participants || cast;
                return fetch('/api/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(topic ? {topic: topic} : {}) });
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success) { showStartError(data.error || 'неизвестная'); return; }
                if (data.session_id) mySessionId = data.session_id;
                debateRunning = true;
                showFinished = false;
                finishRequested = false;
                if (data.resumed) {
                    // Спектакль доигрывается: прежние реплики уже в ленте, и стирать
                    // их значило бы начать с чистого листа после согласия продолжить
                    // (см. show.start_show)
                    lastPostCount = data.total_posts || 0;
                } else {
                    lastPostCount = 0;
                    document.getElementById('posts').innerHTML = '';
                }
                // Тему в шапке берём из ответа сервера: он знает, с какой играет
                setTopicDisplay(data.topic || topic);
                // Спектакль пошёл: убираем баннер с прошлой неудачной попытки
                const box = document.getElementById('modelsWarning');
                if (box) { box.style.display = 'none'; box.innerHTML = ''; }
                startPolling();
                renderCastEditor();
                updateSidebarParticipants();
                updatePanel();
            })
            .catch(err => { console.error('Ошибка запуска:', err); showStartError(err.message); });
        }
        
        function showStartError(message) {
            debateRunning = false;
            document.getElementById('startBtn').disabled = false;
            const box = document.getElementById('modelsWarning');
            if (box) { box.innerHTML = `⚠️ Не удалось начать спектакль: ${escapeHtml(message)}`; box.style.display = 'block'; }
            updatePanel();
            alert('Не удалось начать спектакль: ' + message);
        }
        
        // «Новый спектакль»: сервер собирает новый состав, настройки роли
        // и тема остаются — сброс темы это полный сброс, а не новый состав
        function newShow() {
            stopPolling();
            debateRunning = false;
            showFinished = false;
            finishRequested = false;
            lastPostCount = 0;
            mySessionId = null;
            document.getElementById('posts').innerHTML = '';
            document.getElementById('statusBar').style.display = 'none';
            document.getElementById('statusPlaceholder').style.display = 'block';
            setTurnState('hidden');
            fetch('/api/reset', {method: 'POST'})
                .then(r => r.json())
                .then(data => {
                    if (data.participants) cast = data.participants;
                    // Тему ставим ту, что осталась на сервере: её могли набрать
                    // в поле, но ещё не применить — тогда в поле останется своё
                    if (typeof data.topic === 'string' && data.topic.trim()) {
                        document.getElementById('topicInput').value = data.topic;
                        setTopicDisplay(data.topic);
                    }
                    return loadCast();
                })
                .then(() => { updateSidebarParticipants(); updatePanel(); })
                .catch(err => console.error('Ошибка сброса:', err));
        }
        
        // Кнопки пульта: до спектакля — настройка, во время — остановка, после занавеса —
        // новый спектакль. «Завершить» доступна всё время спектакля: режиссёру не нужно
        // ждать своей очереди, чтобы остановить действие.
        function updatePanel() {
            const startBtn = document.getElementById('startBtn');
            const finishBtn = document.getElementById('finishBtn');
            const newBtn = document.getElementById('newBtn');
            const title = document.getElementById('controlPanelTitle');
            if (debateRunning && !showFinished) {
                startBtn.style.display = 'none';
                newBtn.style.display = 'none';
                // Пока сервер не подтвердил занавес, повторно не показываем: иначе
                // кнопка мелькала бы обратно, пока модель доигрывает реплику
                finishBtn.style.display = finishRequested ? 'none' : 'inline-block';
                title.textContent = 'Режиссёрский пульт — спектакль идёт';
            } else if (showFinished) {
                startBtn.style.display = 'none';
                newBtn.style.display = 'inline-block';
                finishBtn.style.display = 'none';
                title.textContent = 'Режиссёрский пульт — занавес';
            } else {
                startBtn.style.display = 'inline-block';
                startBtn.disabled = false;
                newBtn.style.display = 'none';
                finishBtn.style.display = 'none';
                title.textContent = 'Режиссёрский пульт — настройка';
            }
            syncSectionsToPhase();
        }

        // ── Сворачивание разделов пульта ─────────────────────────────────
        // Разметка разделов остаётся плоской: обёртку и каретку дописывает этот код,
        // чтобы не расписывать одно и то же в каждом разделе
        function decoratePanelSections() {
            document.querySelectorAll('#controlPanel .panel-section').forEach((section, i) => {
                if (section.dataset.collapsible) return;
                const heading = section.querySelector('.panel-heading');
                if (!heading) return;
                section.dataset.collapsible = '1';
                section.dataset.sectionKey = section.id || ('section-' + i);
                const body = document.createElement('div');
                body.className = 'panel-body';
                Array.from(section.children).forEach(child => {
                    if (child !== heading) body.appendChild(child);
                });
                section.appendChild(body);
                const caret = document.createElement('span');
                caret.className = 'caret';
                caret.textContent = '▾';
                heading.appendChild(caret);
                heading.title = 'Свернуть / развернуть раздел';
                heading.addEventListener('click', () => setSectionCollapsed(
                    section.dataset.sectionKey, !body.classList.contains('collapsed')));
            });
        }

        function setSectionCollapsed(key, collapsed) {
            const section = document.querySelector('[data-section-key="' + key + '"]');
            if (!section) return;
            const body = section.querySelector('.panel-body');
            const caret = section.querySelector('.panel-heading .caret');
            if (!body) return;
            body.classList.toggle('collapsed', !!collapsed);
            if (caret) caret.textContent = collapsed ? '▸' : '▾';
            // Надпись на кнопке всего пульта зависит от того, что открыто сейчас
            syncSectionsToggle();
        }

        // Свернуть или развернуть все разделы пульта разом
        function setAllSectionsCollapsed(collapsed) {
            document.querySelectorAll('#controlPanel .panel-section').forEach(section => {
                if (section.dataset.sectionKey) setSectionCollapsed(section.dataset.sectionKey, collapsed);
            });
        }

        // Фазы пульта. В настройке раскрыто всё — там всё и нужно. С началом
        // спектакля пульт сворачивается целиком: режиссёр смотрит спектакль, а не
        // настройки, и раздел открывается только тогда, когда в нём что-то важное
        // (предупреждение готовности, ваш ход) или сам режиссёр его открыл.
        // После занавеса — тоже заголовки. Ручное сворачивание не трогаем:
        // панель реагирует только на смену фазы
        function syncSectionsToPhase() {
            const phase = showFinished ? 'finished' : (debateRunning ? 'running' : 'setup');
            if (phase === sectionsPhase) return;
            sectionsPhase = phase;
            setAllSectionsCollapsed(phase !== 'setup');
            // Ход в новой фазе пересчитываем заново: иначе после сворачивания
            // раздел «Ваша реплика» не раскрылся бы на своём ходу
            turnSectionState = null;
            syncSectionsToggle();
        }

        // Разделы, которые раскрываются сами, когда в них есть дело:
        // «Ваша реплика» — когда ход ваш (в спектакле она была бы большим полем
        // ввода посреди чужого разговора), и «Готовность» — когда есть о чём
        // предупредить (см. syncReadinessSection)
        function syncTurnSection(state) {
            if (!debateRunning || showFinished) return;
            if (state === turnSectionState) return;
            turnSectionState = state;
            setSectionCollapsed('turnSection', state !== 'your');
        }

        function sectionBodies() {
            return Array.from(document.querySelectorAll('#controlPanel .panel-section .panel-body'));
        }

        function everySectionCollapsed() {
            const bodies = sectionBodies();
            return bodies.length > 0 && bodies.every(body => body.classList.contains('collapsed'));
        }

        // Кнопка «развернуть/свернуть пульт»: без неё до кнопки «Завершить
        // спектакль» в свёрнутом разделе 05 пришлось бы добираться заголовками
        function syncSectionsToggle() {
            const btn = document.getElementById('sectionsToggle');
            if (!btn) return;
            btn.style.display = sectionsPhase === 'setup' ? 'none' : 'inline-block';
            btn.textContent = everySectionCollapsed() ? '▾ Развернуть пульт' : '▴ Свернуть пульт';
        }

        function toggleAllSections() {
            // Развёрнутых нет — раскрываем всё, и наоборот: одна кнопка на оба хода
            const collapse = !everySectionCollapsed();
            setAllSectionsCollapsed(collapse);
            syncSectionsToggle();
        }

        // Как назвать роль в интерфейсе: у обычного участника никакой особой роли нет
        function roleLabelOf(role) {
            return role === 'moderator' ? 'модератор' : (role === 'judge' ? 'судья' : '');
        }

        // Раздел «Ваша реплика» всегда на месте: без него нумерация блоков прыгала
        // с 03 на 05. Меняется только содержимое — поле ввода или пояснение
        function setTurnState(state, who, roleName) {
            const composer = document.getElementById('turnComposer');
            const note = document.getElementById('turnNote');
            const title = document.getElementById('turnTitle');
            if (!composer || !note || !title) return;
            if (state === 'your') {
                composer.style.display = 'block';
                note.style.display = 'none';
                title.textContent = 'Ход: ' + (who || 'вы') + (roleName ? ' · ' + roleName : '');
                syncTurnSection(state);
                return;
            }
            syncTurnSection(state);
            composer.style.display = 'none';
            note.style.display = 'block';
            title.textContent = 'Ваша реплика';
            note.textContent = state === 'waiting'
                ? 'Сейчас не ваше время выступать: говорит ' + (who || 'другой участник') + '.'
                : state === 'finished' ? 'Занавес: реплики закончились.'
                : state === 'sent' ? 'Реплика отправлена — ждём ответа других участников.'
                : 'Спектакль ещё не начат — поле появится, когда очередь дойдёт до вас.';
        }

        // Реплики из любого канала — сокет или редкий опрос — рисуются одинаково.
        // Пост с уже показанным номером пропускаем: сокет и опрос могут сработать
        // почти одновременно, и без этого одна реплика появилась бы дважды
        function addNewPosts(posts) {
            (posts || []).forEach(post => {
                if (!post) return;
                if (post.id && post.id <= lastPostCount) return;
                addPost(post);
                if (post.id) lastPostCount = post.id;
            });
        }

        // Раздел 00: содержимое рисуют renderModelsWarning / renderVramWarning,
        // а здесь решается, раскрыт ли раздел, и ставится метка в заголовке
        let readySignature = null;
        function syncReadinessSection() {
            const shown = el => !!(el && el.style.display !== 'none' && el.innerHTML.trim() !== '');
            const models = document.getElementById('modelsWarning');
            const vram = document.getElementById('vramWarning');
            const okBox = document.getElementById('readyOk');
            const badge = document.getElementById('readyBadge');
            const problems = [];
            if (shown(models)) problems.push('модели');
            if (shown(vram)) problems.push('видеопамять');
            if (okBox) okBox.style.display = problems.length ? 'none' : 'block';
            if (badge) badge.textContent = problems.length ? '⚠️ ' + problems.length : '';
            const signature = problems.join(',');
            // Раскрываем, когда появилось о чём предупредить, и сворачиваем, когда
            // всё в порядке. Пока набор предупреждений тот же — раздел не трогаем:
            // иначе он не давал бы свернуть себя руками
            if (signature === readySignature) return;
            readySignature = signature;
            setSectionCollapsed('sec-ready', problems.length === 0);
        }

        // Ручная проверка готовности — не трогая то, что уже введено в форме
        function checkReadiness() {
            const btn = document.querySelector('#sec-ready button');
            if (btn) { btn.disabled = true; btn.textContent = '⏳ Проверяю...'; }
            fetch('/api/participants', {cache: 'no-store'})
                .then(r => r.json())
                .then(data => {
                    renderModelsWarning(data.models_status);
                    renderVramWarning(data.vram_status);
                    syncReadinessSection();
                    refreshMemory();
                })
                .catch(err => console.warn('Проверка готовности не удалась:', err))
                .finally(() => {
                    if (btn) { btn.disabled = false; btn.textContent = '🔄 Проверить сейчас'; }
                });
        }

        // Тёмная сцена: выбор запоминается, при первом входе берётся из настроек системы
        function applyTheme(dark) {
            document.body.classList.toggle('dark', !!dark);
            const btn = document.getElementById('themeBtn');
            if (btn) btn.textContent = dark ? '☀️ Светлая сцена' : '🌙 Тёмная сцена';
        }

        function toggleTheme() {
            const dark = !document.body.classList.contains('dark');
            applyTheme(dark);
            try { localStorage.setItem('theatreTheme', dark ? 'dark' : 'light'); } catch (e) {}
        }

        // Цветные полосы ролей в ленте: оформление, а не смысл, поэтому его можно
        // выключить. Класс стоит на самом теле страницы — оформление постов тогда
        // возвращается к прежнему виду, без правок разметки. Выбор запоминается
        function applyRoleMarks(on) {
            document.body.classList.toggle('role-marks', !!on);
            const btn = document.getElementById('rolesBtn');
            if (btn) btn.textContent = on ? '🎨 Цвет ролей: вкл' : '🎨 Цвет ролей: выкл';
        }

        function toggleRoleMarks() {
            const on = !document.body.classList.contains('role-marks');
            applyRoleMarks(on);
            try { localStorage.setItem('theatreRoleMarks', on ? 'on' : 'off'); } catch (e) {}
        }

        (function initRoleMarks() {
            let saved = null;
            try { saved = localStorage.getItem('theatreRoleMarks'); } catch (e) {}
            // По умолчанию включено: это то, что режиссёр и так различает глазами
            applyRoleMarks(saved === null ? true : saved === 'on');
        })();

        (function initTheme() {
            let saved = null;
            try { saved = localStorage.getItem('theatreTheme'); } catch (e) {}
            const systemDark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
            applyTheme(saved === null ? systemDark : saved === 'dark');
        })();
        
        // Голова реплики — общая у готового поста и у черновика: иначе растущая
        // реплика выглядела бы другим человеком
        function postAvatarHtml(post) {
            const emoji = post.avatar_emoji || '📣';
            // Лицо — свойство участника, а не реплики (см. show.post_view), поэтому
            // и менять его можно там, где оно видно: клик по эмодзи открывает набор
            // эмодзи-аватаров. Картинка по клику, как и раньше, разворачивается целиком.
            // Имя уезжает в data-атрибут, а не в onclick: имена — текст режиссёра,
            // и кавычка в имени ломала бы обработчик
            return post.avatar_url
                ? `<img src="${post.avatar_url}" onclick="showAvatarFull('${post.avatar_url}')">`
                : `<div class="emoji" data-emoji-for="${escapeHtml(post.display_name)}" title="Клик — сменить эмодзи-аватар">${emoji}</div>`;
        }

        function postHeaderHtml(post) {
            const role = post.role || 'participant';
            const roleIcon = post.role_icon || '🎭';
            const roleName = post.role_name || 'Участник';
            const genderSymbol = post.gender === 'male' ? '♂' : '♀';
            return `<div class="post-header"><div><div class="post-author"><span class="role-badge role-${role}">${roleIcon} ${roleName}</span> ${post.display_name} ${genderSymbol}</div><div class="post-model">модель: ${post.model_used}</div></div><div class="post-time">${post.timestamp} | Акт ${post.round}</div></div>`;
        }

        // Один блок о ходе: как эта реплика получилась — по порядку и с числами.
        //
        // Раньше это было разбросано по трём блокам («размышления», «сказано
        // раньше» и «что уехало в модель»), а сами данные лежали в двух разных
        // местах. Сложить из этого картину было нельзя: числа в одном блоке,
        // поиски в другом, а связь между ними — только в голове. Теперь это
        // одна хронология: откуда ход взялся, что происходило по порядку
        // (запросы с их вводом и выводом, поиски с формулировкой и находками),
        // и только потом — что модель сказала.
        const SKETCH_HINT = 'Так бывает, когда модель сначала отвечает, а потом её просят поискать.';
        // Смысл кода ответа вендора: тот же словарь, что в show.FINISH_MEANINGS
        const FINISH_WORDS = {stop: 'вендор считает, что модель договорила',
                              length: 'ответ оборвался по пределу вывода',
                              tool_calls: 'слов модель не сказала: она попросила вызвать инструмент',
                              function_call: 'слов модель не сказала: она попросила вызвать функцию',
                              content_filter: 'вендор вырезал содержимое своим фильтром'};

        const TURN_HINT = 'Весь путь к этой реплике по порядку: что вошло в каждый запрос к модели, что она попросила, что ей принесли и сколько токенов за это заплачено. Ввод — это то, что театр отправил в одном запросе, вывод — то, что вернул вендор (включая оплаченные размышления); «наш счёт» — то, что театр посчитал сам (tiktoken), а число рядом — счёт вендора.';

        function thinkingBlockHtml(hint, text) {
            const body = (text || '').trim();
            if (!body) return '';
            return `<div class="prompt-line">${escapeHtml(hint)}</div>`
                + `<pre class="prompt-text">${escapeHtml(body)}</pre>`;
        }

        // Один запрос хода одной строкой: здесь и объясняется, что значит
        // «1 431 + 1 246». Ввод — то, что ушло в запрос (промпт, история, найденное),
        // вывод — то, что вернул вендор, вместе с оплаченными размышлениями
        // Когда событие случилось: начало и конец — с тысячными долями. Без времени
        // в хронологии нельзя ответить на «чем модель занималась две минуты»
        function stepClock(step) {
            const start = step.clock || '';
            const end = step.clock_end || '';
            if (!start) return '';
            return `${start}${end && end !== start ? ' → ' + end : ''} · `;
        }

        function turnAskText(step) {
            if (step.error) return `не прошёл — ${escapeHtml(step.error)}`;
            const parts = [];
            if (step.tokens_in === undefined && step.tokens_out === undefined) {
                parts.push('числа токенов вендор не сообщил');
                if (step.tokens_in_est) parts.push(`наш счёт ≈${tokensText(step.tokens_in_est)} токенов`);
            } else {
                // Рядом с числом вендора — наш счёт: вендор считает своими
                // токенами, и числа расходятся; когда вендор чисел не дал,
                // наш счёт — единственное, что есть
                const estimate = step.tokens_in_est ? ` (наш счёт ≈${tokensText(step.tokens_in_est)})` : '';
                parts.push(`ввод ${tokensText(step.tokens_in)}${estimate} → вывод ${tokensText(step.tokens_out)} токенов`);
            }
            if (step.reasoning_tokens) parts.push(`из них размышлений ${tokensText(step.reasoning_tokens)}`);
            // Код ответа без перевода читается как код: рядом с ним — его смысл
            if (step.finish_reason) {
                const meaning = FINISH_WORDS[String(step.finish_reason).toLowerCase()];
                parts.push(`конец: ${escapeHtml(step.finish_reason)}` + (meaning ? ` (${meaning})` : ''));
            }
            parts.push(step.tools ? 'с инструментом поиска' : 'без инструмента поиска');
            return parts.join(' · ');
        }

        // «Что вошло в запрос к модели»: снимок запроса этого хода. В посте лежит только
        // сводка (сколько сообщений и токенов, что обрезано), а сам текст
        // подтягивается при раскрытии: снимок хода весит как сцена, и таскать
        // его в ленте незачем. В раскрытом виде видно ровно то, что прочитала
        // модель: системный промпт, история (чьим окном мерена) и что выброшено
        function tokensText(n) {
            return Number(n || 0).toLocaleString('ru-RU');
        }

        // Рубли с копейками — как на ценнике. Цена хода берётся из остатка
        // на ключе, а не из тарифов: тарифов мы не знаем, остаток знает шлюз
        function moneyText(n) {
            return Number(n || 0).toLocaleString('ru-RU', {minimumFractionDigits: 2,
                                                          maximumFractionDigits: 2}) + ' ₽';
        }

        // Сколько спектакль стоит на сейчас — строка для блока «Статус».
        // Показывается ВСЕГДА: и когда говорит модель, и когда ход ваш, и после
        // занавеса. Сумма копится от начала спектакля (см. show._note_money),
        // и раньше она пропадала ровно там, где о ней спокойнее всего знать
        function spentLine(data) {
            const spent = Number((data && data.spent) || 0);
            if (!spent) return '';
            return `<div style="font-size:12px;margin-top:6px;">💰 за спектакль ${moneyText(spent)}</div>`;
        }

        // Сколько длилось — словами: «2 мин 15 с». Секунды до десятых здесь
        // не нужны: это мера ожидания, а не измерение
        function durationText(seconds) {
            const total = Math.max(0, Math.round(Number(seconds) || 0));
            const minutes = Math.floor(total / 60);
            const secs = total % 60;
            if (minutes && secs) return `${minutes} мин ${secs} с`;
            if (minutes) return `${minutes} мин`;
            return `${secs} с`;
        }

        // Часы хода: сколько уже думает говорящий и сколько ему осталось по сроку.
        // Числа приходят секундами и обновляются вместе с состоянием (раз в
        // секунду по сокету), поэтому своих часов страница не заводит и соврать
        // не может. Срок есть только у облачного хода: без него — одно «думает…»
        function turnClockText(data) {
            if (data.turn_elapsed === null || data.turn_elapsed === undefined) return '';
            let text = `⏱ думает ${durationText(data.turn_elapsed)}`;
            if (data.turn_left !== null && data.turn_left !== undefined) {
                text += ` · осталось ${durationText(data.turn_left)}`;
                if (data.turn_extra) {
                    text += ` (ход продлён на ${durationText(data.turn_extra)} за поиски)`;
                }
            }
            return text;
        }

        const WINDOW_WORDS = {cloud: 'окно облака', local: 'окно модели',
                              unbounded: 'окно не ограничено (CLOUD_NUM_CTX = 0)'};
        // «Уехало» ничего не говорило: ни кто, ни куда. Здесь сказано прямо,
        // в каком виде это сообщение лежит в запросе к модели
        const ROLE_WORDS = {system: 'системный промпт', user: 'реплика в запросе',
                            assistant: 'сказано самой моделью', tool: 'результат поиска'};

        function promptMessagesHtml(messages) {
            return (messages || []).map((m, i) =>
                `<div class="prompt-msg"><div class="prompt-msg-head">`
                + `<span class="prompt-role prompt-role-${escapeHtml(m.role || 'user')}">${escapeHtml(m.role || 'user')}</span>`
                // Имя системного сообщения — то же слово «system»: второй раз
                // оно не нужно, а у остальных оно говорит, кто именно говорил
                + `${(m.name && m.name !== m.role) ? escapeHtml(m.name) + ' · ' : ''}${ROLE_WORDS[m.role] || ''}`
                // «0 токенов» — верный сигнал и ни капли смысла: по протоколу
                // просьба о поиске приходит пустым текстом плюс полем tool_calls.
                // Поэтому ноль назван нулём текста, а объяснение идёт строкой ниже
                + `<span class="prompt-tokens">${Number(m.tokens || 0) ? tokensText(m.tokens) + ' токенов' : '0 токенов текста'}</span>`
                + `<span class="prompt-num">${i + 1}/${messages.length}</span></div>`
                + (m.note ? `<div class="prompt-note">${escapeHtml(m.note)}</div>` : '')
                + (m.content ? `<pre class="prompt-text">${escapeHtml(m.content)}</pre>` : '')
                + `</div>`).join('');
        }

        function promptRemovedHtml(removed) {
            if (!removed || !removed.length) return '';
            return removed.map(r =>
                `<div class="prompt-removed">${escapeHtml(r.speaker || 'без имени')}`
                + ` · ${tokensText(r.tokens)} токенов · ${escapeHtml(r.preview || '')}</div>`).join('');
        }

        // Раздел отчёта хода: имя, назначение словами и рамка вокруг. Имён у разделов
        // раньше не было вовсе, и строка «Окно говорящего» выглядела такой же строкой,
        // как хронология, — всё сливалось в одну простыню. Назначение пишется тут же:
        // в разделе с числами без него непонятно, что именно эти числа значат
        // (см. TURN_HINT)
        function turnBlock(title, purpose, inner) {
            return `<section class="prompt-block">`
                + `<div class="prompt-block-title">${escapeHtml(title)}</div>`
                + (purpose ? `<div class="prompt-block-purpose">${escapeHtml(purpose)}</div>` : '')
                + inner + `</section>`;
        }

        // Из чего сложился дописанный хвост — словами и по счёту, а не по
        // привычке. Раньше тут стояло «по паре на каждый поиск, плюс
        // напоминания», и с числами это не сходилось: пара — привычный случай,
        // а не всегдашний (счёт по сообщениям — в show.added_kinds)
        function addedPurpose(summary, added) {
            const kinds = (summary && summary.added_kinds) || {};
            const words = [];
            if (kinds.asks) words.push(`просьба вызвать инструмент — ${kinds.asks}`);
            if (kinds.results) words.push(`найденное по ней — ${kinds.results}`);
            if (kinds.refusals) words.push(`отказ по лимиту поисков — ${kinds.refusals}`);
            if (kinds.nudges) words.push(`просьба приложения словами — ${kinds.nudges}`);
            const count = `${added.length} сообщ. `
                + `(${tokensText(summary.extra_tokens)} токенов)`;
            const tail = 'В хронологии выше те же поиски названы своими словами — '
                + 'с формулировкой запроса и своим весом.';
            if (!words.length) {
                return `После первого запроса приложение дописало модели ${count}. ` + tail;
            }
            return `После первого запроса приложение дописало модели ${count}: `
                + words.join(' · ') + '. ' + tail;
        }

        function turnBodyHtml(data) {
            const s = data.summary || {};
            const b = data.budget || {};
            const who = data.who || {};
            const parts = [];
            // Пол — тем же значком, что у поста в ленте (см. postHeaderHtml):
            // одна и та же строка «кто говорит» в файле и на странице (см. who_line)
            const whoGender = who.gender === 'female' ? '♀' : '♂';
            parts.push(turnBlock('👤 Кто говорит и когда',
                'Дальше всё, что случилось за этот ход: чьё было место под историю, '
                + 'что происходило по порядку и чем ход кончился.',
                `<div class="prompt-line"><b>${escapeHtml(who.name || '')}</b> ${whoGender} · `
                + `${escapeHtml(who.model || '')} · Акт ${who.round} · ${escapeHtml(who.time || '')}`
                + (s.seconds ? ` · ход длился ${durationText(s.seconds)}` : '') + `</div>`));
            // Четыре числа окна — не украшение, а ответ на «куда делись токены»
            // Ноль в запасе — не «ноль токенов на ответ», а «ответ не ограничиваем»:
            // именно это значит CLOUD_MAX_TOKENS = 0, и писать иначе — врать
            const answerSeat = Number(b.reserve || 0) > 0
                ? `${tokensText(b.reserve)} оставлено на ответ модели`
                : 'на ответ ничего не зарезервировано (CLOUD_MAX_TOKENS = 0 — ответ не ограничиваем)';
            const placeHtml = [`<div class="prompt-line"><b>Окно говорящего:</b> ${WINDOW_WORDS[b.kind] || 'окно модели'} ${tokensText(b.window)} токенов целиком`
                + ` — ${answerSeat}, ${tokensText(b.safety)} — технический запас,`
                + ` на историю оставалось ${b.available === null ? 'без предела' : tokensText(b.available)}</div>`];
            // У каждого числа — своё имя: раньше тут стояло «уехало 3 сообщ. из 1»,
            // где первое считало все сообщения запроса, а второе — только сцену
            const tasks = Math.max(0, (s.messages || 0) - 1 - (b.messages_after || 0));
            placeHtml.push(`<div class="prompt-line"><b>Запрос к модели состоял из:</b> ${s.messages} сообщ. `
                + `(${tokensText(s.tokens)} токенов) — системный промпт ${tokensText(b.system_tokens)} токенов, `
                + `из сцены ${b.messages_after} сообщ. (${tokensText(b.kept_tokens)} токенов)`
                + (tasks ? `, и ещё ${tasks} — задания хода` : '')
                + (s.removed_messages
                    ? `. Обрезка выбросила ${s.removed_messages} сообщ. (${tokensText(s.removed_tokens)} токенов)`
                    : '. Обрезка ничего не тронула: сцена влезла в окно целиком')
                + `</div>`);
            // Окно и то, что в него вошло, — один раздел: это два взгляда на одно место
            parts.push(turnBlock('📐 Место под историю: чьё окно и как оно заполнилось',
                'Окно — сколько токенов вообще было у говорящего; запас на ответ — '
                + 'сколько из него оставлено модели на её реплику; остаток — сколько '
                + 'после этого досталось сцене. Ниже — из чего состоял первый запрос '
                + 'и что из сцены пришлось выкинуть, чтобы он влез.',
                placeHtml.join('')));
            // Одна хронология на всё: запросы, размышления и поиски идут в том
            // порядке, как случились, — со временем и весом. Отдельных разделов
            // под поиск и размышления нет нарочно: те же данные дважды — это
            // не полнота, а каша
            const steps = data.steps || [];
            const stepsHtml = [];
            if (!steps.length) {
                stepsHtml.push('<div class="prompt-step">шагов не записано: ход не оставил следов</div>');
            }
            steps.forEach(step => {
                const clock = stepClock(step);
                if (step.kind === 'ask') {
                    stepsHtml.push(`<div class="prompt-step"><span class="prompt-clock">${clock}</span><b>запрос ${step.n}</b> — ${turnAskText(step)}</div>`);
                } else if (step.kind === 'search') {
                    stepsHtml.push(`<div class="prompt-step"><span class="prompt-clock">${clock}</span><b>поиск ${step.n}</b> (лимит ${step.limit}): `
                        + `«${escapeHtml(step.query || '')}» — принесено ${tokensText(step.tokens)} токенов</div>`
                        + `<pre class="prompt-text">${escapeHtml(step.results || '')}</pre>`);
                } else if (step.kind === 'thought') {
                    stepsHtml.push(`<div class="prompt-step"><span class="prompt-clock">${clock}</span>💭 <b>размышления</b> (к запросу ${step.n}) — `
                        + `${tokensText(step.tokens)} токенов, в реплику не попали</div>`
                        + `<pre class="prompt-text">${escapeHtml(step.text || '')}</pre>`);
                } else {
                    const marks = {refused: '⛔', silence: '⚠️', force: '🔍', money: '💰', note: '·'};
                    const mark = marks[step.kind] || '🔍';
                    stepsHtml.push(`<div class="prompt-step"><span class="prompt-clock">${clock}</span>${mark} ${escapeHtml(step.text || '')}</div>`);
                }
            });
            parts.push(turnBlock('🧭 Хронология хода: что происходило по порядку',
                'Запросы к модели, поиски, размышления и заминки — в том порядке, '
                + 'как шли, со временем и весом. Других мест, где ход перечислен '
                + 'по порядку, в отчёте нет.',
                stepsHtml.join('')));
            const sketch = thinkingBlockHtml(SKETCH_HINT, data.sketch);
            if (sketch) {
                parts.push(turnBlock('🌱 Сказано раньше: прежняя версия реплики',
                    'Модель сказала это до того, как попросила поиск, и потом сказала '
                    + 'заново. Оплачены обе версии — поэтому обе и остаются.', sketch));
            }
            if (s.removed_messages) {
                parts.push(turnBlock('✂️ Что выбросила обрезка',
                    'Самое раннее из сцены: этим пожертвовали, чтобы ход влез в окно '
                    + 'говорящего. В самом запросе этих реплик уже нет.',
                    promptRemovedHtml(data.removed)));
            }
            if ((data.added || []).length) {
                // Не «ход дописал», а кто и что: дописывает приложение, а не модель.
                // Полные тексты здесь есть, и это не ошибка: найденное приходит
                // модели именно таким сообщением (кроме текста у него ничего нет),
                // и в этом разделе видно, сколько оно весило в запросе. В хронологии
                // выше тот же поиск назван своими словами — с запросом и весом
                parts.push(turnBlock('✍️ Что приложение дописало в запрос',
                    addedPurpose(s, data.added),
                    promptMessagesHtml(data.added)));
            } else if (s.extra_messages) {
                parts.push(turnBlock('✍️ Что приложение дописало в запрос',
                    `После первого запроса приложение дописало модели ещё ${s.extra_messages} сообщ. `
                    + `(${tokensText(s.extra_tokens)} токенов) — все они названы в хронологии выше, со своим весом.`,
                    ''));
            }
            parts.push(turnBlock('📨 Первый запрос к модели целиком',
                'Тем самым порядком, как читала модель: системный промпт, сцена и '
                + 'задания хода. Это только начало хода — найденное приложение '
                + 'дописало уже после него (см. раздел выше).',
                promptMessagesHtml(data.messages)));
            parts.push(turnBlock('💬 Реплика, которой ход кончился',
                'То же, что в ленте этой репликой ниже: здесь — чтобы ход читался до конца, не отрываясь.',
                `<pre class="prompt-text">${escapeHtml(data.answer || '')}</pre>`));
            return parts.join('');
        }

        // Сколько строк длинного текста видно до раскрытия
        const CLAMPED_TEXT_LINES = 3;

        // Прятать ли «простыню» — по настоящей высоте текста, а не по числу
        // переводов строки: одна длинная строка переносится и занимает столько же
        // места, сколько десять коротких. Функция отдельная не для красоты: её
        // граница проверяется тестом (см. textNeedsClamp)
        function textNeedsClamp(contentHeight, lineHeight, padding) {
            const line = Number(lineHeight) || 0;
            if (!line) return false;
            return Number(contentHeight) - Number(padding || 0) > line * CLAMPED_TEXT_LINES + 2;
        }

        // Свернуть длинные тексты в отчёте хода и раскрывать их по клику
        function clampLongTexts(root) {
            (root || document).querySelectorAll('pre.prompt-text').forEach(pre => {
                const style = getComputedStyle(pre);
                const padding = (parseFloat(style.paddingTop) || 0)
                    + (parseFloat(style.paddingBottom) || 0);
                if (!textNeedsClamp(pre.clientHeight, parseFloat(style.lineHeight), padding)) return;
                pre.classList.add('clamped');
                pre.tabIndex = 0;
                pre.title = 'Клик — раскрыть целиком';
                const toggle = () => pre.classList.toggle('expanded');
                pre.addEventListener('click', toggle);
                pre.addEventListener('keydown', event => {
                    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
                });
            });
        }

        function loadTurnBox(box) {
            if (box.dataset.loaded === '1') return;
            box.dataset.loaded = '1';
            const body = box.querySelector('.prompt-body');
            body.innerHTML = '<div class="prompt-hint">⏳ читаю ход…</div>';
            fetch(`/api/post/${box.dataset.postId}/turn`, {cache: 'no-store'})
                .then(r => r.json().then(data => {
                    if (!r.ok) throw data;
                    return data;
                }))
                .then(data => { body.innerHTML = turnBodyHtml(data); clampLongTexts(body); })
                .catch(err => {
                    // Неудача — не повод оставить блок пустым: его можно
                    // раскрыть ещё раз и снова спросить сервер
                    box.dataset.loaded = '';
                    body.innerHTML = `<div class="prompt-hint">⚠️ ${escapeHtml((err && err.error) || 'снимок не читается')}</div>`;
                });
        }

        // Сводка в свёрнутой строке: сколько было запросов к модели, сколько
        // поисков и сколько токенов ушло — чтобы решить, раскрывать ли
        // Сводка хода в свёрнутой строке: по ней решаешь, раскрывать ли блок.
        // Вынесена отдельной функцией не для красоты: здесь одни числа, а числа
        // проверяются — и настоящим node тоже (см. turnClockText)
        function turnSummaryParts(info) {
            const parts = [];
            // Ноль запросов — не факт о ходе, а «ещё неизвестно»: строку пишут
            // и в начале хода, когда запросов ещё не было (см. show.turn_heading)
            if (info.asks) parts.push(`запросов ${info.asks}`);
            if (info.search_rounds) parts.push(`поисков ${info.search_rounds}`);
            if (info.thought_steps) parts.push(`размышлений ${info.thought_steps}`);
            // Ввод — по ВСЕМ запросам хода, когда их было несколько: поиск
            // это ещё один круг, и вся история уезжает к модели заново, поэтому
            // ход с пятью поисками оплачивается шесть раз. Одно число «токенов
            // на ввод» (это первый запрос) и путало: в кабинете шлюза видны
            // все запросы, и сумма там втрое больше (см. _input_tokens_total)
            if (info.asks > 1 && info.tokens_in_total) {
                parts.push(`на ввод всего ${tokensText(info.tokens_in_total)} токенов`);
            } else {
                parts.push(`${tokensText(info.tokens)} токенов на ввод`);
            }
            // Сколько ход длился — рядом с ценой: время здесь такая же плата,
            // и без него видно, сколько реплика стоила, но не видно, чего
            // она стоила зрителю (см. refresh_turn_report)
            if (info.seconds) parts.push(`⏱ ${durationText(info.seconds)}`);
            // Цена хода — факт со счёта шлюза (разница остатков), а не оценка:
            // тарифов приложение не знает (см. settings.CLOUD_BALANCE_PATH)
            if (info.spent) parts.push(`💰 ${moneyText(info.spent)}`);
            if (info.removed_messages) parts.push(`выброшено ${info.removed_messages}`);
            // Заминки названы по отдельности: отказ в поиске сверх лимита —
            // это ещё не беда (модель просто просила больше, чем ей дали),
            // а вот молчание — уже беда. Общим числом они говорили бы «что-то
            // было», не говоря что
            if (info.search_refusals) parts.push(`поиск сверх лимита ${info.search_refusals}`);
            if (info.silences) parts.push(`⚠️ без ответа ${info.silences}`);
            return parts;
        }

        function postTurnHtml(post) {
            const info = post.turn;
            if (!info) return '';
            const parts = turnSummaryParts(info);
            return `<details class="post-thinking post-prompt" data-post-id="${post.id}">`
                + `<summary>🧾 ход реплики · ${parts.join(' · ')}</summary>`
                + `<div class="prompt-hint">${TURN_HINT}</div>`
                + `<div class="prompt-body"></div></details>`;
        }

        function addPost(post) {
            const postsDiv = document.getElementById('posts');
            // Черновик, чей ход уже закончился, уступает место настоящему посту:
            // так реплика не мелькает и не задваивается
            postsDiv.querySelectorAll('[data-stream-done]').forEach(el => el.remove());
            let searchInfo = '';
            if (post.search_count > 0) { searchInfo = `<div class="search-info"><strong>Источники</strong>${post.search_queries.map(q => `<span class="search-query">"${q}"</span>`).join('')}</div>`; }

            const postDiv = document.createElement('div');
            // Класс роли нужен для цветной полосы слева (см. body.role-marks)
            postDiv.className = `post post-role-${post.role || 'participant'}`;
            postDiv.innerHTML = `<div class="post-avatar">${postAvatarHtml(post)}</div><div class="post-content">${postHeaderHtml(post)}${postTurnHtml(post)}<div class="post-text">${post.content_html || post.content}</div>${searchInfo}</div>`;
            // Формулы в реплике — в MathML (см. renderMath)
            renderMath(postDiv.querySelector('.post-text'));
            // Отчёт о ходе спрашиваем только когда его открыли: событие toggle
            // не всплывает, поэтому слушаем именно свой блок, а не ленту
            const turnBox = postDiv.querySelector('.post-prompt');
            if (turnBox) {
                turnBox.addEventListener('toggle', () => {
                    if (turnBox.open) loadTurnBox(turnBox);
                });
            }
            // Свежие реплики сверху: пульт и поле реплики тоже наверху, и читать
            // спектакль снизу вверх не приходится
            postsDiv.insertBefore(postDiv, postsDiv.firstChild);
        }

        // Черновик реплики: пока облачная модель говорит, текст растёт на глазах
        function upsertStreamPost(draft) {
            const postsDiv = document.getElementById('posts');
            const box = postsDiv.querySelector(`[data-stream-id="${draft.stream_id}"]`);
            if (draft.done) {
                // Ход кончился: настоящий пост придёт следующим событием и уберёт
                // черновик. Если не придёт (связь пропала) — уберём сами
                if (box) { box.setAttribute('data-stream-done', '1'); setTimeout(() => box.remove(), 3000); }
                return;
            }
            let element = box;
            if (!element) {
                element = document.createElement('div');
                element.className = `post post-role-${draft.role || 'participant'} streaming`;
                element.setAttribute('data-stream-id', draft.stream_id);
                element.innerHTML = `<div class="post-avatar">${postAvatarHtml(draft)}</div><div class="post-content">${postHeaderHtml(draft)}<details class="post-thinking" open><summary></summary><div class="thinking-text"></div></details><details class="post-thinking post-sketch"><summary></summary><div class="thinking-hint">${SKETCH_HINT}</div><div class="thinking-text"></div></details><div class="post-text"></div></div>`;
                postsDiv.insertBefore(element, postsDiv.firstChild);
            }
            // Мысли: своим бледным блоком над репликой. Свёрнутыми их сделает
            // зритель сам — а придут они раньше ответа, и без них была бы
            // длинная пауза непонятно чего
            const thoughts = element.querySelector('.post-thinking:not(.post-sketch)');
            if (thoughts) {
                const thought = draft.thinking || '';
                thoughts.style.display = thought ? '' : 'none';
                thoughts.querySelector('.thinking-text').textContent = thought;
                // «размышляет» — пока не сказано ни слова; дальше это уже мысли
                // о сказанном, и подпись должна быть честной
                thoughts.querySelector('summary').textContent = draft.answer_started ? '💭 мысли' : '💭 размышляет';
            }
            // Сказанное до новой попытки: текст уже был в ленте и пропадать ему
            // незачем — сворачивается в блок и остаётся до конца хода
            const sketch = element.querySelector('.post-sketch');
            if (sketch) {
                const said = (draft.sketch || '').trim();
                sketch.style.display = said ? '' : 'none';
                sketch.querySelector('.thinking-text').textContent = said;
                sketch.querySelector('summary').textContent =
                    `🌱 сказано раньше · ${said.length.toLocaleString('ru-RU')} знаков`;
            }
            const postText = element.querySelector('.post-text');
            // Разметку сервер собирает на каждой порции: жирный текст и списки
            // появляются на глазах, а не в самом конце хода. Незакрытая
            // звёздочка так и остаётся звёздочкой — markdown её не съест
            if (postText) postText.innerHTML = draft.content_html || escapeHtml(draft.content || '');
        }
        
        function showAvatarFull(url) { document.getElementById('avatarModalImg').src = url; document.getElementById('avatarModal').style.display = 'block'; }

        // Формулы: сервер отдаёт LaTeX внутри span.math (markdown его не портит),
        // здесь он превращается в MathML — рисует сам браузер, без картинок и шрифтов.
        // Если рендерер не загрузился, формула остаётся текстом, как было раньше
        function renderMath(root) {
            if (!root || typeof temml === 'undefined') return;
            root.querySelectorAll('span.math:not(.math-done)').forEach(el => {
                const latex = el.textContent;
                try {
                    el.innerHTML = temml.renderToString(latex, {
                        displayMode: el.dataset.display === '1',
                        throwOnError: false
                    });
                    el.classList.add('math-done');
                } catch (err) {
                    console.warn('Формула не отрисовалась:', latex, err);
                }
            });
        }
        
        // Предупреждение «модель не влезает в VRAM»: показывается до старта,
        // спектакль не блокирует - просто честно говорит, что будет медленнее
        function renderVramWarning(status) {
            const box = document.getElementById('vramWarning');
            if (!box) return;
            const warnings = (status && status.warnings) || [];
            if (!warnings.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
            
            const gb = b => (b / 1e9).toFixed(1);
            const items = warnings.map(w => {
                let text = `<b>${escapeHtml(w.model)}</b>: при num_ctx ${status.num_ctx} нужно ~${gb(w.need)} ГБ, `
                    + `а доступно ~${gb(status.budget)} ГБ из ${gb(status.gpu_total)} ГБ. `;
                if (w.safe_ctx) {
                    text += `Поставьте num_ctx <b>${w.safe_ctx}</b> (тогда ~${gb(w.safe_need)} ГБ)`;
                } else {
                    text += 'Даже с минимальным контекстом модель не влезает целиком — часть будет считать процессор';
                }
                if (!w.measured) text += ' <span style="opacity:.7;">[оценка по размеру файла]</span>';
                return `<div style="margin-bottom:8px;">${text}</div>`;
            }).join('');
            
            box.innerHTML = `⚠️ <b>Не хватает видеопамяти</b><div style="margin-top:8px;">${items}</div>`
                + '<div style="margin-top:8px;font-size:13px;">Спектакль пойдёт и так, но такие модели будут считать медленнее: уменьшите <code>num_ctx</code> в OPTIONS.</div>';
            box.style.display = 'block';
        }
        
        function renderLoadedModels(loaded, gpu, error) {
            const el = document.getElementById('vramDisplay');
            if (!el) return;
            const gb = b => (b / 1e9).toFixed(1);
            const esc = escapeHtml;
            
            if (error) { el.innerHTML = '<div style="color:#b00020;">Ollama недоступна</div>'; return; }
            if (!loaded || loaded.length === 0) { el.innerHTML = '<div style="color:#999;">В памяти сейчас ничего нет</div>'; return; }
            
            const total = (gpu && gpu.total) ? gpu.total : 0;
            el.innerHTML = loaded.map(m => {
                const vram = m.size_vram || 0;
                const size = vram ? `${gb(vram)} ГБ в VRAM` : `${gb(m.size)} ГБ в RAM`;
                const pct = (vram && total) ? ` <span style="color:#666;">(${Math.round(vram / total * 100)}% из ${gb(total)} ГБ)</span>` : '';
                const ctx = m.context_length ? `<div style="color:#666;font-size:12px;">контекст: ${m.context_length}</div>` : '';
                // Не поместилась в VRAM: либо Ollama сама это сказала, либо заявила
                // больше, чем есть на карте (на Windows часть уходит в общую память)
                let spillBytes = 0;
                if (m.size && vram) {
                    if (total && vram > total) spillBytes = Math.max(0, m.size - total);
                    else if (vram < m.size) spillBytes = m.size - vram;
                }
                const spill = spillBytes
                    ? `<div style="color:#b00020;font-size:12px;">⚠️ ещё ${gb(spillBytes)} ГБ в RAM</div>` : '';
                return `<div style="margin-bottom:10px;"><strong>${esc(m.name)}</strong><div>${size}${pct}</div>${ctx}${spill}</div>`;
            }).join('');
            if (total) {
                el.innerHTML += `<div style="color:#666;font-size:12px;border-top:1px solid #000;padding-top:8px;">Занято на GPU: ${gb(gpu.used)} из ${gb(total)} ГБ</div>`;
            }
        }
        
        // Обновить только блок памяти: он нужен и до спектакля, и после занавеса,
        // когда polling уже остановлен, а модели как раз выгружаются
        function refreshMemory() {
            fetch('/api/status', {cache: 'no-store'}).then(r => r.json())
                .then(d => renderLoadedModels(d.loaded_models, d.gpu_memory, d.loaded_models_error))
                .catch(() => {});
        }
        
        // После занавеса модели выгружаются НЕ мгновенно (гигабайты уходят
        // в память не сразу), поэтому обновляем панель ещё несколько раз,
        // а не двумя разовыми замерами
        let memorySettleTimer = null;
        let memorySettleTicks = 0;
        
        function settleMemoryPanel() {
            memorySettleTicks = 8;  // ~40 секунд наблюдения
            if (memorySettleTimer) return;
            memorySettleTimer = setInterval(() => {
                refreshMemory();
                if (--memorySettleTicks <= 0) {
                    clearInterval(memorySettleTimer);
                    memorySettleTimer = null;
                }
            }, 5000);
            refreshMemory();
        }
        
        let statusRequestInFlight = false;

        // Сколько подряд неудачных опросов считать закрытием театра. Одна неудача не
        // значит ничего: так рвётся и закрытое keep-alive соединение, и ответ
        // занятого сервера — в том числе в момент, когда начинается спектакль
        // и модель грузится в память. Раньше первая же неудача объявляла «Театр
        // закрыт» и глушила опрос: спектакль шёл, а лента замирала навсегда.
        const STATUS_FAILURES_BEFORE_CLOSED = 5;
        let statusFailures = 0;

        function updatePosts() {
            // Опрос и событие Socket.IO могут сработать одновременно, а запрос несёт
            // lastPostCount: два параллельных ответа добавили бы один пост дважды.
            // Второй вызов пропускаем - следующий опрос всё равно подхватит новое.
            if (statusRequestInFlight) return;
            statusRequestInFlight = true;
            
            fetch(`/api/status?lastPostCount=${lastPostCount}`, {cache: 'no-store'})
            // Если сервер ответил не JSON, сказать об этом честно: без этой проверки
            // ошибка выглядела бы как «не удалось прочитать ответ» — и снова
            // выдавала себя за закрытый театр
            .then(r => { if (!r.ok) throw new Error(`сервер ответил ${r.status}`); return r.json(); })
            .then(data => applyStatus(data))
            .catch(err => {
                statusFailures++;
                console.error(`Ошибка обновления статуса (${statusFailures} подряд):`, err);
                // Ниже — поведение для случая, когда связь пропала надолго: без этой
                // проверки любая случайная неудача выглядела как закрытый театр
                if (statusFailures < STATUS_FAILURES_BEFORE_CLOSED) return;
                // Похоже, театр и правда закрыт: показываем это и скрываем кнопку выхода
                const statusDiv = document.getElementById('statusBar');
                statusDiv.style.display = 'block';
                statusDiv.classList.remove('active');
                statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">🎭 Театр закрыт</div><div style="font-style:italic;">Спектакль сохранён для просмотра</div>';
                
                const exitBtn = document.querySelector('.footer .btn');
                if (exitBtn) exitBtn.style.display = 'none';
                
                // Опрос останавливаем только убедившись, что сервера нет, а не после
                // первой же пустой попытки: иначе лента замолкала на весь спектакль
                stopPolling();
            })
            .finally(() => { statusRequestInFlight = false; });
        }

        // Состояние спектакля: и опрос, и сокет приводят сюда. Раньше эта же
        // роспись жила внутри опроса, и состояние пульта зависело от того, кто
        // его принёс — теперь путь один
        function applyStatus(data) {
            statusFailures = 0;
            // Сессия сменилась на сервере — сбросить локальный UI.
            if (data.session_id) {
                if (mySessionId === null) {
                    mySessionId = data.session_id;
                } else if (mySessionId !== data.session_id) {
                    // Спектакль начался в другой вкладке: переходим к просмотру
                    mySessionId = data.session_id;
                    debateRunning = true;
                    lastPostCount = 0;
                    document.getElementById('posts').innerHTML = '';
                    setTurnState('hidden');
                    loadCast().then(() => { updateSidebarParticipants(); updatePanel(); });
                    return;
                }
            }
            const statusDiv = document.getElementById('statusBar');
            const statusPlaceholder = document.getElementById('statusPlaceholder');
            statusDiv.style.display = 'block'; statusPlaceholder.style.display = 'none';
            // Флаги ставим до отрисовки пульта: после перезагрузки страницы
            // он должен сразу знать, что спектакль идёт, а не ждать нового старта
            debateRunning = true;
            showFinished = !!data.finished;
            if (data.topic) setTopicDisplay(data.topic);
            updatePanel();
            if (data.waiting_for_human) {
                // Ход человека: имя всегда, роль — только если она особенная
                const roleName = roleLabelOf(data.current_participant_role);
                const wasOpen = document.getElementById('turnComposer').style.display === 'block';
                setTurnState('your', data.current_participant, roleName);
                if (!wasOpen) {
                    const mi = document.getElementById('moderatorInput');
                    if (mi && !mi.value.trim()) mi.focus();
                }
                statusDiv.classList.add('active');
                statusDiv.innerHTML = `<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">Акт ${data.current_round}</div><div>${escapeHtml(data.current_participant || '')}</div><div style="font-style:italic;font-size:12px;margin-top:8px;">Ваш ход!</div>${spentLine(data)}`;
            } else {
                // Не ваша очередь: блок остаётся на месте с пояснением, чтобы
                // нумерация разделов пульта не прыгала
                setTurnState(data.finished ? 'finished' : 'waiting', data.current_participant);
            }
            if (data.running && !data.waiting_for_human) {
                statusDiv.classList.add('active');
                let at = data.current_action === 'searching' ? `Ищет: "${data.search_query}"` : data.current_action === 'waiting' ? 'Готовит реплику...' : 'Говорит реплику...';
                // Сколько уже стоил спектакль — по факту со счёта шлюза
                const bill = spentLine(data);
                // Часы хода — рядом с ценой: по ним видно, ждать минуту или
                // десять, и успеешь ли сходить за пивом (см. turnClockText)
                const clock = turnClockText(data);
                const clockLine = clock ? `<div style="font-size:12px;margin-top:6px;">${clock}</div>` : '';
                statusDiv.innerHTML = `<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">Акт ${data.current_round}</div><div>${escapeHtml(data.current_participant || '')}</div><div style="font-style:italic;font-size:12px;margin-top:8px;">${at}</div>${clockLine}${bill}`;
            } else if (data.finished) {
                statusDiv.classList.remove('active');
                const curtain = spentLine(data);
                // Спектакль мог вернуться из ДАМПа прошлого запуска: без этих
                // слов лента с чужими репликами выглядит как «театр помнит то,
                // чего я не играл» (см. show.load_play_from_dump)
                const restored = data.restored
                    ? '<div style="font-size:12px;margin-top:6px;font-style:italic;">🗒 прежний спектакль, возвращённый из ДАМПа</div>'
                    : '';
                statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;">🎭 Занавес</div>' + restored + curtain;
                setTurnState('finished');
                document.getElementById('finishBtn').style.display = 'none';
                // «Покинуть театр» оставляем: занавес больше не закрывает сервер,
                // и это единственная кнопка остановки приложения
                stopPolling();
                settleMemoryPanel();
            }
            addNewPosts(data.new_posts);
            // Счётчик показанных реплик двигаем только тогда, когда состояние
            // пришло вместе с репликами: у рассылки по сокету их нет, и счётчик
            // не должен перескочить мимо ещё не показанного поста
            if (data.posts_included && typeof data.total_posts === 'number') {
                lastPostCount = data.total_posts;
            }
            // Показываем, какая модель сейчас в памяти и сколько занимает
            renderLoadedModels(data.loaded_models, data.gpu_memory, data.loaded_models_error);
            // Инструкции в сайдбаре меняются только вручную, поэтому обновляем
            // их раз в 30 секунд, а не на каждом опросе
            if (instructionsTick++ % 10 === 0) updateSidebarParticipants();
        }
        
        function sendModeratorMessage() {
            const input = document.getElementById('moderatorInput');
            fetch('/api/moderator/message', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: input.value}) })
            .then(r => r.json()).then(data => { if (data.success) { input.value = ''; setTurnState('sent'); } else alert('Ошибка: ' + (data.error || 'неизвестная')); })
            .catch(err => { console.error('Ошибка:', err); alert('Ошибка: ' + err.message); });
        }
        
        function updateSidebarParticipants() {
            // Получаем актуальные инструкции с сервера
            fetch('/api/moderator/instructions', {cache: 'no-store'})
            .then(r => r.json())
            .then(data => {
                // Создаём словарь индивидуальных инструкций
                const currentInstructions = {};
                if (data.participant_instructions) {
                    data.participant_instructions.forEach(p => {
                        if (p.instruction && p.instruction.trim()) {
                            currentInstructions[p.name] = p.instruction;
                        }
                    });
                }
                
                // Состав у страницы и у сервера один и тот же (cast), поэтому сайдбар
                // всегда совпадает со сценой: и в настройке, и после перезагрузки
                document.getElementById('participantsDisplay').innerHTML = cast.map(p => {
                    const instruction = currentInstructions[p.display_name];
                    const genderSymbol = p.gender === 'male' ? '♂' : '♀';
                    
                    // Определяем иконку роли
                    let roleIcon = '🎭';
                    let roleLabel = ' <span style="color:#1976d2;font-size:11px;font-weight:bold;">УЧАСТНИК</span>';
                    if (p.is_moderator) {
                        roleIcon = '🎬';
                        roleLabel = ' <span style="color:#f57c00;font-size:11px;font-weight:bold;">МОДЕРАТОР</span>';
                    } else if (p.is_judge) {
                        roleIcon = '⚖️';
                        // Публичного судью слышат участники — это стоит видеть, не
                        // заходя в карточку: на ходу именно это и решает, отзовутся
                        // ли другие на вердикт
                        roleLabel = ' <span style="color:#7b1fa2;font-size:11px;font-weight:bold;">СУДЬЯ</span>'
                            + (roleOptionsOf(p).publicity === 'public'
                                ? '<span style="font-size:11px;" title="Публичный: вердикт слышат участники"> 📣</span>'
                                : '<span style="font-size:11px;" title="Анонимный: вердикт видит только режиссёр"> 🤫</span>');
                    }
                    
                    // Как и в ленте: пол сразу после имени
                    let html = `<div style="margin-bottom:12px;">${roleIcon} <strong>${escapeHtml(p.display_name)}</strong> ${genderSymbol}${roleLabel} <small>(${escapeHtml(p.model)})</small>`;
                    if (instruction) {
                        html += `<br><em style="margin-left:10px;">${escapeHtml(instruction)}</em>`;
                    }
                    return html + '</div>';
                }).join('');
                
                // Обновляем блок "Правила общения"
                const rulesDisplay = document.getElementById('rulesDisplay');
                if (data.static_instructions && data.static_instructions.length > 0) {
                    rulesDisplay.innerHTML = data.static_instructions
                        .filter(rule => rule.trim())
                        .map(rule => `<div style="margin-bottom:8px;">• ${escapeHtml(rule)}</div>`)
                        .join('');
                } else {
                    rulesDisplay.innerHTML = '<div style="color:#999;">Правила не заданы</div>';
                }
                
                // Обновляем блок "Инструкции от руководства"
                const modInstructionsDisplay = document.getElementById('moderatorInstructionsDisplay');
                if (data.moderator_messages && data.moderator_messages.length > 0) {
                    modInstructionsDisplay.innerHTML = data.moderator_messages
                        .filter(msg => msg.trim())
                        .map(msg => `<div style="margin-bottom:8px;">• ${escapeHtml(msg)}</div>`)
                        .join('');
                } else {
                    modInstructionsDisplay.innerHTML = '<div style="color:#999;font-weight:normal;">Нет указаний от руководства</div>';
                }
            })
            .catch(err => console.error('Ошибка обновления сайдбара:', err));
        }
        
        function saveInstructions() {
            // Собираем static_instructions
            const staticInstructions = [];
            const staticContainer = document.getElementById('staticInstructionsEditor');
            staticContainer.querySelectorAll('textarea').forEach(ta => {
                if (ta.value.trim()) staticInstructions.push(ta.value.trim());
            });
            
            // Собираем moderator_messages
            const moderatorMessages = [];
            const modContainer = document.getElementById('moderatorMessagesEditor');
            modContainer.querySelectorAll('textarea').forEach(ta => {
                if (ta.value.trim()) moderatorMessages.push(ta.value.trim());
            });
            
            // Собираем индивидуальные инструкции участников
            const participantInstructions = [];
            const participantContainer = document.getElementById('participantInstructionsEditor');
            participantContainer.querySelectorAll('div[data-participant]').forEach(div => {
                const textarea = div.querySelector('textarea');
                const name = (div.dataset.participant || '').trim();
                if (textarea && name) {
                    participantInstructions.push({ name: name, instruction: textarea.value });
                }
            });
            
            // Собираем правила судьи
            const judgeRules = [];
            document.getElementById('judgeRulesEditor').querySelectorAll('textarea').forEach(ta => {
                if (ta.value.trim()) judgeRules.push(ta.value.trim());
            });
            
            // Отправляем на сервер
            fetch('/api/moderator/instructions', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    static_instructions: staticInstructions,
                    moderator_messages: moderatorMessages,
                    judge_rules: judgeRules,
                    participant_instructions: participantInstructions
                })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    alert('✅ Инструкции обновлены!');
                    // Редактор не прячем: он и есть содержимое вкладки, а в его
                    // полях после сохранения — ровно то, что уехало на сервер
                    // Обновляем сайдбар с актуальными инструкциями
                    updateSidebarParticipants();
                } else {
                    alert('❌ Ошибка: ' + (data.error || 'неизвестная'));
                }
            })
            .catch(err => {
                console.error('Ошибка сохранения:', err);
                alert('❌ Ошибка сохранения: ' + err.message);
            });
        }
        
        // «Завершить спектакль» опускает занавес, но НЕ закрывает театр: сервер остаётся
        // живым, опрос видит finished и показывает «🎭 Новый спектакль». Остановка сервера -
        // отдельная кнопка «Покинуть театр».
        function finishDebate() {
            if (confirm('Опустить занавес? После этого можно собрать новый спектакль.')) {
                finishRequested = true;
                fetch('/api/moderator/finish', {method: 'POST'})
                    .then(() => updatePosts())
                    .catch(() => {});

                // Немедленный отклик, не дожидаясь сервера
                setTurnState('hidden');
                document.getElementById('finishBtn').style.display = 'none';

                const statusDiv = document.getElementById('statusBar');
                statusDiv.style.display = 'block';
                statusDiv.classList.remove('active');
                statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">🎭 Опускаю занавес…</div><div style="font-style:italic;">Режиссёр завершил представление</div>';
                document.getElementById('statusPlaceholder').style.display = 'none';
            }
        }
        
        function shutdownServer() {
            if (confirm('Завершить работу сервера?')) {
                // СРАЗУ останавливаем polling
                stopPolling();
                
                // СРАЗУ скрываем кнопку выхода — чтобы нельзя было нажать повторно
                const exitBtn = document.querySelector('.footer .btn');
                if (exitBtn) exitBtn.style.display = 'none';
                
                // СРАЗУ обновляем UI
                setTurnState('hidden');
                document.getElementById('statusPlaceholder').style.display = 'none';
                
                const statusDiv = document.getElementById('statusBar');
                statusDiv.style.display = 'block';
                statusDiv.classList.remove('active');
                statusDiv.innerHTML = '<div style="text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;">🎭 Театр закрыт</div><div style="font-style:italic;">Спектакль сохранён для просмотра</div>';
                
                // Отправляем запрос на сервер (если он ещё жив)
                fetch('/api/shutdown', {method: 'POST'}).catch(() => {});
            }
        }
        
        // Редактор правил и инструкций наполняется сам при загрузке страницы:
        // вкладка открыта сразу, и отдельной кнопки входа в неё больше нет
        // (сам вызов — в начале скрипта, рядом с refreshMemory)
        function loadInstructionsForEdit() {
            fetch('/api/moderator/instructions')
            .then(r => r.json())
            .then(data => {
                defaultJudgePrompt = data.default_judge_prompt || '';
                renderStaticInstructionsEditor(data.static_instructions);
                renderModeratorMessagesEditor(data.moderator_messages);
                renderJudgeRulesEditor(data.judge_rules || []);
                renderParticipantInstructionsEditor(data.participant_instructions || []);
            })
            .catch(err => console.error('Ошибка загрузки инструкций:', err));
        }
        
        function renderStaticInstructionsEditor(instructions) {
            const container = document.getElementById('staticInstructionsEditor');
            container.innerHTML = instructions.map((instr, idx) => `
                <div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;">
                    <textarea id="static-instr-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;">${instr}</textarea>
                    <button class="btn btn-secondary" onclick="removeStaticInstructionEditor(${idx})" style="padding:8px 12px;margin:0;">❌</button>
                </div>
            `).join('');
        }
        
        function renderModeratorMessagesEditor(messages) {
            const container = document.getElementById('moderatorMessagesEditor');
            container.innerHTML = messages.map((msg, idx) => `
                <div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;">
                    <textarea id="mod-msg-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;">${msg}</textarea>
                    <button class="btn btn-secondary" onclick="removeModeratorMessageEditor(${idx})" style="padding:8px 12px;margin:0;">❌</button>
                </div>
            `).join('');
        }
        
        function renderJudgeRulesEditor(rules) {
            const container = document.getElementById('judgeRulesEditor');
            container.innerHTML = rules.map((rule, idx) => `
                <div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;">
                    <textarea id="judge-rule-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;">${escapeHtml(rule)}</textarea>
                    <button class="btn btn-secondary" onclick="removeJudgeRuleEditor(${idx})" style="padding:8px 12px;margin:0;">❌</button>
                </div>
            `).join('');
        }
        
        function addJudgeRuleEditor() {
            const container = document.getElementById('judgeRulesEditor');
            const idx = 'new' + (++editorRowSeq);
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;';
            div.innerHTML = `
                <textarea id="judge-rule-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Новое правило судьи..."></textarea>
                <button class="btn btn-secondary" onclick="removeJudgeRuleEditor('${idx}')" style="padding:8px 12px;margin:0;">❌</button>
            `;
            container.appendChild(div);
        }
        
        function removeJudgeRuleEditor(idx) {
            const el = document.getElementById(`judge-rule-edit-${idx}`);
            if (el) el.parentElement.remove();
        }
        
        function renderParticipantInstructionsEditor(participantInstructions) {
            const container = document.getElementById('participantInstructionsEditor');
            if (participantInstructions.length === 0) {
                container.innerHTML = '<div style="color:#666;font-style:italic;font-size:13px;">Нет AI-участников для редактирования</div>';
                return;
            }
            container.innerHTML = participantInstructions.map((p, idx) => {
                // Определяем роль участника
                let roleBadge = '<span class="role-badge role-participant">🎭 УЧАСТНИК</span>';
                let borderColor = '#ccc';
                let rows = 3;
                
                if (p.is_judge) {
                    roleBadge = '<span class="role-badge role-judge">⚖️ СУДЬЯ</span>';
                    borderColor = '#7b1fa2';
                    rows = 8; // Больше строк для судьи
                }
                
                // Имя лежит в data-атрибуте: раньше его брали из текста label,
                // а туда попал бейдж роли - и инструкции сохранялись под именем
                // «🎭 УЧАСТНИК Варвара», то есть никогда не применялись
                const value = (p.instruction && p.instruction.trim())
                    ? p.instruction
                    : (p.is_judge ? defaultJudgePrompt : '');
                
                return `
                <div class="instr-card${p.is_judge ? ' judge' : ''}" data-participant="${escapeHtml(p.name)}" style="border:2px solid ${borderColor};">
                    <label style="display:block;font-weight:bold;margin-bottom:5px;font-size:13px;">${roleBadge} ${escapeHtml(p.name)}:</label>
                    ${p.is_judge ? '<div class="instr-hint">Системный промпт судьи (можно редактировать):</div>' : ''}
                    <textarea id="participant-instr-edit-${idx}" rows="${rows}" style="width:100%;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Дополнительная инструкция для ${escapeHtml(p.name)}...">${escapeHtml(value)}</textarea>
                </div>
            `}).join('');
        }
        
        function addStaticInstructionEditor() {
            const container = document.getElementById('staticInstructionsEditor');
            const idx = 'new' + (++editorRowSeq);
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;';
            div.innerHTML = `
                <textarea id="static-instr-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Новая инструкция..."></textarea>
                <button class="btn btn-secondary" onclick="removeStaticInstructionEditor('${idx}')" style="padding:8px 12px;margin:0;">❌</button>
            `;
            container.appendChild(div);
        }
        
        function addModeratorMessageEditor() {
            const container = document.getElementById('moderatorMessagesEditor');
            const idx = 'new' + (++editorRowSeq);
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;';
            div.innerHTML = `
                <textarea id="mod-msg-edit-${idx}" rows="2" style="flex:1;padding:8px;border:1px solid #000;font-size:14px;font-family:Georgia,serif;" placeholder="Новое руководство..."></textarea>
                <button class="btn btn-secondary" onclick="removeModeratorMessageEditor('${idx}')" style="padding:8px 12px;margin:0;">❌</button>
            `;
            container.appendChild(div);
        }
        
        function removeStaticInstructionEditor(idx) {
            const el = document.getElementById(`static-instr-edit-${idx}`);
            if (el) el.parentElement.remove();
        }
        
        function removeModeratorMessageEditor(idx) {
            const el = document.getElementById(`mod-msg-edit-${idx}`);
            if (el) el.parentElement.remove();
        }
        
        // Эмодзи-аватар: клик по значку в ленте открывает набор (сам значок
        // появляется и пропадает вместе с репликами, поэтому слушаем ленту,
        // а не каждый значок по отдельности)
        document.getElementById('posts').addEventListener('click', function(ev) {
            const spot = ev.target.closest('[data-emoji-for]');
            if (spot) openEmojiPicker({name: spot.dataset.emojiFor});
        });
        document.getElementById('emojiMenuGrid').addEventListener('click', function(ev) {
            const one = ev.target.closest('.emoji-choice');
            if (one) chooseEmoji(one.dataset.emoji);
        });

        document.getElementById('topicInput').addEventListener('keydown', function(e) { if (e.ctrlKey && e.key === 'Enter') startDebate(); });
        document.getElementById('moderatorInput').addEventListener('keydown', function(e) { if (e.ctrlKey && e.key === 'Enter') sendModeratorMessage(); });
        const now = new Date();
        document.getElementById('headerDate').textContent = now.toLocaleDateString('ru-RU', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
    </script>
</body>
</html>
"""
