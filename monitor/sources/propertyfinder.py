"""Сбор объявлений Property Finder из публичных страниц поиска.

Официального API для чтения объявлений покупателем у Property Finder нет
(партнёрский API предназначен агентствам для публикации). Страница поиска
отдаёт данные в JSON (__NEXT_DATA__) — его и разбираем, с паузами между
запросами и небольшим числом страниц.
"""
import json
import re
import time
import urllib.parse

from ..http import request

BASE = "https://www.propertyfinder.ae/en/search"
SQFT_TO_SQM = 0.09290304

# Коды типов объектов в URL Property Finder (параметр t)
PF_TYPES = {"apartment": 1, "villa": 35, "townhouse": 22, "penthouse": 20}

_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def search_url(location_id, filters, page=1, sort="nd", deal_badge=None, ptype=None):
    q = [("c", "1"), ("l", str(location_id)), ("ob", sort), ("page", str(page))]
    if filters.get("min_price_aed"):
        q.append(("pf", str(int(filters["min_price_aed"]))))
    if filters.get("max_price_aed"):
        q.append(("pt", str(int(filters["max_price_aed"]))))
    for b in filters.get("bedrooms") or []:
        q.append(("bdr[]", str(b)))
    if ptype:
        q.append(("t", str(PF_TYPES[ptype])))
    if deal_badge:
        q.append(("db[]", deal_badge))
    return BASE + "?" + urllib.parse.urlencode(q)


def fetch_page(url):
    html = request(url)
    m = _NEXT.search(html)
    if not m:
        raise RuntimeError(f"нет __NEXT_DATA__ на {url}")
    d = json.loads(m.group(1))
    sr = d["props"]["pageProps"].get("searchResult") or {}
    return sr.get("listings") or [], sr.get("meta") or {}, d["props"]["pageProps"].get("filtersValueFromQuery")


_DELIVERY_TEXT = re.compile(r"(?:Delivery\s+Date|Handover|Completion\s+Date)\W{0,40}?((?:Q[1-4]|[A-Z][a-z]{2,8})?\s*20\d{2}(?:-\d{2}(?:-\d{2})?)?)")


def detail_handover(url):
    """Срок сдачи со страницы объявления (в выдаче поиска его нет): поля delivery/handover
    в __NEXT_DATA__, иначе текст «Delivery Date: Q2 2027» из блока проекта."""
    html = request(url)
    m = _NEXT.search(html)
    fields = _handover_fields(json.loads(m.group(1))) if m else []
    if not fields:
        fields = [("text", t.group(1)) for t in _DELIVERY_TEXT.finditer(re.sub(r"<[^>]+>", " ", html))][:3]
    return fields


def _bedrooms(p):
    v = p.get("bedrooms_value", p.get("bedrooms"))
    if v in (None, ""):
        return None
    if str(v).lower() in ("studio", "0"):
        return 0
    try:
        return int(str(v).replace("+", ""))
    except ValueError:
        return None


