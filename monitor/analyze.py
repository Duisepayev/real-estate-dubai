"""Оценка объявления: дисконт к сделкам DLD, признаки срочности, риски, расчёт денег."""
import re
import statistics

from . import dld

URGENCY = [
    r"distress(ed)?\s*(sale|deal)?", r"urgent(ly)?\s*sale", r"urgent", r"motivated\s+seller",
    r"must\s+sell", r"below\s+(the\s+)?market", r"below\s+(the\s+)?original\s+price",
    r"below\s+op\b", r"price\s+(reduced|drop(ped)?)", r"reduced\s+price", r"cash\s+(seller|buyers?\s+only)",
    r"quick\s+sale", r"fire\s+sale", r"lowest\s+price", r"bank\s+(auction|repossess)", r"leaving\s+(the\s+)?(uae|country)",
    r"срочн\w*\s+прода\w*", r"ниже\s+рынка", r"срочно", r"мотивирован\w*\s+продав\w*",
]
# Что может объяснять низкую цену
RISKS = {
    "tenanted": (r"\btenanted\b|\brented\b|tenant|lease\s+until|with\s+tenant|арендатор", "есть арендатор — сроки освобождения и доход нужно проверить"),
    "payment_plan": (r"payment\s+plan|post[\s-]?handover|remaining\s+(balance|payments?)|paid\s+\d+\s*%|\d+\s*%\s+paid|outstanding|balance\s+to\s+(the\s+)?developer", "возможны оставшиеся платежи застройщику — цена может быть неполной"),
    "assignment": (r"assignment|resale\s+off[\s-]?plan|oqood", "переуступка off-plan: проверить oqood, % оплаты и сбор застройщика за NOC"),
    "auction": (r"auction|repossess", "аукцион/изъятие: особые условия и депозит"),
    "share": (r"share\s+of|fractional|co[\s-]?own", "долевая/частичная продажа"),
    "view": (r"no\s+view|road\s+view|facing\s+(the\s+)?(road|construction)|low\s+floor|back[\s-]?facing", "вероятно слабый вид или низкий этаж"),
    "condition": (r"needs?\s+(renovation|work)|original\s+condition|fixer|shell\s+and\s+core", "требуется ремонт"),
    "leasehold": (r"leasehold", "leasehold, не freehold"),
}


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_HANDOVER = re.compile(
    r"(?:hand[\s-]?over|handing\s+over|completion|delivery|ready\s+(?:in|by)|\bho\b|сдача|сдачи)"
    r"[\s:\-–]*(?:in|by|on|date|expected|est\.?)?[\s:\-–]*"
    r"(?:(q[1-4])|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|(\d{1,2})[/.])?[\s,\-–]*('\d{2}|20\d{2})\b")


def find_handover(text):
    """Срок сдачи off-plan из текста объявления: 'Handover Q4 2026', 'handover June 2027', 'HO 2028'.
    Возвращает ISO-дату конца периода или None."""
    import calendar
    best = None
    for m in _HANDOVER.finditer((text or "").lower()):
        q, mon, num, yr = m.groups()
        yr = yr.lstrip("'")
        year = int(yr) + 2000 if len(yr) == 2 else int(yr)
        if not 2024 <= year <= 2035:
            continue
        if q:
            month = int(q[1]) * 3
        elif mon:
            month = _MONTHS[mon[:3]]
        elif num and 1 <= int(num) <= 12:
            month = int(num)
        else:
            month = 12
        d = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
        best = max(best or d, d)
    return best


def handover_from_fields(fields):
    """Срок сдачи из полей PF: ISO-дата ('2027-06-30'), 'Q2 2027', 'June 2027', '2027'."""
    import calendar
    best = None
    for _, v in fields or []:
        m = re.match(r"\s*(20\d{2})-(\d{1,2})", v)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            d = f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
        else:
            d = find_handover("handover " + v)
        if d:
            best = max(best or d, d)
    return best


def find_keywords(text):
    t = (text or "").lower()
    found = set()
    for p in URGENCY:
        m = re.search(p, t)
        if m:
            found.add(m.group(0).strip())
    # «urgent» не показываем, если уже есть «urgent sale»
    return sorted(k for k in found if not any(k != o and k in o for o in found))


def find_risks(text):
    t = (text or "").lower()
    return [msg for key, (p, msg) in RISKS.items() if re.search(p, t)]


def costs(price, cfg, completion="ready", emirate="dubai"):
    """Предварительная сумма собственных средств и платёж. Ставки — примеры."""
    f = cfg["finance"]
    dp_pct = f["down_payment_pct"]
    # Минимальный взнос по правилам ЦБ ОАЭ для экспата: 20% до 5 млн, 30% выше; off-plan — 50%
    reg_min = 0.5 if completion == "off_plan" else (0.2 if price <= 5_000_000 else 0.3)
    dp_pct = max(dp_pct, reg_min)
    loan = price * (1 - dp_pct)
    # регистрация: Дубай — DLD 4%, Абу-Даби — ADREC 2%
    reg_pct = 0.02 if emirate == "abu_dhabi" else 0.04
    dld_fee = price * reg_pct + f["dld_admin_aed"]
    agency = price * f["agency_fee_pct"] * 1.05
    bank = loan * f["bank_fee_pct"] * 1.05
    mreg = loan * f["mortgage_reg_pct"] + f["mortgage_reg_admin_aed"]
    fees = dld_fee + agency + f["trustee_fee_aed"] + bank + f["valuation_fee_aed"] + mreg
    n = f["term_years"] * 12
    pays = []
    for r in f["rates"]:
        i = r / 12
        pays.append((r, loan * i / (1 - (1 + i) ** -n)))
    return {"dp_pct": dp_pct, "down": price * dp_pct, "loan": loan, "fees": fees, "dld_fee": dld_fee,
            "agency": agency, "reg_pct": reg_pct, "emirate": emirate, "bank": bank, "own_funds": price * dp_pct + fees, "payments": pays,
            "cash_total": price + dld_fee + agency + f["trustee_fee_aed"]}


