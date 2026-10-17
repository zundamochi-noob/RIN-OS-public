import httpx
import json
from datetime import datetime

from core.config import LATITUDE, LONGITUDE

WEATHER_CODE = {
    0: "快晴", 1: "ほぼ晴れ", 2: "一部曇り", 3: "曇り",
    45: "霧", 48: "霧氷",
    51: "小雨", 53: "雨", 55: "強い雨",
    61: "小雨", 63: "雨", 65: "大雨",
    71: "小雪", 73: "雪", 75: "大雪",
    80: "にわか雨", 81: "雨", 82: "激しいにわか雨",
    95: "雷雨", 96: "雷雨（ひょう）", 99: "激しい雷雨",
}


def code_to_str(code: int) -> str:
    return WEATHER_CODE.get(code, f"天気コード{code}")


async def get_weather() -> str:
    """
    天気の生データをJSON文字列として返す。
    一昨日〜明後日（過去2日＋今日＋未来2日）の5日分を取得する。
    各日には今日からの相対ラベル（昨日・今日・明日など）を付ける。
    どの情報を伝えるか・どう言うかの判断はLLM側に委ねる設計。
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
        "hourly": "temperature_2m,precipitation_probability",
        "timezone": "Asia/Tokyo",
        "past_days": 2,
        "forecast_days": 3,
    }
    async with httpx.AsyncClient() as client:
        res = await client.get(url, params=params)
        data = res.json()

    current = data["current"]
    daily = data["daily"]
    hourly = data["hourly"]

    # 今日の日付（Asia/Tokyo基準）を特定する。
    # past_days=2 を入れると配列の先頭が一昨日になるため、
    # 「今日」が何番目かを日付一致で探す。見つからなければ index 2 を既定とする。
    today = datetime.now().strftime("%Y-%m-%d")
    today_idx = daily["time"].index(today) if today in daily["time"] else 2

    # 今日を基準にした相対ラベル
    labels = {-2: "一昨日", -1: "昨日", 0: "今日", 1: "明日", 2: "明後日"}

    # 5日分の日別予報を生データのまま抜き出す
    days = []
    for i, date in enumerate(daily["time"]):
        offset = i - today_idx
        days.append({
            "date": date,
            "label": labels.get(offset, date),
            "temp_max": daily["temperature_2m_max"][i],
            "temp_min": daily["temperature_2m_min"][i],
            "weather": code_to_str(daily["weather_code"][i]),
            "precip_max": daily["precipitation_probability_max"][i],
        })

    # 今日の時間ごとの予報（6時間ごと）を生データのまま抜き出す
    hourly_today = []
    for i, time in enumerate(hourly["time"]):
        if time.startswith(today) and int(time[11:13]) % 6 == 0:
            hourly_today.append({
                "time": time[11:16],
                "temp": hourly["temperature_2m"][i],
                "precip": hourly["precipitation_probability"][i],
            })

    result = {
        "current": {
            "temp": current["temperature_2m"],
            "weather": code_to_str(current["weather_code"]),
            "wind": current["wind_speed_10m"],
        },
        "today": today,
        "daily": days,
        "hourly_today": hourly_today,
    }

    return json.dumps(result, ensure_ascii=False)
