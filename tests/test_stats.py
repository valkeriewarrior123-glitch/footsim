import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import db, stats, timeline

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
MESSI, MBAPPE, GIROUD, MARTINEZ = "sb_5503", "sb_3009", "sb_3604", "sb_6909"


@unittest.skipUnless(REAL_DB.exists(), "run the importer first")
class Stats(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.connect(self.tmp)
        self.conn.executescript(db.SCHEMA.read_text(encoding="utf-8"))   # adds any newer tables

    def tearDown(self):
        self.conn.close()

    # ---- gate test: computed awards match history -------------------
    def test_gate_golden_boot_matches_history(self):
        st = stats.edition_stats(self.conn, "WC2022")
        real = [r[0] for r in self.conn.execute(
            "SELECT player_id FROM awards WHERE edition_id='WC2022' AND award='golden_boot' ORDER BY rank")]
        self.assertEqual(st.awards["golden_boot"], real)

    def test_known_totals(self):
        st = stats.edition_stats(self.conn, "WC2022")
        self.assertEqual(st.players[MBAPPE].goals, 8)
        self.assertEqual(st.players[MESSI].goals, 7)
        self.assertEqual(st.players[MESSI].pen_goals, 4)
        self.assertEqual(st.players[MESSI].apps, 7)
        self.assertEqual(st.records["total_goals"], 172)
        self.assertEqual(st.teams["ARG"].finish, "Champion")
        self.assertEqual(st.teams["MAR"].finish, "Fourth")
        self.assertEqual(st.teams["MAR"].clean_sheets, 4)

    def test_minutes_are_sane(self):
        st = stats.edition_stats(self.conn, "WC2022")
        for p in st.players.values():
            self.assertLessEqual(p.minutes, 7 * 120)
            self.assertGreaterEqual(p.minutes, 0)
        # Martínez played every minute: 4 x 90 + 3 x 120 (ET in QF, final; R16 normal)... at least 600
        self.assertGreaterEqual(st.players[MARTINEZ].minutes, 600)

    def test_voted_awards_come_from_history(self):
        st = stats.edition_stats(self.conn, "WC2022")
        self.assertEqual(st.awards["golden_ball"][0], MESSI)
        self.assertEqual(st.awards["golden_glove"], [MARTINEZ])

    # ---- branches -----------------------------------------------------
    def test_extra_messi_goal_wins_him_golden_boot(self):
        """8 goals each; Messi has more assists, so he takes the Golden Boot."""
        b = timeline.create_branch(self.conn, "Messi boot")
        fx = timeline.active_fixtures(self.conn, "WC2022")
        f = next(x for x in fx.values() if x.stage == "group" and x.teams() == {"ARG", "MEX"})
        events = list(f.events) + [{"minute": 89, "team_id": "ARG", "player_id": MESSI,
                                    "event_type": "goal", "assist_player_id": None}]
        hs, as_ = (f.home_score + 1, f.away_score) if f.home == "ARG" else (f.home_score, f.away_score + 1)
        timeline.set_result(self.conn, b, f, hs, as_, events=events, lineup=f.lineup, edition_id="WC2022")
        st = stats.edition_stats(self.conn, "WC2022", b)
        self.assertEqual(st.players[MESSI].goals, 8)
        self.assertEqual(st.awards["golden_boot"][:2], [MESSI, MBAPPE])
        # baseline untouched
        self.assertEqual(stats.edition_stats(self.conn, "WC2022").awards["golden_boot"][0], MBAPPE)

    def test_pending_matches_dont_count(self):
        """France knocked out in the group: their knockout goals vanish from the stats."""
        b = timeline.create_branch(self.conn, "France out")
        fx = timeline.active_fixtures(self.conn, "WC2022")
        for opp in ("AUS", "DEN"):
            f = next(x for x in fx.values() if x.stage == "group" and x.teams() == {"FRA", opp})
            hs, as_ = (0, 3) if f.home == "FRA" else (3, 0)
            timeline.set_result(self.conn, b, f, hs, as_, edition_id="WC2022")
        from footsim import engine
        engine.apply_cascade(self.conn, "WC2022", b)
        st = stats.edition_stats(self.conn, "WC2022", b)
        self.assertLess(st.players[MBAPPE].goals, 8)
        self.assertEqual(st.teams["FRA"].finish, "Group stage")
        self.assertNotEqual(st.awards["golden_boot"][0], MBAPPE)

    def test_untouched_branch_reproduces_real_voted_awards(self):
        b = timeline.create_branch(self.conn, "empty")
        st = stats.edition_stats(self.conn, "WC2022", b)
        real = stats.edition_stats(self.conn, "WC2022")
        for award in ("golden_ball", "golden_glove", "best_young"):
            self.assertEqual(st.awards[award][:1], real.awards[award][:1], award)

    def test_awards_follow_performance_in_branches(self):
        """Give Mbappé a hat-trick in the final and France the title: he should take the Golden Ball."""
        b = timeline.create_branch(self.conn, "Mbappé final")
        fx = timeline.active_fixtures(self.conn, "WC2022", b)
        f = next(x for x in fx.values() if x.match_no == 64)
        ev = [e for e in f.events if e["event_type"] not in ("shootout_score", "shootout_miss")]
        ev.append({"minute": 119, "team_id": "FRA", "player_id": MBAPPE, "event_type": "goal", "assist_player_id": None})
        hs, as_ = (f.home_score, f.away_score + 1) if f.away == "FRA" else (f.home_score + 1, f.away_score)
        timeline.set_result(self.conn, b, f, hs, as_, events=ev, lineup=f.lineup, extra_time=1, edition_id="WC2022")
        st = stats.edition_stats(self.conn, "WC2022", b)
        self.assertEqual(st.teams["FRA"].finish, "Champion")
        self.assertEqual(st.awards["golden_ball"][0], MBAPPE)
        self.assertIn(MBAPPE, st.ratings)
        self.assertTrue(3.0 <= min(r for x in st.match_ratings.values() for r in x.values()))

    def test_player_career_totals(self):
        c = stats.player_career(self.conn, MESSI)
        self.assertEqual(c["totals"]["goals"], 7)
        self.assertIn("WC2022", c["editions"])


if __name__ == "__main__":
    unittest.main()
