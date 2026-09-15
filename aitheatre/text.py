"""Текст: markdown, формулы и подсчёт токенов.

Формулы прячутся от markdown до разметки — иначе `_` превращается в <em>, а
`\\{` в `{`, и LaTeX приезжает на страницу рассыпанным. Подсчёт токенов решает,
сколько истории влезает в контекст модели.
"""

import html
import re

from . import deps
from . import settings

# ============================================================
# MARKDOWN
# ============================================================

# Формулы: модели пишут их в LaTeX ($...$, $$...$$, \(...\), \[...\]), а markdown
# съедает из такого текста служебные знаки (_ превращает в <em>, \{ — в скобку),
# поэтому формулы прячем до разметки, а в готовый HTML отдаём отдельным элементом
# span.math — его отрисует страница (Temml → MathML, то есть средствами браузера).
MATH_SEGMENT_RE = re.compile(
    r'\$\$(?P<block>.+?)\$\$'                       # $$ ... $$ — формула отдельной строкой
    r'|\\\[(?P<bracket>.+?)\\\]'                     # \[ ... \]
    r'|(?<!\$)\$(?P<inline>[^$\n]+?)\$(?!\$)'        # $ ... $
    r'|\\\((?P<paren>.+?)\\\)',                     # \( ... \)
    re.DOTALL,
)


def _looks_like_math(body: str, display: bool) -> bool:
    """Похоже ли это на формулу, а не на цены в долларах («$5 и $7»)."""
    if display:
        return True
    stripped = body.strip()
    if not stripped:
        return False
    if any(ch in stripped for ch in "\\^_={}"):
        return True
    # Короткая запись без пробелов: f(x), x2, 500 — тоже считаем формулой
    return " " not in stripped


def _protect_math(text: str) -> tuple:
    """Прячет формулы от markdown: возвращает текст с метками и список формул."""
    stashed = []

    def stash(match):
        block = match.group('block') or match.group('bracket')
        inline = match.group('inline') or match.group('paren')
        body = (block if block is not None else inline) or ""
        display = block is not None
        if not _looks_like_math(body, display):
            return match.group(0)
        stashed.append((body.strip(), display))
        return f"\x00MATH{len(stashed) - 1}\x00"

    return MATH_SEGMENT_RE.sub(stash, text), stashed


def _restore_math(rendered: str, stashed: list) -> str:
    """Возвращает формулы в готовый HTML отдельными элементами (LaTeX не экранируем —
    его читает рендерер на странице, а HTML-знаки в нём экранирует он сам)."""
    for index, (latex, display) in enumerate(stashed):
        if display:
            tag = f'<span class="math math-block" data-display="1">{html.escape(latex)}</span>'
        else:
            tag = f'<span class="math" data-display="0">{html.escape(latex)}</span>'
        rendered = rendered.replace(f"\x00MATH{index}\x00", tag)
    return rendered


def markdown_to_html(text: str) -> str:
    text, stashed_math = _protect_math(text)
    if not deps.MARKDOWN_AVAILABLE:
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
        text = re.sub(r'~~(.+?)~~', r'<del>\1</del>', text)
        text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
        text = text.replace('\n', '<br>')
        return _restore_math(text, stashed_math)

    rendered = deps.markdown.markdown(text, extensions=['nl2br'])
    return _restore_math(rendered, stashed_math)

def estimate_tokens(text: str) -> int:
    """
    Подсчёт количества токенов в тексте.
    Если tiktoken доступен - точный подсчёт, иначе приближённая оценка.
    """
    if deps.TIKTOKEN_AVAILABLE and deps.TIKTOKEN_ENCODER:
        # Точный подсчёт через tiktoken
        return len(deps.TIKTOKEN_ENCODER.encode(text))
    else:
        # Fallback: приближённая оценка. Кириллица в cl100k режется примерно по 2
        # символа на токен, латиница и пунктуация - по 4. Лучше переоценить:
        # заниженный подсчёт ведёт к переполнению контекста модели.
        cyrillic = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")
        return max(1, cyrillic // 2 + (len(text) - cyrillic) // 4)

def trim_history_by_tokens(messages: list, system_prompt_tokens: int, model: str = "") -> list:
    """
    Обрезает историю сообщений на основе подсчёта токенов.
    Возвращает обрезанный список сообщений.

    Окно берётся у той модели, которая будет говорить (см.
    settings.context_budget): у облачного участника оно своё, у местного —
    из OPTIONS. Иначе история любой сцены мерилась бы олламовскими 7 тысячами
    токенов, и облачные участники забывали бы начало разговора.
    """
    num_ctx, num_predict = settings.context_budget(model)
    # В консоли видно, чьё это окно: «доступно: 24076 из окна облака 32768».
    # Без этой подписи по строке в логе невозможно понять, почему история
    # обрезается — а именно на это и уходило время при разборе облака
    window_note = (f" из окна облака {num_ctx}"
                   if str(model or "").startswith(settings.CLOUD_MODEL_PREFIX) else "")

    # Подсчитываем токены в каждом сообщении
    messages_with_tokens = []
    for msg in messages:
        content = msg.get("content", "")
        tokens = estimate_tokens(content)
        messages_with_tokens.append((msg, tokens))

    # Подсчитываем общие токены
    total_tokens = sum(tokens for _, tokens in messages_with_tokens)

    # Окно не ограничено (CLOUD_NUM_CTX = 0): история остаётся целой
    if num_ctx <= 0:
        print(f"  📊 История: {total_tokens} токенов (окно облака не ограничено) ✅")
        return messages

    # Вычисляем доступное пространство для истории
    available_tokens = max(
        0,
        num_ctx - num_predict - settings.CONTEXT_SAFETY_MARGIN - system_prompt_tokens
    )

    # Если вписываемся - возвращаем всё
    if total_tokens <= available_tokens:
        print(f"  📊 История: {total_tokens} токенов (доступно: {available_tokens}{window_note}) ✅")
        return messages

    # Обрезаем старые сообщения, пока не влезем
    print(f"  📊 История: {total_tokens} токенов (доступно: {available_tokens}{window_note}) ⚠️ Обрезка...")
    
    trimmed_messages = []
    current_tokens = 0
    
    # Идём с конца (новые сообщения важнее)
    for msg, tokens in reversed(messages_with_tokens):
        if current_tokens + tokens <= available_tokens:
            trimmed_messages.insert(0, msg)
            current_tokens += tokens
        else:
            break
    
    removed_count = len(messages) - len(trimmed_messages)
    print(f"  ✂️  Удалено {removed_count} сообщений, осталось {len(trimmed_messages)} ({current_tokens} токенов)")
    
    return trimmed_messages