def value_comps(cs, size, a):
    """Сводка по сделкам для оценки. Если продавались квартиры почти той же площади,
    сравниваем только с ними: у больших квартир цена за м² ниже, и сравнение по м²
    с меньшими давало ложный дисконт (Avida 189 м² против сделок 145–158 м²).
    Иначе цену за м² каждой сделки поправляем на разницу площадей."""
    tol = a.get("same_size_tolerance", 0.05)
    near = [c for c in cs if abs(c["size_sqm"] - size) <= size * tol]
    if near:
        return dld.summarize(near), "same_size"
    k = a.get("size_elasticity", 0.2)
    adj = [dict(c, ppsqm=c["ppsqm"] * (c["size_sqm"] / size) ** k) for c in cs]
    return dld.summarize(adj), "size_adjusted"


def evaluate(n, dld_db, projects, store, cfg):
    a = cfg["alerts"]
    text = f"{n.get('title', '')}\n{n.get('description', '')}"
    res = {"keywords": find_keywords(text), "risks": find_risks(text),
           "handover": (handover_from_fields(n.get("handover_fields")) or find_handover(text))
                       if n.get("completion") == "off_plan" else None, "project": None, "match_score": 0,
           "comps": None, "comp_mode": None, "basis": "project", "comp_list": [], "listing_peers": None, "discount": None, "fair": None}
    if n.get("price_aed") and n.get("size_sqm"):
        res["ppsqm"] = n["price_aed"] / n["size_sqm"]
    if any(s.get("emirate") == "abu_dhabi" for s in cfg["searches"] if s["name"] == n.get("search")):
        dld_db = None  # сделки Абу-Даби в DLD не попадают; не сравниваем с одноимёнными проектами Дубая
    names = n.get("match_names") or [x for x in [n.get("tower"), n.get("subcommunity")] if x]
    if names and dld_db is not None:
        areas = next((s.get("dld_areas") for s in cfg["searches"] if s["name"] == n.get("search")), None)
        proj, score = projects.match(names, areas)
        res["project"], res["match_score"] = proj, score
        if proj and n.get("size_sqm"):
            cs = dld.comps(dld_db, proj, n, months=a["comps_months"], size_tol=a["size_tolerance"])
            res["comp_list"] = sorted(cs, key=lambda c: c["date"], reverse=True)
            res["comps"], res["comp_mode"] = value_comps(cs, n["size_sqm"], a) if cs else (None, None)
    if not res["comps"] and not res["project"] and dld_db is not None and n.get("size_sqm"):
        # проект не найден: сравниваем со сделками района без проекта (старые дома), а не с объявлениями
        areas = next((s.get("dld_areas") for s in cfg["searches"] if s["name"] == n.get("search")), None)
        cs = dld.comps(dld_db, None, n, months=a["comps_months"], size_tol=a["size_tolerance"], areas=areas)
        if cs:
            res["basis"] = "area"
            res["comp_list"] = sorted(cs, key=lambda c: c["date"], reverse=True)
            res["comps"], res["comp_mode"] = value_comps(cs, n["size_sqm"], a) if cs else (None, None)
    peers = store.active_peers(n) if store else []
    if peers:
        res["listing_peers"] = {"n": len(peers), "median": statistics.median(peers), "min": min(peers)}
    c = res["comps"]
    if c and res.get("ppsqm"):
        size = n["size_sqm"]
        res["fair"] = (c["p25"] * size, c["median"] * size, c["p75"] * size)
        res["discount"] = 1 - res["ppsqm"] / c["median"]
    res["verdict"] = verdict(n, res, a)
    late = cfg["filters"].get("latest_handover")
    if late and res["handover"] and res["handover"] > late:
        res["verdict"] = "skip"  # сдача позже согласованного срока
    elif late and n.get("completion") == "off_plan" and not res["handover"] and res["verdict"] == "target":
        res["verdict"] = "review"  # срок сдачи неизвестен — может оказаться позже согласованного
    return res


def verdict(n, r, a):
    """target   — дисконт ≥ цели при достаточном числе сделок
       suspect  — дисконт подозрительно большой (вероятна ошибка/неполная цена)
       review   — кандидат на проверку (меньший дисконт с признаками срочности, или мало данных, но есть сигналы)
       skip     — не уведомлять"""
    d, c = r["discount"], r["comps"]
    enough = c and c["n"] >= a["min_comps"]
    if d is not None and d >= a["suspicious_discount"]:
        return "suspect"
    spread = (c["p75"] - c["p25"]) / c["median"] if c else None
    # сделки района без проекта — не тот же дом, поэтому максимум «на проверку»
    if enough and d >= a["target_discount"] and spread <= a.get("max_spread", 0.35) and r.get("basis") != "area":
        return "target"
    signals = bool(r["keywords"]) or n.get("pf_flags", {}).get("is_price_drop") or n.get("pf_flags", {}).get("is_great_value")
    if d is not None and d >= a["target_discount"] and c["n"] >= 2:
        return "review"  # дисконт есть, но сделок мало или они сильно разбросаны
    if d is not None and d >= a["review_discount"] and signals:
        return "review"
    peers = r.get("listing_peers")
    if d is None and signals and peers and peers["n"] >= 3 and r.get("ppsqm") \
            and r["ppsqm"] <= peers["median"] * (1 - a["target_discount"]):
        return "review"  # сделок нет, но заметно дешевле соседних объявлений
    return "skip"
