import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import api, db, dispatch, outcomes, stats, timeline

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
WC, UCL, SA = "WC2022", "UCL1213", "SERIEA1213"


def count(conn, b):
    return conn.execute("SELECT COUNT(*) FROM fixture_overrides WHERE branch_id = ?", (b,)).fetchone()[0]


@unittest.skipUnless(REAL_DB.exists(), "run the importer and ratings loader first")
class Outcomes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)
        self.b = api.create_branch(self.conn, "o")["branch_id"]

    def tearDown(self):
        self.conn.close()

    def lock(self, ed, kind, **p):
        return outcomes.add_lock(self.conn, ed, self.b, kind, p)

    def test_podium_preview_changes_nothing_apply_keeps(self):
        self.lock(WC, "champion", team="BRA")
        self.lock(WC, "runner_up", team="FRA")
        r = outcomes.run(self.conn, WC, self.b, apply=False, seed=1)
        self.assertTrue(r["ok"], r)
        self.assertEqual(count(self.conn, self.b), 0)
        r = outcomes.run(self.conn, WC, self.b, apply=True, seed=1)
        self.assertTrue(r["ok"] and r["applied"])
        res = stats.edition_stats(self.conn, WC, self.b).resolution
        self.assertEqual((res.champion, res.runner_up), ("BRA", "FRA"))

    def test_two_legged_champion(self):
        self.lock(UCL, "champion", team="c_juventus")
        r = outcomes.run(self.conn, UCL, self.b, apply=True, seed=2)
        self.assertTrue(r["ok"], r)
        self.assertEqual(stats.edition_stats(self.conn, UCL, self.b).resolution.champion, "c_juventus")

    def test_exact_score(self):
        f = next(x for x in timeline.active_fixtures(self.conn, WC, self.b).values() if x.teams() == {"ARG", "KSA"})
        self.lock(WC, "score", fixture_id=f.fixture_id, home=5, away=0)
        self.assertTrue(outcomes.run(self.conn, WC, self.b, apply=True, seed=5)["ok"])
        g = timeline.active_fixtures(self.conn, WC, self.b)[f.fixture_id]
        self.assertEqual((g.home_score, g.away_score), (5, 0))

    def test_player_goals_assists_and_boot_keep_scores(self):
        nm = api.names(self.conn)
        mb = next(k for k, v in nm.items() if v == "Kylian Mbappé")
        self.lock(WC, "player_total", player_id=mb, goals=12, assists=3)
        self.lock(WC, "golden_boot", player_id=mb)
        scores = {f.fixture_id: (f.home_score, f.away_score) for f in timeline.active_fixtures(self.conn, WC, self.b).values()}
        r = outcomes.run(self.conn, WC, self.b, apply=True, seed=3)
        self.assertTrue(r["ok"], r)
        st = stats.edition_stats(self.conn, WC, self.b)
        self.assertEqual((st.players[mb].goals, st.players[mb].assists), (12, 3))
        self.assertEqual(st.awards["golden_boot"][0], mb)
        after = {f.fixture_id: (f.home_score, f.away_score) for f in timeline.active_fixtures(self.conn, WC, self.b).values()}
        self.assertEqual(scores, after)

    def test_impossible_changes_nothing(self):
        nm = api.names(self.conn)
        me = next(k for k, v in nm.items() if v == "Lionel Messi")
        self.lock(WC, "player_total", player_id=me, goals=30)
        r = outcomes.run(self.conn, WC, self.b, apply=True, seed=3)
        self.assertFalse(r["ok"])
        self.assertIn("at most", r["contradictions"][0]["message"])
        self.assertEqual(count(self.conn, self.b), 0)

    def test_locked_score_blocks_champion(self):
        fin = next(x for x in timeline.active_fixtures(self.conn, WC, self.b).values() if x.stage == "F")
        self.lock(WC, "score", fixture_id=fin.fixture_id, home=0, away=1)
        self.lock(WC, "champion", team="ARG")
        r = outcomes.run(self.conn, WC, self.b, apply=True, seed=5)
        self.assertFalse(r["ok"])
        self.assertEqual(count(self.conn, self.b), 0)

    def test_league_positions_and_conflict(self):
        tn = api.team_names(self.conn)
        napoli = next(t for t, n in tn.items() if n.startswith("Napoli"))
        self.lock(SA, "league_position", team=napoli, min=1, max=1)
        r = outcomes.run(self.conn, SA, self.b, apply=True, seed=4)
        self.assertTrue(r["ok"], r)
        self.assertEqual(outcomes.engine.resolve(self.conn, SA, self.b).tables["A"][0].team, napoli)
        b2 = api.create_branch(self.conn, "o2")["branch_id"]
        outcomes.add_lock(self.conn, SA, b2, "league_position", {"team": napoli, "min": 1, "max": 1})
        outcomes.add_lock(self.conn, SA, b2, "league_position", {"team": "c_milan", "min": 1, "max": 1})
        r = outcomes.run(self.conn, SA, b2, apply=True, seed=4)
        self.assertFalse(r["ok"])

    def test_lock_validation_replace_and_routes(self):
        with self.assertRaises(api.EditError):
            self.lock(WC, "champion", team="NOPE")
        with self.assertRaises(api.EditError):
            self.lock(WC, "league_position", team="BRA", min=1, max=1)
        self.lock(WC, "champion", team="BRA")
        self.lock(WC, "champion", team="ARG")
        v = outcomes.view(self.conn, WC, self.b)
        self.assertEqual(len(v["locks"]), 1)
        self.assertIn("Argentina", v["locks"][0]["text"])
        outcomes.delete_lock(self.conn, self.b, v["locks"][0]["lock_id"])
        self.assertEqual(outcomes.view(self.conn, WC, self.b)["locks"], [])
        with self.assertRaises(api.EditError):
            outcomes.run(self.conn, WC, self.b)

    def test_delete_branch_removes_locks(self):
        self.lock(WC, "champion", team="BRA")
        api.delete_branch(self.conn, self.b)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outcome_locks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
