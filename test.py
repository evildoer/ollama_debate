import requests

model_name = "g1"  # Имя вашей локальной модели
url = "http://localhost:11434/api/show"

try:
    response = requests.post(url, json={"name": model_name})
    data = response.json()
    
    # Ищем упоминание tools в возможностях модели
    capabilities = data.get("capabilities", [])
    if "tools" in capabilities:
        print(f"✅ Локальная модель {model_name} поддерживает Tools.")
    else:
        print(f"❌ Локальная модель {model_name} НЕ поддерживает Tools.")
except Exception as e:
    print(f"Не удалось подключиться к Ollama: {e}")
