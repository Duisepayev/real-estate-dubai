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

    def test_match_requires_all_project_words(self):
        db = dld_db([("x", "2026-09-01", "Sell", 0, "A", "Baystar by VIDA", None, "Unit", "Flat", "2 B/R", 100, 1),
                     ("y", "2026-09-01", "Sell", 0, "A", "ACACIA AT PARK HEIGHTS", None, "Unit", "Flat", "2 B/R", 100, 1),
                     ("z", "2026-09-01", "Sell", 0, "A", "MAPLE", None, "Unit", "Villa", "3 B/R", 100, 1),
                     ("w", "2026-09-01", "Sell", 0, "A", "MAPLE 2", None, "Unit", "Villa", "3 B/R", 100, 1),
                     ("v", "2026-09-01", "Sell", 0, "A", "1 Residences", None, "Unit", "Flat", "3 B/R", 100, 1)])
        pr = dld.Projects(db)
        self.assertIsNone(pr.match(["Vida Residences"])[0])
        self.assertIsNone(pr.match(["Park Heights 2 Tower 2", "Park Heights 2", "Park Heights"])[0])
        self.assertEqual(pr.match(["Acacia A", "Acacia", "Park Heights"])[0], "ACACIA AT PARK HEIGHTS")
        self.assertEqual(pr.match(["Maple 2"])[0], "MAPLE 2")
        self.assertIsNone(pr.match(["Murjan 1"])[0])

    def test_building_name_beats_subcommunity(self):
        db = dld_db([("a", "2026-09-01", "Sell", 0, "P", "THE CRESCENT", None, "Unit", "Flat", "2 B/R", 100, 1),
                     ("b", "2026-09-01", "Sell", 0, "P", "ROYAL AMWAJ", None, "Unit", "Flat", "2 B/R", 100, 1)])
        self.assertEqual(dld.Projects(db).match(["The Crescent", "Royal Amwaj Residences South"])[0], "ROYAL AMWAJ")

    def test_flats_preferred_over_hotel_apartments(self):
        import datetime as dt
        d = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        rows = [(f"f{i}", d, "Sell", 0, "M", "Beach Mansion", None, "Unit", "Flat", "3 B/R", 153, 6_000_000) for i in range(3)]
        rows += [(f"h{i}", d, "Sell", 0, "M", "Beach Mansion", None, "Unit", "Hotel Apartment", "3 B/R", 153, 12_000_000) for i in range(3)]
        cs = dld.comps(dld_db(rows), "Beach Mansion", self.n)
        self.assertEqual({c["value_aed"] for c in cs}, {6_000_000})

    def test_no_comps_no_signal_skip(self):
        db = dld_db([])
        n = dict(self.n, title="3BR", description="", pf_flags={})
        r = analyze.evaluate(n, db, dld.Projects(db), None, self.cfg)
        self.assertEqual(r["verdict"], "skip")

    def test_unmatched_old_tower_uses_area_deals(self):
        # Sadaf 8 (JBR): в DLD без проекта; объявления рядом завышены относительно сделок
        import datetime as dt
        d = (dt.date.today() - dt.timedelta(days=40)).isoformat()
        vals = [2_231_926, 2_500_000, 2_500_000, 2_575_000, 2_585_000, 2_800_000]
        db = dld_db([(f"s{i}", d, "Sell", 0, "JUMEIRAH BEACH RESIDENCE", None, None, "Unit", "Flat", "3 B/R", 177, v)
                     for i, v in enumerate(vals)])
        n = self.n | {"search": "JBR", "tower": "Sadaf 8", "subcommunity": "Sadaf", "match_names": ["Sadaf", "Sadaf 8"],
                      "price_aed": 2_600_000, "size_sqm": 177.3, "description": "", "title": "Higher 3 BR"}

        class Peers:
            def active_peers(self, _n):
                return [19_231] * 5
        r = analyze.evaluate(n, db, dld.Projects(db), Peers(), self.cfg)
        self.assertIsNone(r["project"])
        self.assertEqual(r["basis"], "area")
        self.assertEqual(r["comps"]["n"], 6)
        self.assertLess(r["discount"], 0.05)
        self.assertEqual(r["verdict"], "skip")
        # та же цена на 25% ниже сделок района — только «на проверку», не «цель»
        r = analyze.evaluate(n | {"price_aed": 1_900_000}, db, dld.Projects(db), Peers(), self.cfg)
        self.assertEqual(r["verdict"], "review")
        text = notify.format_listing(n | {"first_seen": "2026-10-19T07:00:00"}, r, analyze.costs(1_900_000, self.cfg), [])
        self.assertIn("района без названия проекта", text)

    def test_abu_dhabi_skips_dld_and_uses_adrec_fee(self):
        db = dld_db([("x", "2026-09-01", "Sell", 0, "A", "Beach Mansion", None, "Unit", "Flat", "3 B/R", 150, 9_000_000)])
        n = self.n | {"search": "Saadiyat Island"}
        r = analyze.evaluate(n, db, dld.Projects(db), None, self.cfg)
        self.assertIsNone(r["project"])
        self.assertIsNone(r["comps"])
        c = analyze.costs(4_000_000, self.cfg, "ready", "abu_dhabi")
        self.assertEqual(c["reg_pct"], 0.02)
        text = notify.format_listing(n | {"first_seen": "2026-10-19T07:00:00"}, r | {"verdict": "review"}, c, [])
        self.assertIn("ADREC 2%", text)
        self.assertIn("Абу-Даби", text)

    def test_costs(self):
        c = analyze.costs(4_000_000, self.cfg)
        self.assertEqual(c["down"], 1_400_000)
        self.assertGreater(c["own_funds"], 1_400_000 + 160_000)
        off = analyze.costs(4_000_000, self.cfg, "off_plan")
        self.assertEqual(off["dp_pct"], 0.5)

    def test_handover(self):
        self.assertEqual(analyze.find_handover("Hot deal | Handover 2028"), "2028-12-31")
        self.assertEqual(analyze.find_handover("handover Q2 2027, 60/40 plan"), "2027-06-30")
        self.assertEqual(analyze.find_handover("Handover: June 2027"), "2027-06-30")
        self.assertIsNone(analyze.find_handover("Spacious 3BR, park view"))

    def test_late_handover_skipped(self):
        import datetime as dt
        d = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        rows = [(f"t{i}", d, "Sell", 1, "M", "Beach Mansion", None, "Unit", "Flat", "3 B/R", 153, 6_000_000) for i in range(5)]
        db = dld_db(rows)
        n = dict(self.n, completion="off_plan", title="Below OP | Handover Q4 2028")
        self.assertEqual(analyze.evaluate(n, db, dld.Projects(db), None, self.cfg)["verdict"], "skip")
        n = dict(n, title="Below OP | Handover Q1 2027")
        self.assertEqual(analyze.evaluate(n, db, dld.Projects(db), None, self.cfg)["verdict"], "target")

    def test_risks(self):
        self.assertTrue(analyze.find_risks("Tenanted until June, 40% paid, post-handover payment plan"))

    def test_threads_extract(self):
        i = threads.extract("Urgent sale Palm Jumeirah 3BR AED 3.2M, below market")
        self.assertEqual(i["prices"], [3_200_000])
        self.assertIn("palm jumeirah", i["places"])

    def test_telegram_chat_discovery(self):
        import json, os
        from unittest import mock
        upd = {"result": [{"message": {"chat": {"id": 111, "type": "private"}, "text": "hi"}},
                          {"message": {"chat": {"id": 222, "type": "private"}, "text": "/start monitor"}}]}
        s = Store(":memory:")
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "x"}, clear=False), \
                mock.patch.object(notify, "request", return_value=json.dumps(upd)):
            os.environ.pop("TELEGRAM_CHAT_ID", None)
            self.assertEqual(notify.resolve_telegram_chat(s, log=lambda *a: None), "222")
        self.assertEqual(s.get_kv("telegram_chat_id"), "222")

    def test_threads_apify_mapping(self):
        import json, os
        from unittest import mock
        from monitor import http
        items = [{"postId": "9", "url": "https://www.threads.net/@a/post/x", "text": "Urgent sale Palm Jumeirah 3BR AED 4.5M",
                  "username": "a", "takenAt": 2_000_000_000}, {"postId": "8", "text": "old", "takenAt": 1}]
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t"}), mock.patch.object(http, "request", return_value=json.dumps(items)):
            os.environ.pop("THREADS_ACCESS_TOKEN", None)
            posts = threads.search(["urgent sale dubai"], 1_000_000_000, cfg={"apify_actor": "a~b"}, log=lambda *a: None)
        self.assertEqual([p["id"] for p in posts], ["th:9"])

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
