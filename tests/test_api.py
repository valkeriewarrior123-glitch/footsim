import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import api, db, timeline

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
ED = "WC2022"


@unittest.skipUnless(REAL_DB.exists(), "run the importer first")
class EditorApi(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)
        self.b = api.create_branch(self.conn, "test")["branch_id"]

    def tearDown(self):
        self.conn.close()

    def group_fx(self, a, b):
        fx = timeline.active_fixtures(self.conn, ED, self.b)
        return next(f for f in fx.values() if f.stage == "group" and f.teams() == {a, b})

    def spain_win_payload(self, f):
        espid = api.squad(self.conn, ED, "ESP")[-1]["player_id"]
        jpnid = api.squad(self.conn, ED, "JPN")[-1]["player_id"]
        ev = [{"minute": 10, "team_id": "ESP", "player_id": espid, "event_type": "goal"},
              {"minute": 50, "team_id": "ESP", "player_id": espid, "event_type": "goal"},
              {"minute": 70, "team_id": "JPN", "player_id": jpnid, "event_type": "goal"}]
        hs, as_ = (1, 2) if f.home == "JPN" else (2, 1)
        return {"home_score": hs, "away_score": as_, "events": ev}

    def test_history_is_read_only(self):
        f = self.group_fx("JPN", "ESP")
        with self.assertRaises(api.EditError):
            api.edit_fixture(self.conn, ED, None, f.fixture_id, self.spain_win_payload(f))

    def test_score_must_match_goal_events(self):
        f = self.group_fx("JPN", "ESP")
        p = self.spain_win_payload(f)
        p["events"] = p["events"][:2]
        with self.assertRaisesRegex(api.EditError, "goal event"):
            api.edit_fixture(self.conn, ED, self.b, f.fixture_id, p)

    def test_knockout_draw_needs_shootout(self):
        fx = timeline.active_fixtures(self.conn, ED, self.b)
        final = next(f for f in fx.values() if f.match_no == 64)
        p = {"home_score": 0, "away_score": 0, "events": [], "extra_time": True}
        with self.assertRaisesRegex(api.EditError, "shootout"):
            api.edit_fixture(self.conn, ED, self.b, final.fixture_id, p)

    def test_edit_returns_diff_and_logs_displacement(self):
        f = self.group_fx("JPN", "ESP")
        r = api.edit_fixture(self.conn, ED, self.b, f.fixture_id, self.spain_win_payload(f))
        d = r["diff"]
        self.assertEqual(d["champion"], {"before": "ARG", "after": None})
        self.assertTrue(any(g["group"] == "E" for g in d["groups"]))
        self.assertTrue(any(t["team"] == "Japan" and t["after"] == "Group stage" for t in d["finishes"]))
        disp = api.displacements(self.conn, ED, self.b)
        self.assertIn({"slot": "2E", "was": "Spain", "now": "Germany"}, disp)
        self.assertIn({"slot": "1E", "was": "Japan", "now": "Spain"}, disp)

    def test_fill_pending_knockout_with_new_teams(self):
        f = self.group_fx("JPN", "ESP")
        api.edit_fixture(self.conn, ED, self.b, f.fixture_id, self.spain_win_payload(f))
        view = api.edition_view(self.conn, ED, self.b)
        s53 = next(s for s in view["slots"] if s["match_no"] == 53)
        self.assertEqual(s53["status"], "pending")
        detail = api.fixture_detail(self.conn, ED, s53["fixture_id"], self.b)
        self.assertTrue(detail["lineup"], "new fixtures get a default lineup")
        self.assertTrue(detail["squads"]["ESP"])
        scorer = detail["squads"]["ESP"][-1]["player_id"]
        h_is_esp = detail["home"] == "ESP"
        payload = {"home_score": 1 if h_is_esp else 0, "away_score": 0 if h_is_esp else 1,
                   "events": [{"minute": 30, "team_id": "ESP", "player_id": scorer, "event_type": "goal"}]}
        r = api.edit_fixture(self.conn, ED, self.b, s53["fixture_id"], payload)
        view = api.edition_view(self.conn, ED, self.b)
        self.assertEqual(next(s for s in view["slots"] if s["match_no"] == 53)["status"], "valid")
        self.assertTrue(any(b["match_no"] == 57 for b in r["diff"]["bracket"]))

    def test_revert_restores_history(self):
        f = self.group_fx("JPN", "ESP")
        api.edit_fixture(self.conn, ED, self.b, f.fixture_id, self.spain_win_payload(f))
        r = api.revert_fixture(self.conn, ED, self.b, f.fixture_id)
        self.assertEqual(r["diff"]["champion"], {"before": None, "after": "ARG"})
        self.assertEqual(api.displacements(self.conn, ED, self.b), [])
        view = api.edition_view(self.conn, ED, self.b)
        self.assertTrue(all(s["status"] == "valid" for s in view["slots"]))

    def test_views_render(self):
        v = api.edition_view(self.conn, ED, None)
        self.assertEqual(len(v["groups"]), 8)
        self.assertEqual(v["champion"], "ARG")
        s = api.stats_view(self.conn, ED, None)
        self.assertEqual(s["scorers"][0]["name"], "Kylian Mbappé")

    def test_delete_branch(self):
        f = self.group_fx("JPN", "ESP")
        api.edit_fixture(self.conn, ED, self.b, f.fixture_id, self.spain_win_payload(f))
        api.delete_branch(self.conn, self.b)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fixture_overrides").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
