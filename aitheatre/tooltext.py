"""Просьба о поиске, написанная словами.

Обычно просьба о поиске приходит полем tool_calls — на этом стоит протокол
инструментов, и так её отдают почти все шлюзы. Но некоторые облачные модели
печатают вызов прямо в тексте реплики:

    search:web_search{query: "Сыктывкар новости сейчас"}
    call:google_search:search{queries:["последние новости Сыктывкар"]}

Для приложения это выглядело обычной репликой. Сырой вызов уезжал в пост
спектакля (в ленте такие посты и видели), поиск не выполнялся вовсе — и поэтому
же в посте не появлялось «Источников», хотя модель именно их и просила.

Здесь такие просьбы узнаются: вызов вынимается из текста, а запросы отдаются
наружу. Поиск по ним выполняет уже код хода (см. ollama_api.ask_model), и
результаты уходят модели обычным сообщением — не ответом инструмента: модель,
не удержавшая протокол, на ответ инструмента с чужим id ответит отказом.
"""

import re

# Своих имён у моделей много (search_web, web_search, google_search,
# browser_search, internet_search), и перечислять их — значит отстать от
# следующей модели. Решает слово «search» в имени инструмента
_SEARCH_WORD = "search"

# Имя инструмента — это то, что стоит перед фигурной скобкой: «web_search{»,
# «call:google_search:search{». Двоеточия и точки внутри имени разрешены
_NAME_TAIL = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]*$")

# Внутри вызова запрос называется по-разному: query — у нашего инструмента,
# queries — у моделей Google, q — у поисковиков
_QUERY_KEY = re.compile(
    r"[\"']?(query|queries|search_query|query_string|q|text)[\"']?\s*[:=]\s*", re.I)

# Вызов, завёрнутый в теги: <tool_call>{"name": "search_web", ...}</tool_call>
_TAG = re.compile(r"<\|?(tool_call|function_call|tool_code)\|?>(.*?)<\|?/\1\|?>",
                  re.I | re.S)

# Имя инструмента внутри блока: {"name": "search_web", "arguments": {...}}
_JSON_NAME = re.compile(r"[\"']name[\"']\s*:\s*[\"']([^\"']*)[\"']", re.I)


def _closing(text: str, start: int):
    """Где кончается блок, открытый скобкой в позиции start (None — не кончился).

    Скобки считаются все вместе: {…[ … ]…} — это один блок, а не два. Кавычки
    уважаются, иначе скобка из строки запроса закрыла бы блок на середине.
    """
    depth = 0
    quote = ""
    i = start
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
            if depth <= 0:
                return i
        i += 1
    return None


def _quoted(text: str) -> list:
    """Строки в кавычках из куска: ими и записаны запросы."""
    found = []
    quote = ""
    start = 0
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                found.append(text[start:i].strip())
                quote = ""
        elif char in "\"'":
            quote = char
            start = i + 1
        i += 1
    return [text for text in found if text]


def _values_after(block: str, at: int) -> list:
    """Значение ключа запроса: строка, список строк или слово без кавычек."""
    i = at
    while i < len(block) and block[i] in " \t\r\n":
        i += 1
    if i >= len(block):
        return []
    if block[i] == "[":
        end = block.find("]", i)
        return _quoted(block[i + 1:end if end > 0 else len(block)])
    if block[i] in "\"'":
        return _quoted(block[i:])
    # Слово без кавычек: редкость, но у модели и такое бывает
    stops = [pos for pos in (block.find(char, i) for char in ",}\n") if pos > 0]
    word = block[i:min(stops) if stops else len(block)].strip().strip("\"'")
    return [word] if len(word) > 1 else []


def _queries_in(block: str) -> list:
    """Запросы, названные внутри вызова. Пусто — значит не про поиск."""
    for match in _QUERY_KEY.finditer(block):
        values = _values_after(block, match.end())
        if values:
            return values
    return []


def _looks_like_search(name: str, block: str) -> bool:
    """Вызов это или обычная фигурная скобка в тексте.

    Одно слово «search» в имени — почти всегда вызов: в обычной реплике перед
    скобкой так не пишут. Вторая форма — целый объект в форме инструмента
    ({"name": "search_web", "arguments": …}) без имени перед скобкой.
    """
    if _SEARCH_WORD in name.lower():
        return True
    named = _JSON_NAME.search(block)
    return bool(named and _SEARCH_WORD in named.group(1).lower())


def _spans(text: str) -> list:
    """Найденные просьбы: (начало, конец, запросы) в порядке появления.

    Незакрытый вызов в самом конце текста тоже вынимается: он бывает либо
    недописанным черновиком (модель печатает вызов прямо сейчас), либо следом
    оборванного по времени хода. В реплике ему делать нечего в обоих случаях.
    """
    found = []
    for match in _TAG.finditer(text):
        found.append((match.start(), match.end(), _queries_in(match.group(2))))

    for position, char in enumerate(text):
        if char != "{":
            continue
        if any(start <= position < end for start, end, _ in found):
            continue        # эту скобку уже накрыл вызов целиком
        name_start = _NAME_TAIL.search(text[:position])
        name_start = name_start.start() if name_start else position
        name = text[name_start:position].strip().strip("\"'")
        end = _closing(text, position)
        if end is None:
            if _SEARCH_WORD in name.lower():
                found.append((name_start, len(text), []))
            continue
        block = text[position:end + 1]
        if _looks_like_search(name, block):
            found.append((name_start, end + 1, _queries_in(block)))

    return sorted(found)


def take_calls(text: str) -> tuple:
    """Отдаёт (реплику без просьбы о поиске, список запросов из неё).

    Пустой список запросов значит, что вызов узнан, но запрос в нём не прочитан
    (или вызов не дописан): из реплики он всё равно убирается, а поиск просто
    не выполняется.
    """
    text = text or ""
    spans = _spans(text)
    queries = []
    for _start, _end, found in spans:
        queries.extend(found)

    for start, end, _found in reversed(spans):
        text = text[:start] + text[end:]

    # Дырку на месте вызова надо убрать: иначе в посте остались бы пустые
    # строки там, где модель просила поиск
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), _unique(queries)


def _unique(queries: list) -> list:
    """Запросы без повторов, но в том порядке, в каком их просила модель."""
    seen, result = set(), []
    for query in queries:
        key = query.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(query.strip())
    return result
