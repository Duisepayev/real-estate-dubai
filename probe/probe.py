"""Проверка №2: поля листинга, локации PF, фильтры DLD."""
import json, re, urllib.request, urllib.parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"

def get(url, data=None, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, repr(e)

def pf(url):
    s, body = get(url)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
    return s, (json.loads(m.group(1)) if m else None), body

s, d, _ = pf("https://www.propertyfinder.ae/en/search?c=1&fu=0&ob=nd&pt=4000000&bdr[]=3")
sr = d["props"]["pageProps"]["searchResult"]
p = sr["listings"][0]["property"]
print("PROPERTY KEYS", sorted(p.keys()))
for k in ["offering_type", "price", "description", "price_per_area", "deal_badges", "badges", "is_price_reduced", "price_drop", "previous_price", "listed_date", "location_tree", "project", "unit_area"]:
    if k in p: print(k, "=", json.dumps(p[k], ensure_ascii=False)[:300])
print("pageProps keys", list(d["props"]["pageProps"].keys()))
fc = d["props"]["pageProps"]["filterCapabilities"]["catalog"]
for k in ["deal_badges", "listed_within", "price_type", "category_id", "completion_status"]:
    print(k, json.dumps(fc.get(f"filter[{k}]"), ensure_ascii=False)[:600])
print("filter keys", list(fc.keys()))
print("searchQuery", json.dumps(d["props"]["pageProps"].get("searchQuery"), ensure_ascii=False)[:800])
print("filtersValueFromQuery", json.dumps(d["props"]["pageProps"].get("filtersValueFromQuery"), ensure_ascii=False)[:800])
print("payment_method", p.get("payment_method"), "is_price_drop", p.get("is_price_drop"), "great", p.get("is_great_value"), "rooms", p.get("rooms"))

for q in ["Emaar Beachfront", "Palm Jumeirah", "Balqis", "Jumeirah Beach Residence", "Dubai Hills Estate"]:
    for u in [f"https://www.propertyfinder.ae/api/pwa/locations?locale=en&filters.name={urllib.parse.quote(q)}&pagination.limit=5",
              f"https://www.propertyfinder.ae/api/pwa/locations/search?locale=en&query={urllib.parse.quote(q)}"]:
        s, body = get(u, headers={"Accept": "application/json"})
        print("LOC", q, s, body[:500].replace("\n", " "))
        if s == 200: break

# Проверяем, работает ли текстовая ссылка с локацией через слаг
s, d, body = pf("https://www.propertyfinder.ae/en/buy/dubai/apartments-for-sale-emaar-beachfront.html")
print("slug page", s, bool(d))
if d:
    sr = d["props"]["pageProps"].get("searchResult") or {}
    print("meta", json.dumps(sr.get("meta", {}), ensure_ascii=False)[:300])
    if sr.get("listings"): print(json.dumps(sr["listings"][0]["property"]["location"], ensure_ascii=False))
    print("searchQuery", json.dumps(d["props"]["pageProps"].get("searchQuery"), ensure_ascii=False)[:800])

print("=== DLD ===")
def dld(extra):
    payload = {"P_FROM_DATE": "07/01/2026", "P_TO_DATE": "10/15/2026", "P_GROUP_ID": "1", "P_IS_OFFPLAN": "", "P_IS_FREE_HOLD": "", "P_AREA_ID": "", "P_USAGE_ID": "1", "P_PROP_TYPE_ID": "", "P_TAKE": "5", "P_SKIP": "0", "P_SORT": "TRANSACTION_NUMBER_ASC"}
    payload.update(extra)
    s, body = get("https://gateway.dubailand.gov.ae/open-data/transactions", json.dumps(payload).encode(), {"Content-Type": "application/json", "Accept": "application/json", "Origin": "https://dubailand.gov.ae", "Referer": "https://dubailand.gov.ae/"})
    try:
        r = json.loads(body)["response"]["result"]
    except Exception:
        print("DLD", extra, s, body[:300]); return []
    print("DLD", extra, s, "n", len(r), "total", r[0]["TOTAL"] if r else 0)
    return r

r = dld({"P_TAKE": "1000"})
from collections import Counter
print(Counter(x["AREA_EN"] for x in r).most_common(60))
print(Counter((x["AREA_EN"], x["PROJECT_EN"]) for x in r if x["AREA_EN"] and any(t in x["AREA_EN"] for t in ["MARSA", "PALM", "HADAEQ", "MINA"])).most_common(60))
print(Counter(x["PROCEDURE_EN"] for x in r))
print(Counter(x["IS_OFFPLAN_EN"] for x in r))
dld({"P_IS_OFFPLAN": "0"})
for path in ["lookups/areas", "areas", "lookup/areas", "transactions/lookups"]:
    s, body = get("https://gateway.dubailand.gov.ae/open-data/" + path, headers={"Accept": "application/json", "Origin": "https://dubailand.gov.ae", "Referer": "https://dubailand.gov.ae/"})
    print("LOOKUP", path, s, body[:300])
