"""Second round of feedback: roles, set-outcome assists/cards, ratings, Free mode, bracket picker, world view."""
import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import api, db, engine, stats, timeline

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
ED = "WC2022"


@unittest.skipUnless(REAL_DB.exists(), "run the importer first")
class Feedback2(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)
        self.b = api.create_branch(self.conn, "GK Timeline")["branch_id"]
        self.brandt = next(p for p in api.squad(self.conn, ED, "GER") if "Brandt" in p["name"])["player_id"]
        self.me = api.create_player(self.conn, ED, self.b, {
            "full_name": "Alex Drakos", "birth_date": "1998-03-05", "team_id": "GER", "position": "FW",
            "replaces_player_id": self.brandt, "tier": "generational", "overall": 98, "roles": ["ST", "LW"]})["player_id"]

    def tearDown(self):
        self.conn.close()

    def fx(self, a, b, raw=False):
        fx = timeline.active_fixtures(self.conn, ED, self.b, raw=raw)
        return next(f for f in fx.values() if f.teams() == {a, b} and f.stage == "group")

    # ---- roles ---------------------------------------------------------------
    def test_roles_saved_and_validated(self):
        self.assertEqual(api.player_view(self.conn, ED, self.b)["player"]["roles"], ["LW", "ST"])
        api.update_player(self.conn, self.b, {"roles": ["RW", "CAM"]})
        self.assertEqual(api.player_view(self.conn, ED, self.b)["player"]["roles"], ["CAM", "RW"])
        with self.assertRaises(api.EditError):
            api.update_player(self.conn, self.b, {"roles": ["GK"]})        # outfield player can't be GK

    def test_squad_shows_roles_and_minutes(self):
        sq = {p["name"]: p for p in api.squad(self.conn, ED, "ARG")}
        self.assertIn("ST", sq["Lionel Messi"]["roles"])
        self.assertGreater(sq["Lionel Messi"]["minutes"], 600)
        self.assertTrue(any(p["minutes"] == 0 for p in sq.values()))      # someone never played

    # ---- set outcome fills assists and cards ----------------------------------
    def test_set_outcome_fills_assists_and_my_assists(self):
        f = self.fx("GER", "JPN", raw=True)
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "start"})
        gh = f.home == "GER"
        r = api.roll_constrained(self.conn, ED, self.b, f.fixture_id, {
            "home_score": 6 if gh else 2, "away_score": 2 if gh else 6, "my_goals": 4, "my_assists": 1, "keep_real": True}, seed=3)
        mine_g = [e for e in r["events"] if e["player_id"] == self.me and e["event_type"] in ("goal", "pen_goal")]
        mine_a = [e for e in r["events"] if e.get("assist_player_id") == self.me]
        self.assertEqual((len(mine_g), len(mine_a)), (4, 1))
        new_goals = [e for e in r["events"] if e["event_type"] == "goal" and e["team_id"] == "GER"]
        self.assertGreaterEqual(sum(1 for e in new_goals if e.get("assist_player_id")), 2)

    def test_set_outcome_rolls_cards_when_match_has_none(self):
        f = self.fx("GER", "CRC", raw=True)
        n = 0
        for seed in range(12):
            r = api.roll_constrained(self.conn, ED, self.b, f.fixture_id, {"home_score": 3, "away_score": 1}, seed=seed)
            n += sum(1 for e in r["events"] if e["event_type"] == "yellow")
        self.assertGreater(n, 10)

    # ---- Free mode -------------------------------------------------------------
    def test_free_mode_unlocks_other_matches(self):
        f = self.fx("FRA", "AUS")
        payload = {"home_score": 0, "away_score": 0, "events": []}
        with self.assertRaisesRegex(api.EditError, "Focus mode"):
            api.edit_fixture(self.conn, ED, self.b, f.fixture_id, payload)
        api.set_mode(self.conn, self.b, "free")
        self.assertTrue(api.edit_fixture(self.conn, ED, self.b, f.fixture_id, payload)["ok"])
        self.assertEqual(api.edition_view(self.conn, ED, self.b)["mode"], "free")

    # ---- bracket picker ---------------------------------------------------------
    def test_pick_winner(self):
        api.pick_winner(self.conn, ED, self.b, 49, "USA", seed=2)
        v = api.edition_view(self.conn, ED, self.b)
        m49 = next(s for s in v["slots"] if s["match_no"] == 49)
        self.assertEqual(m49["fixture"]["winner"], "USA")
        self.assertEqual(m49["fixture"]["origin"], "picked")
        self.assertEqual({next(s for s in v["slots"] if s["match_no"] == 58)[k] for k in ("home", "away")}, {"USA", "ARG"})

    def test_pick_podium_keeps_real_results_that_fit(self):
        api.pick_podium(self.conn, ED, self.b, "MAR", "BRA", "FRA", seed=1)
        v = api.edition_view(self.conn, ED, self.b)
        self.assertEqual((v["champion"], v["runner_up"], v["third"]), ("MAR", "BRA", "FRA"))
        kept = [s for s in v["slots"] if s["fixture"] and s["fixture"]["origin"] == "baseline"]
        self.assertGreaterEqual(len(kept), 9)                                # all R16 + compatible QFs stay real
        self.assertEqual(engine.resolve(self.conn, ED, self.b).violations, [])

    def test_podium_errors(self):
        with self.assertRaisesRegex(api.EditError, "same half"):
            api.pick_podium(self.conn, ED, self.b, "MAR", "ENG")
        with self.assertRaisesRegex(api.EditError, "round of 16"):
            api.pick_podium(self.conn, ED, self.b, "GER", "ARG")

    # ---- world view ---------------------------------------------------------
    def test_world_diff(self):
        api.pick_podium(self.conn, ED, self.b, "MAR", "BRA", "FRA", seed=1)
        w = api.world_diff(self.conn, self.b)
        self.assertEqual(w["summary"]["changed"], 1)
        sec = next(x for x in w["sections"] if x["edition_id"] == "WC2022")
        self.assertEqual(sec["name"], "FIFA World Cup 2022")
        self.assertTrue(any(r["kind"] == "podium" for r in sec["rocks"]))
        self.assertIn("matches", sec["detail"])


if __name__ == "__main__":
    unittest.main()
