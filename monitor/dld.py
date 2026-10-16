"""Зарегистрированные сделки Dubai Land Department (открытые данные).

Источник: публичный шлюз открытых данных DLD (тот же, что питает раздел
Open Data на dubailand.gov.ae). Ключ не нужен. В данных есть проект,
район, тип, число комнат, площадь и сумма сделки; корпуса, этажа и вида нет.
"""
import datetime as dt
import re
import sqlite3
import statistics
import time

from .http import request

URL = "https://gateway.dubailand.gov.ae/open-data/transactions"
HEADERS = {"Accept": "application/json", "Origin": "https://dubailand.gov.ae",
           "Referer": "https://dubailand.gov.ae/"}
PAGE = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS tx (
  number TEXT PRIMARY KEY, date TEXT, procedure TEXT, offplan INTEGER,
  area TEXT, project TEXT, master_project TEXT, prop_type TEXT, sub_type TEXT,
  rooms TEXT, size_sqm REAL, value_aed REAL
);
CREATE INDEX IF NOT EXISTS tx_project ON tx(project);
CREATE INDEX IF NOT EXISTS tx_area ON tx(area);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def _fetch(date_from, date_to, skip):
    payload = {"P_FROM_DATE": date_from.strftime("%m/%d/%Y"), "P_TO_DATE": date_to.strftime("%m/%d/%Y"),
               "P_GROUP_ID": "1", "P_IS_OFFPLAN": "", "P_IS_FREE_HOLD": "", "P_AREA_ID": "",
               "P_USAGE_ID": "1", "P_PROP_TYPE_ID": "", "P_TAKE": str(PAGE), "P_SKIP": str(skip),
               "P_SORT": "TRANSACTION_NUMBER_ASC"}
    import json
    body = json.loads(request(URL, data=payload, headers=HEADERS, timeout=90))
    return body["response"]["result"] or []


def sync(db, months=12, overlap_days=21, log=print):
    """Догружает сделки: первый раз — за `months`, далее — последние `overlap_days`
    (DLD публикует с задержкой, поэтому окно перекрывается)."""
    today = dt.date.today()
    last = db.execute("SELECT v FROM meta WHERE k='synced_to'").fetchone()
    start = today - dt.timedelta(days=int(months * 30.5))
    if last:
        start = max(start, dt.date.fromisoformat(last[0]) - dt.timedelta(days=overlap_days))
    total = 0
    cur = start
    while cur <= today:
        end = min(cur + dt.timedelta(days=13), today)
        skip = 0
        while True:
            rows = _fetch(cur, end, skip)
            db.executemany("INSERT OR REPLACE INTO tx VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                (r["TRANSACTION_NUMBER"], (r.get("INSTANCE_DATE") or "")[:10], r.get("PROCEDURE_EN"),
                 int(r.get("IS_OFFPLAN") or 0), r.get("AREA_EN"), r.get("PROJECT_EN"),
                 r.get("MASTER_PROJECT_EN"), r.get("PROP_TYPE_EN"), r.get("PROP_SB_TYPE_EN"),
                 r.get("ROOMS_EN"), r.get("ACTUAL_AREA") or r.get("PROCEDURE_AREA"), r.get("TRANS_VALUE"))
                for r in rows])
            total += len(rows)
            if len(rows) < PAGE:
                break
            skip += PAGE
            time.sleep(0.5)
        cur = end + dt.timedelta(days=1)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('synced_to', ?)", (today.isoformat(),))
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    log(f"[dld] загружено {total} записей, в базе {n}")
    return total


# ---------- сопоставление объявлений со сделками ----------

_STOP = {"the", "at", "by", "residence", "residences", "tower", "towers", "building", "dubai",
         "phase", "and", "of", "in", "apartments", "villas", "estate"}


def tokens(s):
    s = (s or "").lower().replace("&", " ")
    return {t for t in re.findall(r"[a-z]+|\d+", s) if t not in _STOP}


def rooms_label(bedrooms):
    if bedrooms is None:
        return None
    return "Studio" if bedrooms == 0 else f"{bedrooms} B/R"


def _sub_types(ptype):
    if ptype in ("villa", "townhouse"):
        return ("Villa",)
    return ("Flat", "Hotel Apartment")


class Projects:
    """Справочник имён проектов DLD для нечёткого поиска."""

    def __init__(self, db):
        self.rows = [(p, a, tokens(p)) for p, a in
                     db.execute("SELECT project, area FROM tx WHERE project IS NOT NULL GROUP BY project, area")]

    def match(self, names, areas=None):
        """Ищет проект DLD по названиям башни/сабкомьюнити из объявления.
        Возвращает (project, score) или (None, 0)."""
        best = (None, 0.0)
        for name in names:
            nt = tokens(name)
            if not nt:
                continue
            for proj, area, pt in self.rows:
                if not pt or (areas and area not in areas and not (nt & pt)):
                    continue
                inter = len(nt & pt)
                if not inter:
                    continue
                score = inter / len(nt | pt)
                # цифры (номер корпуса) должны совпадать, если указаны с обеих сторон
                nd, pd = {t for t in nt if t.isdigit()}, {t for t in pt if t.isdigit()}
                if nd and pd and not (nd & pd):
                    score *= 0.4
                if score > best[1]:
                    best = (proj, score)
        return best if best[1] >= 0.5 else (None, 0.0)


def comps(db, project, listing, months=12, size_tol=0.25):
    """Сопоставимые сделки: тот же проект, число комнат, тип, площадь ±size_tol."""
    since = (dt.date.today() - dt.timedelta(days=int(months * 30.5))).isoformat()
    size = listing.get("size_sqm")
    q = ("SELECT number, date, procedure, offplan, size_sqm, value_aed, rooms FROM tx "
         "WHERE project=? AND date>=? AND size_sqm>0 AND value_aed>0 AND sub_type IN (%s)"
         % ",".join("?" * len(_sub_types(listing["property_type"]))))
    args = [project, since, *_sub_types(listing["property_type"])]
    rl = rooms_label(listing.get("bedrooms"))
    if rl:
        q += " AND rooms=?"
        args.append(rl)
    if size:
        q += " AND size_sqm BETWEEN ? AND ?"
        args += [size * (1 - size_tol), size * (1 + size_tol)]
    rows = db.execute(q, args).fetchall()
    # готовый объект сравниваем с готовыми, off-plan — со всеми
    if listing.get("completion") == "ready":
        ready = [r for r in rows if not r[3]]
        if len(ready) >= 3 or len(ready) == len(rows):
            rows = ready
    return [{"number": r[0], "date": r[1], "procedure": r[2], "offplan": bool(r[3]),
             "size_sqm": r[4], "value_aed": r[5], "ppsqm": r[5] / r[4]} for r in rows]


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(cs):
    pp = [c["ppsqm"] for c in cs]
    if not pp:
        return None
    return {"n": len(pp), "median": statistics.median(pp), "p25": quantile(pp, 0.25),
            "p75": quantile(pp, 0.75), "first": min(c["date"] for c in cs), "last": max(c["date"] for c in cs)}
