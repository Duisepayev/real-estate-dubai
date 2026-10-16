import sqlite3
import unittest

from monitor import analyze, dld, notify
from monitor.config import load_config
from monitor.sources import propertyfinder as pf
from monitor.sources import threads
from monitor.store import Store

RAW = {"property": {
    "id": "1", "title": "Distress sale | Beach Mansion 3BR", "description": "Motivated seller. Vacant on transfer.",
    "price": {"value": 4_000_000, "is_hidden": False}, "size": {"value": 1646, "unit": "sqft"},
    "bedrooms": "3", "property_type": "Apartment", "completion_status": "completed",
    "location": {"name": "Beach Mansion Tower 1", "type": "TOWER", "full_name": "Beach Mansion Tower 1, Emaar Beachfront, Dubai Harbour, Dubai"},
    "location_tree": [{"name": "Dubai", "type": "CITY"}, {"name": "Dubai Harbour", "type": "COMMUNITY"},
                      {"name": "Emaar Beachfront", "type": "SUBCOMMUNITY"}, {"name": "Beach Mansion Tower 1", "type": "TOWER"}],
    "share_url": "https://www.propertyfinder.ae/en/plp/buy/x-1.html", "listed_date": "2026-10-10T00:00:00Z",
    "is_price_drop": True}}


def dld_db(rows):
    db = sqlite3.connect(":memory:")
    db.executescript(dld.SCHEMA)
    db.executemany("INSERT INTO tx VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return db


class T(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.n = pf.normalize(RAW)

    def test_normalize(self):
        n = self.n
        self.assertEqual(n["bedrooms"], 3)
        self.assertAlmostEqual(n["size_sqm"], 152.9, places=1)
        self.assertEqual(n["tower"], "Beach Mansion Tower 1")
        self.assertEqual(n["completion"], "ready")

    def test_valuation_target(self):
        import datetime as dt
        d = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        rows = [(f"t{i}", d, "Sell", 0, "MARSA DUBAI", "Beach Mansion", None, "Unit", "Flat", "3 B/R", 150 + i, 5_700_000 + i * 10_000)
                for i in range(5)]
        rows.append(("other", d, "Sell", 0, "MARSA DUBAI", "Sunrise Bay", None, "Unit", "Flat", "3 B/R", 150, 9_000_000))
        db = dld_db(rows)
        r = analyze.evaluate(self.n, db, dld.Projects(db), None, self.cfg)
        self.assertEqual(r["project"], "Beach Mansion")
        self.assertEqual(r["comps"]["n"], 5)
        self.assertGreater(r["discount"], 0.25)
        self.assertEqual(r["verdict"], "target")
        self.assertIn("motivated seller", r["keywords"])
        text = notify.format_listing(self.n | {"first_seen": "2026-10-16T12:00:00"}, r,
                                     analyze.costs(self.n["price_aed"], self.cfg), [("a", 4_200_000), ("b", 4_000_000)])
        self.assertIn("Дисконт к медиане", text)
        self.assertIn("4 200 000 → 4 000 000", text)

    def test_tower_number_mismatch(self):
        db = dld_db([("x", "2026-09-01", "Sell", 0, "MARSA DUBAI", "Beach Mansion Tower 2", None, "Unit", "Flat", "3 B/R", 150, 1)])
        proj, score = dld.Projects(db).match(["Beach Mansion Tower 1"])
        self.assertIsNone(proj)

    def test_no_comps_no_signal_skip(self):
        db = dld_db([])
        n = dict(self.n, title="3BR", description="", pf_flags={})
        r = analyze.evaluate(n, db, dld.Projects(db), None, self.cfg)
        self.assertEqual(r["verdict"], "skip")

    def test_costs(self):
        c = analyze.costs(4_000_000, self.cfg)
        self.assertEqual(c["down"], 1_400_000)
        self.assertGreater(c["own_funds"], 1_400_000 + 160_000)
        off = analyze.costs(4_000_000, self.cfg, "off_plan")
        self.assertEqual(off["dp_pct"], 0.5)

    def test_risks(self):
        self.assertTrue(analyze.find_risks("Tenanted until June, 40% paid, post-handover payment plan"))

    def test_threads_extract(self):
        i = threads.extract("Urgent sale Palm Jumeirah 3BR AED 3.2M, below market")
        self.assertEqual(i["prices"], [3_200_000])
        self.assertIn("palm jumeirah", i["places"])

    def test_store_dedup_and_history(self):
        s = Store(":memory:")
        n = dict(self.n, search="x")
        self.assertTrue(s.upsert(dict(n))[0])
        n2 = dict(n, price_aed=3_800_000)
        new, old, hist = s.upsert(n2)
        self.assertFalse(new)
        self.assertEqual(old, 4_000_000)
        self.assertEqual([p for _, p in hist], [4_000_000, 3_800_000])


if __name__ == "__main__":
    unittest.main()
