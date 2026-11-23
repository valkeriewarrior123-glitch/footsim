import random
import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import api, db, ratings, roller, timeline
from footsim.timeline import Fixture

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
ED = "WC2022"


@unittest.skipUnless(REAL_DB.exists(), "run the importer and ratings loader first")
class Roller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, cls.tmp)
        cls.conn = db.init_db(cls.tmp)
        cls.info = roller._player_info(cls.conn, ED)
        cls.fx = timeline.active_fixtures(cls.conn, ED)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def find(self, a, b, stage=None):
        return next(f for f in self.fx.values() if f.teams() == {a, b} and (stage is None or f.stage == stage))

    def test_ratings_loaded(self):
        self.assertGreater(ratings.rating(self.conn, "BRA", "2022-11-20"),
                           ratings.rating(self.conn, "GHA", "2022-11-20") + 300)

    def test_same_seed_same_result(self):
        f = self.find("ARG", "KSA")
        a = roller.roll(self.conn, ED, None, f, seed=7, info=self.info)
        b = roller.roll(self.conn, ED, None, f, seed=7, info=self.info)
        self.assertEqual(a, b)

    def test_every_roll_is_a_valid_result(self):
        rng = random.Random(1)
        fixtures = list(self.fx.values())
        for i in range(150):
            f = rng.choice(fixtures)
            r = roller.roll(self.conn, ED, None, f, seed=i, info=self.info)
            api._validate(f, r)                                    # score matches events etc.
            for team in (f.home, f.away):
                self.assertEqual(sum(1 for l in r["lineup"] if l["team_id"] == team and l["started"]), 11)
            if f.stage != "group":
                self.assertIsNotNone(Fixture("x", f.stage, None, None, f.date, f.home, f.away, r["home_score"],
                                             r["away_score"], 0, r["pen_home"], r["pen_away"]).winner())
            off = {e["player_id"]: e["minute"] for e in r["events"] if e["event_type"] in ("second_yellow", "red")}
            for e in r["events"]:
                if e["event_type"] in ("goal", "pen_goal") and e["player_id"] in off:
                    self.assertLessEqual(e["minute"], off[e["player_id"]], "scored after being sent off")

    def test_suspended_players_left_out(self):
        sf = self.find("ARG", "CRO", "SF")
        banned = roller.suspended_for(self.conn, ED, None, sf)
        names = {api.names(self.conn)[p] for p in banned}
        self.assertTrue({"Marcos Acuña", "Gonzalo Montiel"} <= names)
        r = roller.roll(self.conn, ED, None, sf, seed=3, info=self.info)
        self.assertFalse(banned & {l["player_id"] for l in r["lineup"]})

    def test_scoring_levels_are_realistic(self):
        """Evenly matched teams: about 2.3-3.0 goals a game and 20-32% draws."""
        f = self.find("ENG", "USA")
        goals = draws = 0
        n = 1500
        for i in range(n):
            r = roller.roll(self.conn, ED, None, f, seed=10_000 + i, info=self.info)
            goals += r["home_score"] + r["away_score"]
            draws += r["home_score"] == r["away_score"]
        self.assertTrue(2.0 <= goals / n <= 3.1, goals / n)
        self.assertTrue(0.18 <= draws / n <= 0.33, draws / n)

    def test_strength_follows_elo(self):
        """Strong v weak: the favourite's points share is close to the Elo expectation."""
        f = self.find("BRA", "CMR")
        eh = ratings.rating(self.conn, f.home, "2022-11-20")
        ea = ratings.rating(self.conn, f.away, "2022-11-20")
        expect = ratings.expected(eh - ea)
        share, n = 0.0, 1500
        for i in range(n):
            r = roller.roll(self.conn, ED, None, f, seed=20_000 + i, info=self.info)
            share += 1.0 if r["home_score"] > r["away_score"] else 0.5 if r["home_score"] == r["away_score"] else 0.0
        self.assertAlmostEqual(share / n, expect, delta=0.06)

    def test_bonus_makes_a_team_stronger(self):
        f = self.find("WAL", "IRN")
        wal_home = f.home == "WAL"
        def wins(bonus):
            w = 0
            for i in range(600):
                r = roller.roll(self.conn, ED, None, f, seed=30_000 + i, bonus=bonus, info=self.info)
                hs, as_ = r["home_score"], r["away_score"]
                w += (hs > as_) if wal_home else (as_ > hs)
            return w
        self.assertGreater(wins({"WAL": 150}), wins({}) + 40)


@unittest.skipUnless(REAL_DB.exists(), "run the importer and ratings loader first")
class RollPending(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)

    def tearDown(self):
        self.conn.close()

    def test_roll_pending_finishes_the_tournament(self):
        b = api.create_branch(self.conn, "spain")["branch_id"]
        fx = timeline.active_fixtures(self.conn, ED, b)
        f = next(x for x in fx.values() if x.stage == "group" and x.teams() == {"JPN", "ESP"})
        esp = api.squad(self.conn, ED, "ESP")[-1]["player_id"]
        jpn = api.squad(self.conn, ED, "JPN")[-1]["player_id"]
        api.edit_fixture(self.conn, ED, b, f.fixture_id, {
            "home_score": 1 if f.home == "JPN" else 2, "away_score": 2 if f.home == "JPN" else 1,
            "events": [{"minute": 5, "team_id": "ESP", "player_id": esp, "event_type": "goal"},
                       {"minute": 9, "team_id": "ESP", "player_id": esp, "event_type": "goal"},
                       {"minute": 70, "team_id": "JPN", "player_id": jpn, "event_type": "goal"}]})
        r = api.roll_pending(self.conn, ED, b, seed=5)
        view = api.edition_view(self.conn, ED, b)
        self.assertIsNotNone(view["champion"])
        self.assertTrue(all(s["status"] == "valid" for s in view["slots"]))
        self.assertIn(53, r["rolled"])
        # untouched side keeps real results
        self.assertEqual(next(s for s in view["slots"] if s["match_no"] == 49)["fixture"]["origin"], "baseline")

    def test_preview_does_not_save(self):
        b = api.create_branch(self.conn, "preview")["branch_id"]
        fx = timeline.active_fixtures(self.conn, ED, b)
        f = next(x for x in fx.values() if x.stage == "group" and x.teams() == {"QAT", "ECU"})
        p = api.roll_preview(self.conn, ED, b, f.fixture_id, seed=1)
        self.assertIn("events", p)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fixture_overrides WHERE branch_id = ?",
                                           (b,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
