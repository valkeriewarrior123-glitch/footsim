"""Tests for the first round of user feedback (positions, editing, suspensions, constrained rolls)."""
import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import api, db, engine, player, stats, timeline

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
ED = "WC2022"


@unittest.skipUnless(REAL_DB.exists(), "run the importer first")
class Feedback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)
        self.b = api.create_branch(self.conn, "GK Timeline")["branch_id"]
        self.brandt = next(p for p in api.squad(self.conn, ED, "GER") if "Brandt" in p["name"])["player_id"]
        self.me = api.create_player(self.conn, ED, self.b, {
            "full_name": "Drakos", "birth_date": "1998-03-05", "team_id": "GER", "position": "FW",
            "replaces_player_id": self.brandt, "tier": "generational", "overall": 98})["player_id"]

    def tearDown(self):
        self.conn.close()

    def fx(self, a, b, raw=False):
        fx = timeline.active_fixtures(self.conn, ED, self.b, raw=raw)
        return next(f for f in fx.values() if f.teams() == {a, b} and f.stage == "group")

    # ---- data and lists ---------------------------------------------------
    def test_every_player_has_a_position_and_birth_date(self):
        n = self.conn.execute("SELECT COUNT(*) FROM players WHERE is_created = 0 AND (position IS NULL OR birth_date IS NULL)").fetchone()[0]
        self.assertEqual(n, 0)

    def test_squads_are_alphabetical(self):
        names = [p["name"].casefold() for p in api.squad(self.conn, ED, "GER", self.b)]
        self.assertEqual(names, sorted(names))

    # ---- editing the player ------------------------------------------------
    def test_edit_player_any_time(self):
        api.update_player(self.conn, self.b, {"full_name": "Nikos Drakos", "known_as": "Drakos", "position": "MF",
                                              "overall": 90, "birth_date": "1999-01-01"})
        p = player.profile(self.conn, self.b)
        self.assertEqual((p.position, p.overall, p.birth_date), ("MF", 90, "1999-01-01"))
        with self.assertRaises(api.EditError):
            api.update_player(self.conn, self.b, {"birth_date": "2015-01-01"})

    def test_change_replaced_player(self):
        sane = next(p for p in api.squad(self.conn, ED, "GER") if "Sané" in p["name"])["player_id"]
        api.update_player(self.conn, self.b, {"replaces_player_id": sane})
        ids = {p["player_id"] for p in api.squad(self.conn, ED, "GER", self.b)}
        self.assertIn(self.brandt, ids)          # back in the squad
        self.assertNotIn(sane, ids)
        self.assertEqual(len(ids), 26)

    def test_auto_target_is_same_position(self):
        v = api.player_view(self.conn, ED, self.b)
        m = v["matches"][0]
        self.assertTrue(m["starters"][0]["same"])
        same_names = {x["name"] for x in m["starters"] if x["same"]}
        self.assertIn(m["auto_target"], same_names)      # shares a role (Drakos defaults to ST)
        st = next(x for x in m["starters"] if x["name"] == m["auto_target"])
        self.assertIn("ST", st["roles"])

    # ---- suspensions ---------------------------------------------------------
    def test_cannot_pick_suspended_player(self):
        f = self.fx("GER", "JPN", raw=True)
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "start"})
        f = self.fx("GER", "JPN")
        hs, as_ = f.home_score, f.away_score
        ev = list(f.events) + [{"minute": 20, "team_id": "GER", "player_id": self.me, "event_type": "yellow"},
                               {"minute": 40, "team_id": "GER", "player_id": self.me, "event_type": "second_yellow"}]
        api.edit_fixture(self.conn, ED, self.b, f.fixture_id, {"home_score": hs, "away_score": as_, "events": ev})
        nxt = self.fx("ESP", "GER", raw=True)
        with self.assertRaisesRegex(api.EditError, "suspended"):
            api.set_role(self.conn, ED, self.b, nxt.fixture_id, {"role": "start"})

    # ---- constrained roll ------------------------------------------------------
    def test_set_outcome_reports_real_goals(self):
        f = self.fx("GER", "JPN", raw=True)
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "start"})
        ger_home = f.home == "GER"
        r = api.roll_constrained(self.conn, ED, self.b, f.fixture_id, {
            "home_score": 3 if ger_home else 1, "away_score": 1 if ger_home else 3, "my_goals": 2, "keep_real": True}, seed=4)
        self.assertEqual((r["home_score"], r["away_score"]), (3, 1) if ger_home else (1, 3))
        mine = [e for e in r["events"] if e["player_id"] == self.me and e["event_type"] in ("goal", "pen_goal")]
        self.assertEqual(len(mine), 2)
        kinds = [n["kind"] for n in r["notes"]]
        self.assertIn("kept", kinds)
        self.assertIn("removed", kinds)          # Japan scored 2 for real; only 1 now
        self.assertEqual(r["source"], "real result")
        # it saves cleanly
        api.edit_fixture(self.conn, ED, self.b, f.fixture_id, {k: r[k] for k in (
            "home_score", "away_score", "events", "lineup", "extra_time", "pen_home", "pen_away")})
        self.assertEqual(stats.edition_stats(self.conn, ED, self.b).players[self.me].goals, 2)

    def test_my_goals_can_take_over_real_goals(self):
        f = self.fx("GER", "JPN", raw=True)
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "start"})
        r = api.roll_constrained(self.conn, ED, self.b, f.fixture_id, {
            "home_score": f.home_score, "away_score": f.away_score, "my_goals": 1, "keep_real": True}, seed=1)
        self.assertTrue(any(n["kind"] == "taken" for n in r["notes"]))   # the only German goal becomes his

    def test_impossible_outcomes_are_refused(self):
        f = self.fx("GER", "JPN", raw=True)
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "start"})
        with self.assertRaisesRegex(api.EditError, "Not enough"):
            api.roll_constrained(self.conn, ED, self.b, f.fixture_id, {"home_score": 1, "away_score": 1, "my_goals": 3})
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "unused"})
        with self.assertRaisesRegex(api.EditError, "isn't playing"):
            api.roll_constrained(self.conn, ED, self.b, f.fixture_id, {"home_score": 1, "away_score": 1, "my_goals": 1})

    def test_locked_events_survive_rolls(self):
        f = self.fx("GER", "JPN")
        locked = [e for e in f.events if e["event_type"] in ("goal", "pen_goal", "yellow")][:2]
        for seed in range(15):
            r = api.roll_constrained(self.conn, ED, self.b, f.fixture_id, {"locked_events": locked}, seed=seed)
            for l in locked:
                self.assertTrue(any(e["minute"] == l["minute"] and e["player_id"] == l["player_id"]
                                    and e["event_type"] == l["event_type"] for e in r["events"]), (seed, l))

    def test_locked_score_is_kept(self):
        f = self.fx("GER", "CRC")
        for seed in range(10):
            r = api.roll_constrained(self.conn, ED, self.b, f.fixture_id, {"home_score": 5, "away_score": 0}, seed=seed)
            self.assertEqual((r["home_score"], r["away_score"]), (5, 0))

    # ---- changes tab ----------------------------------------------------------
    def test_history_diff_lists_every_changed_match(self):
        f = self.fx("GER", "JPN", raw=True)
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "start"})
        r = api.roll_constrained(self.conn, ED, self.b, f.fixture_id, {
            "home_score": 2 if f.home == "GER" else 0, "away_score": 0 if f.home == "GER" else 2, "my_goals": 1,
            "keep_real": True}, seed=2)
        api.edit_fixture(self.conn, ED, self.b, f.fixture_id, {k: r[k] for k in (
            "home_score", "away_score", "events", "lineup", "extra_time", "pen_home", "pen_away")})
        api.roll_pending(self.conn, ED, self.b, seed=3)
        d = api.history_diff(self.conn, ED, self.b)
        self.assertGreaterEqual(len(d["matches"]), 1)
        self.assertTrue(any(m["how"] == "edited by you" for m in d["matches"]))
        self.assertEqual(d["player"]["name"], "Drakos")
        self.assertEqual(engine.resolve(self.conn, ED, self.b).violations, [])


if __name__ == "__main__":
    unittest.main()
