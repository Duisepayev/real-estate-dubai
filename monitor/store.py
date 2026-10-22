"""Состояние мониторинга: объявления, история цен, отправленные уведомления."""
import datetime as dt
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS listing (
  id TEXT PRIMARY KEY, source TEXT, url TEXT, fingerprint TEXT, data TEXT,
  first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1, price_aed REAL
);
CREATE INDEX IF NOT EXISTS listing_fp ON listing(fingerprint);
CREATE TABLE IF NOT EXISTS price_history (id TEXT, ts TEXT, price_aed REAL);
CREATE TABLE IF NOT EXISTS notified (
  key TEXT PRIMARY KEY, listing_id TEXT, ts TEXT, price_aed REAL, verdict TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fingerprint(n):
    """Один и тот же объект часто выставляют несколько агентов: ключ по
    башне/району, спальням, площади (±1 м²) — без цены."""
    place = (n.get("tower") or n.get("subcommunity") or n.get("community") or "").lower()
    size = round((n.get("size_sqm") or 0))
    return f"{place}|{n.get('bedrooms')}|{n.get('property_type')}|{size}"


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def get_kv(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_kv(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, v))

    def ids(self, source=None):
        q = "SELECT id FROM listing" + (" WHERE source=?" if source else "")
        return {r[0] for r in self.db.execute(q, (source,) if source else ())}

    def searches(self):
        """Поиски (зоны), по которым уже сохранены объявления."""
        return {r[0] for r in self.db.execute("SELECT DISTINCT json_extract(data, '$.search') FROM listing") if r[0]}

    def upsert(self, n):
        """Сохраняет объявление. Возвращает (is_new, old_price, history)."""
        ts = now()
        row = self.db.execute("SELECT price_aed, first_seen FROM listing WHERE id=?", (n["id"],)).fetchone()
        fp = fingerprint(n)
        if row is None:
            self.db.execute("INSERT INTO listing VALUES (?,?,?,?,?,?,?,1,?)",
                            (n["id"], n["source"], n["url"], fp, json.dumps(n, ensure_ascii=False), ts, ts, n["price_aed"]))
            self.db.execute("INSERT INTO price_history VALUES (?,?,?)", (n["id"], ts, n["price_aed"]))
            old = None
            first_seen = ts
        else:
            old, first_seen = row
            self.db.execute("UPDATE listing SET data=?, last_seen=?, active=1, price_aed=?, fingerprint=? WHERE id=?",
                            (json.dumps(n, ensure_ascii=False), ts, n["price_aed"], fp, n["id"]))
            if n["price_aed"] and old and abs(n["price_aed"] - old) >= 1:
                self.db.execute("INSERT INTO price_history VALUES (?,?,?)", (n["id"], ts, n["price_aed"]))
        n["first_seen"] = first_seen
        n["fingerprint"] = fp
        return row is None, old, self.history(n["id"])

    def history(self, lid):
        return self.db.execute("SELECT ts, price_aed FROM price_history WHERE id=? ORDER BY ts", (lid,)).fetchall()

    def mark_inactive(self, source, seen_ids, searches):
        """После полного обхода помечает пропавшие объявления этих поисков неактивными."""
        rows = self.db.execute("SELECT id, data FROM listing WHERE source=? AND active=1", (source,)).fetchall()
        gone = [r[0] for r in rows if r[0] not in seen_ids and json.loads(r[1]).get("search") in searches]
        self.db.executemany("UPDATE listing SET active=0 WHERE id=?", [(g,) for g in gone])
        return len(gone)

    def active_peers(self, n):
        """Цены активных объявлений того же здания/района, типа и числа спален (для сравнения по запрашиваемым ценам).
        Сравниваем цену целиком, а не за м²: в объявлениях площадь указывают по-разному (застройка или участок),
        и у больших домов цена за м² ниже — вилла 348 м² выглядела на 35% дешевле соседей, хотя стоила как они."""
        out = []
        for (d,) in self.db.execute("SELECT data FROM listing WHERE active=1 AND id!=?", (n["id"],)):
            o = json.loads(d)
            same_place = (n.get("tower") and o.get("tower") == n.get("tower")) or \
                         (not n.get("tower") and o.get("subcommunity") == n.get("subcommunity"))
            if same_place and o.get("bedrooms") == n.get("bedrooms") and o.get("property_type") == n.get("property_type") \
                    and o.get("size_sqm") and o.get("price_aed") and o.get("fingerprint") != n.get("fingerprint"):
                out.append(o["price_aed"])
        return out

    def last_notification(self, key):
        return self.db.execute("SELECT ts, price_aed, verdict FROM notified WHERE key=?", (key,)).fetchone()

    def record_notification(self, key, lid, price, verdict):
        last = self.last_notification(key)
        if last and last[1] and price:
            price = min(price, last[1])  # храним самую низкую цену, о которой уже писали
        self.db.execute("INSERT OR REPLACE INTO notified VALUES (?,?,?,?,?)", (key, lid, now(), price, verdict))

    def commit(self):
        self.db.commit()
