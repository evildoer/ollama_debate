#!/usr/bin/env python3
"""
Ollama AI Debate - Современный веб-интерфейс с WebSocket
=========================================================

Установка (подробности — в README.md):
    python -m venv venv && venv\\Scripts\\activate     # Windows
    source venv/bin/activate                          # Linux и macOS
    python -m pip install -r requirements.txt

Запуск:
    python ollama_debate_web.py

Сам театр живёт в пакете aitheatre/ (настройки, страница, спектакль, Ollama,
аватары — см. aitheatre/__init__.py). Этот файл остался точкой входа: он
запускает сервер и открывает браузер, чтобы привычная команда не менялась.
"""

from aitheatre.web import main

if __name__ == "__main__":
    main()
