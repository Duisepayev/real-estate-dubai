"""Поиск публикаций в Threads.

1. Официальный Threads API (keyword_search), если задан THREADS_ACCESS_TOKEN.
   Нужны разрешения threads_basic и threads_keyword_search; пока приложение не
   прошло App Review по threads_keyword_search, поиск видит только посты
   владельца токена. Лимит — 2 200 запросов за 24 часа.
2. Иначе — актор Apify (APIFY_TOKEN), который читает публичный поиск Threads.
   Это сторонний сервис с оплатой за пост; правила Meta автоматический сбор
   не разрешают.
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


def search(keywords, since_ts, cfg=None, log=print):
    cfg = cfg or {}
    if os.environ.get("THREADS_ACCESS_TOKEN"):
        return search_official(keywords, since_ts, log)
    if os.environ.get("APIFY_TOKEN"):
        return search_apify(keywords, since_ts, cfg.get("apify_actor"), cfg.get("max_posts", 60), log)
    log("[threads] нет THREADS_ACCESS_TOKEN и APIFY_TOKEN — источник пропущен")
    return []


def search_apify(keywords, since_ts, actor, max_posts, log=print):
    from ..http import request
    import json
    url = (f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?"
           + urllib.parse.urlencode({"token": os.environ["APIFY_TOKEN"], "timeout": 240}))
    try:
        items = json.loads(request(url, data={"searchKeywords": keywords, "sort": "recent", "maxItems": max_posts},
                                   timeout=300, retries=1))
    except Exception as e:  # noqa: BLE001
        log(f"[threads] Apify: ошибка {e}")
        return []
    out = {}
    for p in items if isinstance(items, list) else []:
        ts = p.get("takenAt") or 0
        if not p.get("text") or (ts and ts < since_ts):
            continue
        pid = str(p.get("postId") or p.get("code") or p.get("url"))
        out[pid] = {"id": f"th:{pid}", "url": p.get("url"), "text": p["text"], "username": p.get("username"),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else ""}
    log(f"[threads] Apify: получено {len(items) if isinstance(items, list) else 0}, новых {len(out)}")
    return list(out.values())


def search_official(keywords, since_ts, log=print):
    token = os.environ["THREADS_ACCESS_TOKEN"]
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
