"""Two-legged ties: edit a leg, aggregate/away-goals/shootout rules, the bracket following, rolling."""
import shutil, tempfile, unittest
from pathlib import Path

from footsim import api, db, engine, roller, timeline

LEG1, LEG2 = "UCL1213_R16_49_1", "UCL1213_R16_49_2"      # Celtic 0-3 Juventus, Juventus 2-0 Celtic (real)


class Ties(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = db.init_db(Path(cls.tmp) / "t.db")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def branch(self):
        return timeline.create_branch(self.conn, "t")

    def edit(self, b, fid, hs, as_, **kw):
        f = timeline.active_fixtures(self.conn, "UCL1213", b)[fid]
        events = []
        for team, n in ((f.home, hs), (f.away, as_)):
            who = next(p["player_id"] for p in api.squad(self.conn, "UCL1213", team, b) if p.get("position") != "GK")
            events += [{"minute": 10 + 12 * i, "team_id": team, "player_id": who, "event_type": "goal",
                        "assist_player_id": None} for i in range(n)]
        return api.edit_fixture(self.conn, "UCL1213", b, fid, {"home_score": hs, "away_score": as_, "events": events, **kw})

    def slot(self, b, no):
        return engine.resolve(self.conn, "UCL1213", b).slot(no)

    def test_real_tie_unchanged_by_a_branch(self):
        b = self.branch()
        s = self.slot(b, 49)
        self.assertEqual((s.status, s.fixture.winner()), ("valid", "c_juventus"))

    def test_edit_first_leg_changes_the_winner_and_the_next_round(self):
        b = self.branch()
        self.edit(b, LEG1, 3, 0)                           # Celtic 3-0 Juventus; Juventus won leg 2 by 2-0: 3-2 Celtic
        s = self.slot(b, 49)
        self.assertEqual((s.status, s.fixture.winner(), s.fixture.decided_by), ("valid", "c_celticfc", "aggregate"))
        qf = next(x for x in engine.resolve(self.conn, "UCL1213", b).slots if x.stage == "QF" and "c_celticfc" in (x.home, x.away))
        self.assertEqual(qf.stage, "QF")
        self.assertEqual(qf.status, "pending")             # Celtic have not played this quarter-final: both legs need a result
        legs = [f for f in timeline.active_fixtures(self.conn, "UCL1213", b).values() if f.match_no == qf.match_no]
        self.assertEqual(sorted((f.leg, f.home_score) for f in legs), [(1, None), (2, None)])
        self.assertEqual({legs[0].home, legs[0].away}, {qf.home, qf.away})

    def test_level_tie_needs_extra_time_and_a_shootout(self):
        b = self.branch()
        with self.assertRaises(api.EditError):             # 0-3 on the night, Celtic 3-0 away... level 3-3 on aggregate and on away goals
            self.edit(b, LEG2, 0, 3)
        with self.assertRaises(api.EditError):
            self.edit(b, LEG2, 0, 3, extra_time=1)         # shootout still missing
        self.edit(b, LEG2, 0, 3, extra_time=1, pen_home=4, pen_away=3)
        s = self.slot(b, 49)
        self.assertEqual((s.fixture.winner(), s.fixture.decided_by), ("c_juventus", "penalties"))
        self.assertEqual((s.fixture.pen_home, s.fixture.pen_away), (3, 4))   # listed in first-leg order: Celtic, Juventus

    def test_away_goals_settle_it_without_a_shootout(self):
        b = self.branch()
        with self.assertRaises(api.EditError):
            self.edit(b, LEG2, 1, 4)                       # level 4-4, Celtic scored 4 away: extra time was played
        self.edit(b, LEG2, 1, 4, extra_time=1)
        s = self.slot(b, 49)
        self.assertEqual((s.fixture.winner(), s.fixture.decided_by), ("c_celticfc", "away goals"))
        with self.assertRaises(api.EditError):
            self.edit(b, LEG2, 1, 4, extra_time=1, pen_home=3, pen_away=1)   # no shootout when away goals decide

    def test_first_leg_has_no_extra_time_or_shootout(self):
        b = self.branch()
        with self.assertRaises(api.EditError):
            self.edit(b, LEG1, 1, 1, extra_time=1, pen_home=4, pen_away=3)
        self.edit(b, LEG1, 1, 1)                           # a draw is fine on its own

    def test_detail_describes_the_tie(self):
        b = self.branch()
        d = api.fixture_detail(self.conn, "UCL1213", LEG2, b)
        self.assertEqual((d["tie"]["leg"], d["tie"]["other"]["fixture_id"], d["tie"]["away_goals"]), (2, LEG1, True))

    def test_revert_a_leg(self):
        b = self.branch()
        self.edit(b, LEG1, 3, 0)
        api.revert_fixture(self.conn, "UCL1213", b, LEG1)
        self.assertEqual(self.slot(b, 49).fixture.winner(), "c_juventus")
        self.assertEqual(len(engine.resolve(self.conn, "UCL1213", b).slots), 15)

    def test_roll_pending_plays_both_legs_and_finds_a_champion(self):
        b = self.branch()
        self.edit(b, LEG1, 3, 0)
        roller.roll_pending(self.conn, "UCL1213", b, seed=7)
        res = engine.resolve(self.conn, "UCL1213", b)
        self.assertTrue(res.champion)
        for s in res.slots:
            self.assertEqual(s.status, "valid", (s.match_no, s.reason))
            if s.legs == 2:
                f1, f2 = s.fixture.legs
                self.assertFalse(f1.extra_time or f1.pen_home is not None)
                self.assertTrue(s.fixture.agg_winner)

    def test_rolling_a_second_leg_follows_the_aggregate(self):
        b = self.branch()
        self.edit(b, LEG1, 3, 0)
        f = timeline.active_fixtures(self.conn, "UCL1213", b)[LEG2]
        for seed in range(12):
            out = roller.roll(self.conn, "UCL1213", b, f, seed=seed)
            api._validate(f, out, timeline.tie_rule(self.conn, "UCL1213", b, f))
