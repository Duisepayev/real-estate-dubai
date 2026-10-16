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


def costs(price, cfg, completion="ready"):
    """Предварительная сумма собственных средств и платёж. Ставки — примеры."""
    f = cfg["finance"]
    dp_pct = f["down_payment_pct"]
    # Минимальный взнос по правилам ЦБ ОАЭ для экспата: 20% до 5 млн, 30% выше; off-plan — 50%
    reg_min = 0.5 if completion == "off_plan" else (0.2 if price <= 5_000_000 else 0.3)
    dp_pct = max(dp_pct, reg_min)
    loan = price * (1 - dp_pct)
    dld_fee = price * 0.04 + f["dld_admin_aed"]
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
            "agency": agency, "bank": bank, "own_funds": price * dp_pct + fees, "payments": pays,
            "cash_total": price + dld_fee + agency + f["trustee_fee_aed"]}


def evaluate(n, dld_db, projects, store, cfg):
    a = cfg["alerts"]
    text = f"{n.get('title', '')}\n{n.get('description', '')}"
    res = {"keywords": find_keywords(text), "risks": find_risks(text), "project": None, "match_score": 0,
           "comps": None, "comp_list": [], "listing_peers": None, "discount": None, "fair": None}
    if n.get("price_aed") and n.get("size_sqm"):
        res["ppsqm"] = n["price_aed"] / n["size_sqm"]
    names = n.get("match_names") or [x for x in [n.get("tower"), n.get("subcommunity")] if x]
    if names and dld_db is not None:
        proj, score = projects.match(names)
        res["project"], res["match_score"] = proj, score
        if proj and n.get("size_sqm"):
            cs = dld.comps(dld_db, proj, n, months=a["comps_months"], size_tol=a["size_tolerance"])
            res["comp_list"] = sorted(cs, key=lambda c: c["date"], reverse=True)
            res["comps"] = dld.summarize(cs)
    peers = store.active_peers(n) if store else []
    if peers:
        res["listing_peers"] = {"n": len(peers), "median": statistics.median(peers), "min": min(peers)}
    c = res["comps"]
    if c and res.get("ppsqm"):
        size = n["size_sqm"]
        res["fair"] = (c["p25"] * size, c["median"] * size, c["p75"] * size)
        res["discount"] = 1 - res["ppsqm"] / c["median"]
    res["verdict"] = verdict(n, res, a)
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
    if enough and d >= a["target_discount"] and spread <= a.get("max_spread", 0.35):
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
