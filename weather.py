"""
Получение погоды через OpenWeatherMap и советы по одежде.
"""

import requests

WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"


class WeatherError(Exception):
    pass


def fetch_weather(city: str, api_key: str, lang: str = "ru") -> dict:
    params = {"q": city, "appid": api_key, "units": "metric", "lang": lang}
    try:
        resp = requests.get(WEATHER_URL, params=params, timeout=10)
    except requests.RequestException as e:
        raise WeatherError(f"Не удалось получить погоду: {e}") from e

    if resp.status_code == 404:
        raise WeatherError("Город не найден. Проверь название и попробуй ещё раз.")
    if resp.status_code == 401:
        raise WeatherError("Неверный API-ключ OpenWeatherMap.")
    resp.raise_for_status()

    data = resp.json()
    return {
        "temp": round(data["main"]["temp"]),
        "feels_like": round(data["main"]["feels_like"]),
        "description": data["weather"][0]["description"],
        "wind_speed": data.get("wind", {}).get("speed", 0),
        "humidity": data["main"]["humidity"],
        "rain": "rain" in data,
        "snow": "snow" in data,
    }


def clothing_advice(w: dict) -> str:
    t = w["feels_like"]
    advice = []

    if t <= -15:
        advice.append("Очень холодно: тёплый зимний пуховик, шапка, шарф, перчатки, тёплая обувь.")
    elif t <= -5:
        advice.append("Холодно: зимняя куртка, шапка, шарф, тёплая обувь.")
    elif t <= 5:
        advice.append("Прохладно: демисезонная куртка, шапка не помешает.")
    elif t <= 12:
        advice.append("Свежо: лёгкая куртка или плотная кофта.")
    elif t <= 18:
        advice.append("Комфортно: свитер/худи, лёгкая куртка на всякий случай.")
    elif t <= 24:
        advice.append("Тепло: футболка или рубашка, лёгкая одежда.")
    else:
        advice.append("Жарко: максимально лёгкая одежда, головной убор от солнца.")

    if w["rain"]:
        advice.append("Ожидается дождь — возьми зонт или дождевик.")
    if w["snow"]:
        advice.append("Ожидается снег — обувь с хорошим протектором.")
    if w["wind_speed"] >= 8:
        advice.append("Сильный ветер — куртка с капюшоном будет кстати.")

    return " ".join(advice)
