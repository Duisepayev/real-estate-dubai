"""Одноразовая проверка доступности источников с раннера GitHub Actions."""
import json, re, sys, urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"

def get(url, data=None, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, repr(e)

def walk_keys(o, path="", depth=0, out=None):
    out = out if out is not None else []
    if depth > 6: return out
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{path}.{k}"
            if isinstance(v, list) and v and isinstance(v[0], dict):
                out.append((p, len(v), list(v[0].keys())[:25]))
            walk_keys(v, p, depth + 1, out)
    return out

print("=== Property Finder ===")
url = "https://www.propertyfinder.ae/en/search?c=1&fu=0&l=50&ob=nd&pt=4000000&bdr[]=3"
s, body = get(url)
print("status", s, "len", len(body))
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
print("next_data:", bool(m))
if m:
    d = json.loads(m.group(1))
    for p, n, keys in walk_keys(d)[:15]:
        print(p, n, keys)
    try:
        listings = d["props"]["pageProps"]["searchResult"]["listings"]
        print(json.dumps(listings[0], ensure_ascii=False)[:4000])
        print("meta", json.dumps(d["props"]["pageProps"]["searchResult"].get("meta"), ensure_ascii=False)[:800])
    except Exception as e:
        print("path err", e)
else:
    print(body[:600])

print("=== DLD gateway ===")
payload = {"P_FROM_DATE": "09/01/2026", "P_TO_DATE": "10/15/2026", "P_GROUP_ID": "1", "P_IS_OFFPLAN": "", "P_IS_FREE_HOLD": "", "P_AREA_ID": "", "P_USAGE_ID": "", "P_PROP_TYPE_ID": "", "P_TAKE": "3", "P_SKIP": "0", "P_SORT": "TRANSACTION_NUMBER_ASC"}
s, body = get("https://gateway.dubailand.gov.ae/open-data/transactions", json.dumps(payload).encode(), {"Content-Type": "application/json", "Accept": "application/json", "Origin": "https://dubailand.gov.ae", "Referer": "https://dubailand.gov.ae/"})
print("status", s); print(body[:3000])

print("=== Threads ===")
s, body = get("https://graph.threads.net/v1.0/keyword_search?q=distress%20sale")
print("status", s); print(body[:400])