def _handover_fields(obj, path=""):
    """Поля со сроком сдачи в данных PF (delivery_date, handover, completion_date…), как пары (ключ, значение)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{path}.{k}" if path else k
            if isinstance(v, (dict, list)):
                out += _handover_fields(v, key)
            elif v and isinstance(v, (str, int)) and re.search(r"deliver|handover|completion_date|completion_year", k, re.I):
                out.append((key, str(v)))
    elif isinstance(obj, list):
        for x in obj[:20]:
            out += _handover_fields(x, path)
    return out


def normalize(item):
    """Приводит объявление PF к общей схеме."""
    p = item.get("property") or {}
    if not p.get("id"):
        return None
    price = (p.get("price") or {}).get("value")
    size = p.get("size") or {}
    size_sqm = None
    if size.get("value"):
        size_sqm = size["value"] * (SQFT_TO_SQM if size.get("unit", "sqft") == "sqft" else 1)
    loc = p.get("location") or {}
    tree = p.get("location_tree") or []
    names = [t.get("name") for t in tree if t.get("name")]
    community = next((t["name"] for t in tree if t.get("type") == "COMMUNITY"), None)
    # названия ниже уровня комьюнити — для сопоставления с проектами DLD
    below = [t["name"] for t in tree if t.get("type") not in ("CITY", "COMMUNITY") and t.get("name")]
    if loc.get("name") and loc.get("type") not in ("CITY", "COMMUNITY") and loc["name"] not in below:
        below.append(loc["name"])
    sub = next((t["name"] for t in tree if t.get("type") == "SUBCOMMUNITY"), None)
    tower = loc.get("name") if loc.get("type") in ("TOWER", "SUBCOMMUNITY") else None
    ptype = (p.get("property_type") or "").lower()
    return {
        "source": "propertyfinder",
        "id": f"pf:{p['id']}",
        "url": p.get("share_url") or ("https://www.propertyfinder.ae" + (p.get("details_path") or "")),
        "title": p.get("title") or "",
        "description": p.get("description") or "",
        "price_aed": float(price) if price else None,
        "price_hidden": bool((p.get("price") or {}).get("is_hidden")),
        "size_sqm": round(size_sqm, 1) if size_sqm else None,
        "bedrooms": _bedrooms(p),
        "bathrooms": p.get("bathrooms_value", p.get("bathrooms")),
        "property_type": ptype,
        # PF пишет off_plan, off_plan_primary («Offplan: First Sale»), off_plan_secondary и т.п.
        "completion": "off_plan" if str(p.get("completion_status") or "").startswith("off_plan") else "ready",
        "completion_raw": p.get("completion_status"),
        "handover_fields": _handover_fields(p),
        "furnished": p.get("furnished"),
        "location_full": loc.get("full_name") or ", ".join(reversed(names)),
        "community": community,
        "subcommunity": sub,
        "tower": tower,
        "match_names": below,
        "listed_at": p.get("listed_date"),
        "refreshed_at": p.get("last_refreshed_at"),
        "agent": (p.get("agent") or {}).get("name"),
        "broker": (p.get("broker") or {}).get("name"),
        "reference": p.get("reference"),
        "pf_flags": {k: bool(p.get(k)) for k in ("is_price_drop", "is_great_value", "is_verified",
                                                 "is_direct_from_developer", "is_under_offer_by_competitor")},
        "payment_method": p.get("payment_method") or [],
    }


def _matches(n, filters):
    if not n or not n["price_aed"]:
        return False
    if filters.get("min_price_aed") and n["price_aed"] < filters["min_price_aed"]:
        return False
    if filters.get("max_price_aed") and n["price_aed"] > filters["max_price_aed"]:
        return False
    beds = filters.get("bedrooms")
    if beds and n["bedrooms"] not in beds and not (n["bedrooms"] is not None and 7 in beds and n["bedrooms"] > 7):
        return False
    types = filters.get("property_types")
    if types and n["property_type"] not in types:
        return False
    if filters.get("completion") and n["completion"] not in filters["completion"]:
        return False
    if filters.get("min_size_sqm") and (n["size_sqm"] or 0) < filters["min_size_sqm"]:
        return False
    return True


def collect(search, filters, max_pages, delay, seen_ids=None, log=print):
    """Обходит страницы «новые сначала». Если seen_ids передан, останавливается,
    когда на странице не осталось новых объявлений (быстрый режим)."""
    out = {}
    for page in range(1, max_pages + 1):
        url = search_url(search["pf_location_id"], filters, page=page)
        try:
            items, meta, fq = fetch_page(url)
        except Exception as e:  # noqa: BLE001 — один сбой не должен ронять весь запуск
            log(f"[pf] {search['name']} стр.{page}: ошибка {e}")
            break
        if page == 1:
            log(f"[pf] {search['name']}: всего {meta.get('total_count')} по фильтру, страниц {meta.get('page_count')}")
        new_here = 0
        for it in items:
            n = normalize(it)
            if n and _matches(n, filters):
                n["search"] = search["name"]
                if n["id"] not in out:
                    out[n["id"]] = n
                    if not seen_ids or n["id"] not in seen_ids:
                        new_here += 1
        if not items or page >= (meta.get("page_count") or 1):
            break
        if seen_ids is not None and new_here == 0:
            break
        time.sleep(delay)
    return list(out.values())
