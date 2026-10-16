"""Поиск публикаций в Threads через официальный Threads API (keyword_search).

Ограничения Meta: нужен токен пользователя с разрешениями threads_basic и
threads_keyword_search. Пока приложение не прошло App Review по
threads_keyword_search, поиск возвращает только собственные публикации
владельца токена. Лимит — 2 200 запросов за 24 часа на пользователя.
"""
import os
import re
import time
import urllib.parse

from ..http import get_json

API = "https://graph.threads.net/v1.0/keyword_search"
FIELDS = "id,text,permalink,timestamp,username,media_type"

PLACES = ["emaar beachfront", "beachfront", "dubai harbour", "palm jumeirah", "palm", "balqis", "jbr",
          "jumeirah beach residence", "dubai hills", "sidra", "maple", "marina", "downtown", "business bay",
          "пальм", "бичфронт", "дубай хиллс", "марина"]


def search(keywords, since_ts, log=print):
    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        log("[threads] THREADS_ACCESS_TOKEN не задан — источник пропущен")
        return []
    out = {}
    for kw in keywords:
        q = {"q": kw, "search_type": "RECENT", "fields": FIELDS, "limit": 50,
             "since": int(since_ts), "access_token": token}
        try:
            data = get_json(API + "?" + urllib.parse.urlencode(q))
        except Exception as e:  # noqa: BLE001
            log(f"[threads] «{kw}»: ошибка {e}")
            continue
        for p in data.get("data", []):
            if p.get("text"):
                out[p["id"]] = {"id": f"th:{p['id']}", "url": p.get("permalink"), "text": p["text"],
                                "username": p.get("username"), "timestamp": p.get("timestamp")}
        time.sleep(1)
    return list(out.values())


def extract(text):
    t = text.lower()
    prices = []
    for m in re.finditer(r"(\d+(?:[.,]\d+)*)\s*(m\b|mn\b|million|млн|k\b)?\s*(aed|dhs|dirham|дирх)", t):
        prices.append(_to_num(m.group(1), m.group(2)))
    for m in re.finditer(r"(aed|dhs)\s*(\d+(?:[.,]\d+)*)\s*(m\b|mn\b|million|млн|k\b)?", t):
        prices.append(_to_num(m.group(2), m.group(3)))
    prices = sorted({p for p in prices if p and 300_000 <= p <= 100_000_000})
    places = [p for p in PLACES if p in t]
    return {"prices": prices, "places": places}


def _to_num(num, unit):
    num = num.replace(",", "") if unit is None else num.replace(",", ".")
    try:
        v = float(num)
    except ValueError:
        return None
    if unit and unit.startswith(("m", "млн")):
        v *= 1_000_000
    elif unit == "k":
        v *= 1_000
    return v
