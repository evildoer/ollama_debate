"""Разовая ручная проверка одной модели Ollama: скачана ли она, что умеет.

Запуск (при активном виртуальном окружении):

    python test.py            # проверит модель из MODEL_NAME ниже
    python test.py q2         # или любую, указанную в аргументе

Приложение делает те же проверки само: при старте пишет в лог, все ли модели
на месте, а поддержку инструментов и размышлений видно в пульте и в сайдбаре.
Этот файл — для быстрой разовой проверки, поэтому он зависит только от
стандартной библиотеки: ничего доустанавливать не нужно.
"""

import json
import sys
import urllib.error
import urllib.request

MODEL_NAME = "g1"
SHOW_URL = "http://localhost:11434/api/show"


def ask_ollama(model: str) -> dict:
    """Спрашивает у Ollama сведения о модели. Бросает RuntimeError с понятным текстом."""
    payload = json.dumps({"name": model}).encode("utf-8")
    request = urllib.request.Request(
        SHOW_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        # На отсутствующую модель Ollama отвечает 400 (не 404) с текстом
        # вида {"error":"model 'x' not found"} — важно сказать это человеку
        if error.code in (400, 404) or "not found" in body.lower():
            raise RuntimeError(f"модели «{model}» нет в Ollama — скачайте: "
                               f"ollama pull {model}") from error
        raise RuntimeError(f"Ollama ответила ошибкой {error.code}: {body[:200]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"нет связи с Ollama ({SHOW_URL}): {error.reason}. "
                           "Проверьте, что она запущена") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Ollama ответила не JSON") from error


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else MODEL_NAME
    try:
        info = ask_ollama(model)
    except RuntimeError as error:
        print(f"❌ {error}")
        return 1

    capabilities = info.get("capabilities") or []
    has_tools = "tools" in capabilities
    has_thinking = "thinking" in capabilities

    print(f"Модель: {model}")
    print(f"Возможности: {', '.join(capabilities) if capabilities else '—'}")
    print(f"{'✅' if has_tools else '❌'} Поиск в интернете (Tools): "
          f"{'поддерживается' if has_tools else 'НЕ поддерживается'}")
    print(f"{'✅' if has_thinking else '❌'} Размышления: "
          f"{'поддерживаются' if has_thinking else 'НЕ поддерживаются'}")
    if not has_tools:
        print("   Это нормально: такая модель просто не будет искать в интернете.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
