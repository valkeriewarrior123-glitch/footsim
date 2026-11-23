import sqlite3
import unittest
from pathlib import Path

from footsim import db

ROOT = Path(__file__).resolve().parent.parent


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_tables_exist(self):
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ["fixtures", "match_events", "rulesets", "team_ratings",
                  "branches", "fixture_overrides", "displacement_log"]:
            self.assertIn(t, names)

    def test_canon_cutoff(self):
        self.assertTrue(db.is_locked("2000-06-10"))
        self.assertFalse(db.is_locked("2001-01-01"))
        self.assertFalse(db.is_locked("2022-11-20"))

    def test_load_wc2022_ruleset(self):
        self.conn.execute("INSERT INTO competitions VALUES ('WC','FIFA World Cup','international','FIFA')")
        self.conn.execute("INSERT INTO editions VALUES ('WC2022','WC','2022','2022-11-20','2022-12-18',0,NULL)")
        eid = db.load_ruleset(self.conn, ROOT / "data_packs" / "WC2022" / "ruleset.json")
        rules = db.get_ruleset(self.conn, eid)
        self.assertEqual(len(rules["knockout"]["bracket"]), 16)
        self.assertEqual(rules["group_stage"]["qualify_per_group"], 2)

    def test_bad_event_type_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO match_events (fixture_id, minute, team_id, event_type) VALUES ('x',1,'X','dive')")


if __name__ == "__main__":
    unittest.main()
