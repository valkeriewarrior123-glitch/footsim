"""Engine tests. Need footsim.db built first:
    python -m footsim.import_statsbomb <open-data/data>
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import db, engine, timeline
from footsim.timeline import Fixture

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"

REAL_GROUPS = {"A": ["NED", "SEN"], "B": ["ENG", "USA"], "C": ["ARG", "POL"], "D": ["FRA", "AUS"],
               "E": ["JPN", "ESP"], "F": ["MAR", "CRO"], "G": ["BRA", "SUI"], "H": ["POR", "KOR"]}


@unittest.skipUnless(REAL_DB.exists(), "run the importer first")
class WorldCup2022(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)

    def tearDown(self):
        self.conn.close()

    def fixture(self, a, b) -> Fixture:
        fx = timeline.active_fixtures(self.conn, "WC2022")
        return next(f for f in fx.values() if f.stage == "group" and f.teams() == {a, b})

    # ---- gate test -------------------------------------------------
    def test_gate_zero_overrides_reproduces_history(self):
        res = engine.resolve(self.conn, "WC2022")
        for g, top2 in REAL_GROUPS.items():
            self.assertEqual([r.team for r in res.tables[g][:2]], top2, f"group {g}")
        self.assertTrue(all(s.status == "valid" for s in res.slots))
        self.assertEqual((res.champion, res.runner_up, res.third), ("ARG", "FRA", "CRO"))
        self.assertEqual(res.violations, [])
        self.assertFalse(any(r.decided_by_lots for rows in res.tables.values() for r in rows))

    def test_empty_branch_equals_baseline(self):
        b = timeline.create_branch(self.conn, "empty")
        self.assertEqual(engine.resolve(self.conn, "WC2022", b).champion, "ARG")

    # ---- cascade ---------------------------------------------------
    def test_spain_beat_japan_cascades(self):
        b = timeline.create_branch(self.conn, "Spain top")
        f = self.fixture("JPN", "ESP")
        hs, as_ = (1, 2) if f.home == "JPN" else (2, 1)
        timeline.set_result(self.conn, b, f, hs, as_, edition_id="WC2022",
                            events=[{"minute": 10, "team_id": "ESP", "player_id": None, "event_type": "goal"},
                                    {"minute": 20, "team_id": "ESP", "player_id": None, "event_type": "goal"},
                                    {"minute": 30, "team_id": "JPN", "player_id": None, "event_type": "goal"}])
        res = engine.apply_cascade(self.conn, "WC2022", b)
        self.assertEqual([r.team for r in res.tables["E"][:2]], ["ESP", "GER"])
        s53, s55 = res.slot(53), res.slot(55)
        self.assertEqual((s53.home, s53.away, s53.status), ("ESP", "CRO", "pending"))
        self.assertEqual((s55.home, s55.away, s55.status), ("MAR", "GER", "pending"))
        # untouched side of the bracket keeps its real results
        for n in (49, 50, 51, 52, 54, 56, 58, 60):
            self.assertEqual(res.slot(n).status, "valid", n)
        # everything downstream waits
        for n in (57, 59, 61, 62, 63, 64):
            self.assertEqual(res.slot(n).status, "tbd", n)
        self.assertIsNone(res.champion)
        # pending rows were stored with the new pairings
        rows = self.conn.execute("SELECT home_team_id, away_team_id FROM fixture_overrides "
                                 "WHERE branch_id = ? AND status = 'pending'", (b,)).fetchall()
        self.assertEqual({frozenset(r) for r in rows}, {frozenset(("ESP", "CRO")), frozenset(("MAR", "GER"))})

    def test_resolving_pending_moves_bracket_forward(self):
        b = timeline.create_branch(self.conn, "Spain top")
        f = self.fixture("JPN", "ESP")
        timeline.set_result(self.conn, b, f, *((1, 2) if f.home == "JPN" else (2, 1)), edition_id="WC2022")
        res = engine.apply_cascade(self.conn, "WC2022", b)
        fx = timeline.active_fixtures(self.conn, "WC2022", b)
        m53 = next(x for x in fx.values() if x.match_no == 53)
        timeline.set_result(self.conn, b, m53, 2 if m53.home == "ESP" else 0, 0 if m53.home == "ESP" else 2,
                            edition_id="WC2022")
        res = engine.apply_cascade(self.conn, "WC2022", b)
        # QF 57 = W53 v W54 -> ESP v BRA, never happened -> pending
        self.assertEqual({res.slot(57).home, res.slot(57).away}, {"ESP", "BRA"})
        self.assertEqual(res.slot(57).status, "pending")

    def test_second_order_reuses_real_meeting(self):
        # Pretend MAR v GER happened at an earlier World Cup knockout.
        self.conn.execute("INSERT INTO editions VALUES ('WC2018X','WC','2018','2018-06-14','2018-07-15',0,'test')")
        self.conn.execute("""INSERT INTO fixtures VALUES ('old1','WC2018X','R16',NULL,NULL,NULL,'2018-07-01',
                             'GER','MAR',2,1,0,NULL,NULL,1)""")
        self.conn.commit()
        b = timeline.create_branch(self.conn, "reuse")
        f = self.fixture("JPN", "ESP")
        timeline.set_result(self.conn, b, f, *((1, 2) if f.home == "JPN" else (2, 1)), edition_id="WC2022")
        res = engine.apply_cascade(self.conn, "WC2022", b)
        s55 = res.slot(55)
        self.assertEqual(s55.status, "valid")
        self.assertEqual(s55.fixture.origin, "reused_historical")
        self.assertEqual(s55.fixture.winner(), "GER")

    def test_fork_inherits_parent(self):
        parent = timeline.create_branch(self.conn, "parent")
        f = self.fixture("JPN", "ESP")
        timeline.set_result(self.conn, parent, f, *((1, 2) if f.home == "JPN" else (2, 1)), edition_id="WC2022")
        child = timeline.create_branch(self.conn, "child", parent_branch_id=parent)
        res = engine.resolve(self.conn, "WC2022", child)
        self.assertEqual(res.tables["E"][0].team, "ESP")

    def test_undo_group_edit_restores_real_knockouts(self):
        b = timeline.create_branch(self.conn, "undo one")
        f = self.fixture("JPN", "ESP")
        timeline.set_result(self.conn, b, f, *((1, 2) if f.home == "JPN" else (2, 1)), edition_id="WC2022")
        engine.apply_cascade(self.conn, "WC2022", b)
        # put the real result back as a user edit, then cascade
        timeline.set_result(self.conn, b, f, f.home_score, f.away_score, events=f.events,
                            lineup=f.lineup, edition_id="WC2022")
        res = engine.apply_cascade(self.conn, "WC2022", b)
        self.assertEqual(res.champion, "ARG")
        self.assertTrue(all(s.status == "valid" for s in res.slots))
        left = self.conn.execute("SELECT COUNT(*) FROM fixture_overrides WHERE branch_id = ? "
                                 "AND origin IN ('cascade','reused_historical')", (b,)).fetchone()[0]
        self.assertEqual(left, 0)

    def test_reverting_override_restores_history(self):
        b = timeline.create_branch(self.conn, "undo")
        f = self.fixture("JPN", "ESP")
        timeline.set_result(self.conn, b, f, *((1, 2) if f.home == "JPN" else (2, 1)), edition_id="WC2022")
        engine.apply_cascade(self.conn, "WC2022", b)
        self.conn.execute("DELETE FROM fixture_overrides WHERE branch_id = ?", (b,))
        self.assertEqual(engine.resolve(self.conn, "WC2022", b).champion, "ARG")


class Suspensions(unittest.TestCase):
    """Synthetic checks of card accumulation."""

    def run_cards(self, matches):
        fx = {}
        for i, (stage, cards, lineup) in enumerate(matches):
            f = Fixture(f"f{i}", stage, "A" if stage == "group" else None, None, f"2022-11-{20 + i}",
                        "AAA", "BBB", 1, 0)
            f.events = [{"minute": 10, "team_id": "AAA", "player_id": p, "event_type": t,
                         "assist_player_id": None} for p, t in cards]
            f.lineup = [{"team_id": "AAA", "player_id": p, "started": 1} for p in lineup]
            fx[f.fixture_id] = f
        res = engine.Resolution("X", {}, [])
        engine._apply_suspensions(res, fx, {"suspensions": {"yellows_for_ban": 2,
                                                             "yellows_reset_after_stage": "QF"}})
        return res

    def test_two_yellows_ban_next_match(self):
        res = self.run_cards([("group", [("p1", "yellow")], ["p1"]),
                              ("group", [("p1", "yellow")], ["p1"]),
                              ("group", [], ["p1"])])
        self.assertEqual(res.suspensions.get("f2"), {"AAA": ["p1"]})
        self.assertEqual(res.violations, [("f2", "p1")])

    def test_second_yellow_bans_without_accumulating(self):
        res = self.run_cards([("group", [("p1", "yellow"), ("p1", "second_yellow")], []),
                              ("group", [], []),
                              ("group", [("p1", "yellow")], [])])
        self.assertEqual(res.suspensions.get("f1"), {"AAA": ["p1"]})
        self.assertNotIn("f3", res.suspensions)

    def test_yellows_reset_after_quarter_final(self):
        res = self.run_cards([("QF", [("p1", "yellow")], []),
                              ("SF", [("p1", "yellow")], []),
                              ("F", [], [])])
        self.assertNotIn("f2", res.suspensions)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(REAL_DB.exists(), "run the importer first")
class SuspensionsCarryForward(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)
        self.b = timeline.create_branch(self.conn, "cards")
        self.kimmich = self.conn.execute("SELECT player_id FROM players WHERE full_name LIKE '%Kimmich%'").fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def book(self, a, b):
        fx = timeline.active_fixtures(self.conn, "WC2022", self.b)
        f = next(x for x in fx.values() if x.stage == "group" and x.teams() == {a, b})
        ev = [e for e in f.events] + [{"minute": 30, "team_id": "GER", "player_id": self.kimmich,
                                       "event_type": "yellow", "assist_player_id": None}]
        timeline.set_result(self.conn, self.b, f, f.home_score, f.away_score, events=ev, lineup=f.lineup,
                            edition_id="WC2022")
        return engine.apply_cascade(self.conn, "WC2022", self.b), f

    def test_ban_from_edits_keeps_him_out_of_next_real_match(self):
        # Kimmich was really booked against Spain; one more yellow against Japan means a ban v Costa Rica
        res, f2 = self.book("GER", "JPN")
        self.assertEqual(res.violations, [])
        self.assertTrue(any(x["player_id"] == self.kimmich for x in res.fixes))
        fx = timeline.active_fixtures(self.conn, "WC2022", self.b)
        crc = next(x for x in fx.values() if x.stage == "group" and x.teams() == {"GER", "CRC"})
        self.assertNotIn(self.kimmich, {l["player_id"] for l in crc.lineup})
        self.assertEqual((crc.home_score, crc.away_score), (2, 4) if crc.home == "CRC" else (4, 2))
        self.assertEqual(crc.origin, "suspension_fix")
        # undo the booking: he's available again and the fix disappears
        self.conn.execute("DELETE FROM fixture_overrides WHERE branch_id = ? AND fixture_id = ?", (self.b, f2.fixture_id))
        self.conn.commit()
        engine.apply_cascade(self.conn, "WC2022", self.b)
        fx = timeline.active_fixtures(self.conn, "WC2022", self.b)
        crc = next(x for x in fx.values() if x.stage == "group" and x.teams() == {"GER", "CRC"})
        self.assertIn(self.kimmich, {l["player_id"] for l in crc.lineup})
        self.assertEqual(crc.origin, "baseline")
