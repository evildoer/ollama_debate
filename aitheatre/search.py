"""Поиск в интернете: DuckDuckGo через прокси, а если он не помог — напрямую.

Один и тот же поиск нужен двум разным делам: модель ищет факты для реплики,
а театр — картинку для грима. Поэтому он живёт отдельно и от Ollama, и от
аватаров: сначала три попытки через прокси (если он включён), потом три напрямую.
Молчаливых отказов здесь нет — каждый неудачный заход виден в логе.
"""

import time

from . import deps
from . import settings


def proxy_modes() -> list:
    """Порядок сетевых попыток: сначала через прокси (если включён), затем напрямую."""
    return [settings.PROXY, None] if settings.PROXY else [None]


def ddgs_search(kind: str, method: str, query: str, max_results: int = 5) -> tuple:
    """
    Ищет через ddgs: 3 попытки через прокси (если включён), затем 3 попытки напрямую.
    Возвращает (results, last_error).
    """
    last_error = None

    for proxy in proxy_modes():
        mode = f"через прокси {proxy}" if proxy else "без прокси"
        if proxy is None and settings.PROXY:
            print(f"  🔄 {kind}: прокси не помог, пробую без прокси...")

        for attempt in range(3):
            try:
                ddgs_kwargs = {"proxy": proxy} if proxy else {}
                with deps.DDGS(**ddgs_kwargs) as ddgs:
                    results = list(getattr(ddgs, method)(
                        query,
                        safesearch="off",
                        max_results=max_results
                    ))

                if proxy is None and settings.PROXY:
                    print(f"  ✅ {kind} без прокси успешен!")
                return results, None
            except Exception as e:
                last_error = e
                if attempt < 2:
                    print(f"  ⚠️  {kind} ({mode}): попытка {attempt + 1} не удалась: {e}, пробую ещё раз...")
                    time.sleep(2)
                else:
                    print(f"  ⚠️  {kind} ({mode}): ошибка после 3 попыток: {e}")

    return [], last_error
