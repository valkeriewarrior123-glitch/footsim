import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import api, db, player, roller, stats, timeline

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
ED = "WC2022"
MESSI = "sb_5503"


@unittest.skipUnless(REAL_DB.exists(), "run the importer and ratings loader first")
class CreatedPlayer(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)
        self.b = api.create_branch(self.conn, "career")["branch_id"]
        self.dybala = next(p for p in api.squad(self.conn, ED, "ARG") if "Dybala" in p["name"])["player_id"]
        self.me = api.create_player(self.conn, ED, self.b, {
            "full_name": "Mateo Rivas", "birth_date": "2001-03-14", "team_id": "ARG", "position": "FW",
            "replaces_player_id": self.dybala, "tier": "world_class"})["player_id"]

    def tearDown(self):
        self.conn.close()

    def fx(self, a, b, stage=None, raw=False):
        fx = timeline.active_fixtures(self.conn, ED, self.b, raw=raw)
        return next(f for f in fx.values() if f.teams() == {a, b} and (stage is None or f.stage == stage))

    # ---- creation ------------------------------------------------------
    def test_squad_swap(self):
        ids = {p["player_id"] for p in api.squad(self.conn, ED, "ARG", self.b)}
        self.assertIn(self.me, ids)
        self.assertNotIn(self.dybala, ids)
        self.assertEqual(len(ids), 26)
        # real history is untouched
        self.assertIn(self.dybala, {p["player_id"] for p in api.squad(self.conn, ED, "ARG")})

    def test_validation(self):
        b2 = api.create_branch(self.conn, "x")["branch_id"]
        base = {"full_name": "A", "birth_date": "2001-01-01", "team_id": "ARG", "position": "FW",
                "replaces_player_id": self.dybala, "tier": "star"}
        for bad in ({"birth_date": "not a date"}, {"position": "XX"}, {"tier": "god"}, {"overall": 120},
                    {"replaces_player_id": "sb_3009"}):          # Mbappé isn't Argentinian
            with self.assertRaises(api.EditError, msg=bad):
                api.create_player(self.conn, ED, b2, {**base, **bad})
        with self.assertRaises(api.EditError):                      # one player per branch
            api.create_player(self.conn, ED, self.b, base)

    def test_tier_sets_overall(self):
        self.assertEqual(player.profile(self.conn, self.b).overall, 89)
        api.update_player(self.conn, self.b, {"overall": 91})
        self.assertEqual(player.profile(self.conn, self.b).overall, 91)

    # ---- the lineup rule ----------------------------------------------------
    def test_auto_role_takes_replaced_players_minutes(self):
        st = stats.edition_stats(self.conn, ED, self.b)
        real = stats.edition_stats(self.conn, ED).players[self.dybala]
        mine = st.players[self.me]
        self.assertEqual((mine.apps, mine.minutes, mine.shootout_scored), (real.apps, real.minutes, real.shootout_scored))
        self.assertNotIn(self.dybala, st.players)
        self.assertEqual(st.resolution.champion, "ARG")

    def test_start_in_messis_place_takes_his_goals(self):
        final = self.fx("ARG", "FRA", "F", raw=True)
        api.set_role(self.conn, ED, self.b, final.fixture_id, {"role": "start", "target_player_id": MESSI})
        st = stats.edition_stats(self.conn, ED, self.b)
        self.assertEqual(st.players[MESSI].goals, 5)
        self.assertEqual(st.players[self.me].goals, 2)
        self.assertEqual(st.resolution.champion, "ARG")      # real result stands

    def test_sub_takes_only_later_events(self):
        final = self.fx("ARG", "FRA", "F", raw=True)
        # Messi scored at 23' (pen) and 108'. Coming on for him at 100' takes only the second.
        api.set_role(self.conn, ED, self.b, final.fixture_id, {"role": "sub", "target_player_id": MESSI, "minute": 100})
        f = self.fx("ARG", "FRA", "F")
        mine = [e for e in f.events if e["player_id"] == self.me and e["event_type"] in ("goal", "pen_goal")]
        self.assertEqual(len(mine), 1)
        self.assertGreaterEqual(mine[0]["minute"], 100)
        me_l = next(l for l in f.lineup if l["player_id"] == self.me)
        self.assertEqual((me_l["started"], me_l["minute_on"]), (0, 100))

    def test_unused_bench_player_fills_replaced_spot(self):
        final = self.fx("ARG", "FRA", "F", raw=True)
        api.set_role(self.conn, ED, self.b, final.fixture_id, {"role": "unused"})
        f = self.fx("ARG", "FRA", "F")
        ids = {l["player_id"] for l in f.lineup}
        self.assertNotIn(self.me, ids)
        self.assertNotIn(self.dybala, ids)
        self.assertEqual(sum(1 for l in f.lineup if l["team_id"] == "ARG" and l["started"]), 11)
        self.assertEqual(sum(1 for l in self.fx("ARG", "FRA", "F", raw=True).lineup if l["team_id"] == "ARG"),
                         sum(1 for l in f.lineup if l["team_id"] == "ARG"))

    def test_injury_blocks_playing(self):
        final = self.fx("ARG", "FRA", "F", raw=True)
        api.add_injury(self.conn, ED, self.b, {"start_date": "2022-12-15", "end_date": "2022-12-31"})
        with self.assertRaises(api.EditError):
            api.set_role(self.conn, ED, self.b, final.fixture_id, {"role": "start"})
        f = self.fx("ARG", "FRA", "F")
        self.assertNotIn(self.me, {l["player_id"] for l in f.lineup})

    # ---- strict ripple ---------------------------------------------------
    def test_only_own_team_matches_can_be_edited(self):
        f = self.fx("FRA", "AUS")
        with self.assertRaisesRegex(api.EditError, "Focus mode"):
            api.edit_fixture(self.conn, ED, self.b, f.fixture_id, {"home_score": 0, "away_score": 0, "events": []})
        with self.assertRaises(api.EditError):
            api.roll_preview(self.conn, ED, self.b, f.fixture_id)

    def test_own_team_edit_and_cascade(self):
        f = self.fx("ARG", "KSA")
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "start"})
        hs, as_ = (2, 0) if f.home == "ARG" else (0, 2)
        r = api.edit_fixture(self.conn, ED, self.b, f.fixture_id, {"home_score": hs, "away_score": as_, "events": [
            {"minute": 10, "team_id": "ARG", "player_id": self.me, "event_type": "goal"},
            {"minute": 30, "team_id": "ARG", "player_id": self.me, "event_type": "goal"}]})
        self.assertTrue(r["ok"])
        self.assertEqual(stats.edition_stats(self.conn, ED, self.b).players[self.me].goals, 2)

    def test_events_need_player_on_pitch(self):
        f = self.fx("ARG", "KSA", raw=True)
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "unused"})
        hs, as_ = (1, 0) if f.home == "ARG" else (0, 1)
        with self.assertRaisesRegex(api.EditError, "isn't playing"):
            api.edit_fixture(self.conn, ED, self.b, f.fixture_id, {"home_score": hs, "away_score": as_, "events": [
                {"minute": 10, "team_id": "ARG", "player_id": self.me, "event_type": "goal"}]})

    def test_other_teams_pending_can_only_be_rolled(self):
        f = self.fx("ARG", "AUS", "R16", raw=True)    # make Argentina lose the R16 -> AUS go through
        hs, as_ = (0, 1) if f.home == "ARG" else (1, 0)
        aus = api.squad(self.conn, ED, "AUS")[-1]["player_id"]
        api.edit_fixture(self.conn, ED, self.b, f.fixture_id, {"home_score": hs, "away_score": as_, "events": [
            {"minute": 50, "team_id": "AUS", "player_id": aus, "event_type": "goal"}]})
        view = api.edition_view(self.conn, ED, self.b)
        qf = next(s for s in view["slots"] if s["match_no"] == 58)
        self.assertEqual((qf["status"], {qf["home"], qf["away"]}), ("pending", {"NED", "AUS"}))
        with self.assertRaises(api.EditError):
            api.edit_fixture(self.conn, ED, self.b, qf["fixture_id"], {"home_score": 1, "away_score": 0, "events": []})
        api.roll_save(self.conn, ED, self.b, qf["fixture_id"])
        view = api.edition_view(self.conn, ED, self.b)
        self.assertEqual(next(s for s in view["slots"] if s["match_no"] == 58)["status"], "valid")

    # ---- roller --------------------------------------------------------
    def test_roller_honours_role_and_rating(self):
        f = self.fx("ARG", "MEX")
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "start"})
        info = roller._player_info(self.conn, ED)
        goals = apps = 0
        for i in range(300):
            r = roller.roll(self.conn, ED, self.b, f, seed=i, info=info)
            apps += any(l["player_id"] == self.me and l["started"] for l in r["lineup"])
            goals += sum(1 for e in r["events"] if e["player_id"] == self.me and e["event_type"] in ("goal", "pen_goal"))
        self.assertEqual(apps, 300)
        self.assertTrue(0.3 <= goals / 300 <= 1.2, goals / 300)   # a world-class forward scores often

        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "unused"})
        r = roller.roll(self.conn, ED, self.b, f, seed=1, info=info)
        self.assertNotIn(self.me, {l["player_id"] for l in r["lineup"]})

    def test_role_change_on_edited_match_moves_events(self):
        f = self.fx("ARG", "KSA")
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "start"})
        hs, as_ = (1, 0) if f.home == "ARG" else (0, 1)
        api.edit_fixture(self.conn, ED, self.b, f.fixture_id, {"home_score": hs, "away_score": as_, "events": [
            {"minute": 10, "team_id": "ARG", "player_id": self.me, "event_type": "goal"}]})
        api.set_role(self.conn, ED, self.b, f.fixture_id, {"role": "unused"})
        st = stats.edition_stats(self.conn, ED, self.b)
        self.assertEqual(st.players.get(self.me).goals if self.me in st.players else 0,
                         sum(1 for m in api.player_view(self.conn, ED, self.b)["matches"]
                             for e in m["events"] if e["type"] == "goal"))
        f2 = self.fx("ARG", "KSA")
        self.assertNotIn(self.me, {l["player_id"] for l in f2.lineup})
        self.assertEqual(sum(1 for e in f2.events if e["event_type"] == "goal" and e["team_id"] == "ARG"), 1)

    # ---- forks and views -------------------------------------------------
    def test_fork_keeps_player_roles_and_injuries(self):
        final = self.fx("ARG", "FRA", "F", raw=True)
        api.set_role(self.conn, ED, self.b, final.fixture_id, {"role": "start", "target_player_id": MESSI})
        api.add_injury(self.conn, ED, self.b, {"start_date": "2022-11-01", "end_date": "2022-11-05"})
        child = api.create_branch(self.conn, "fork", self.b)["branch_id"]
        self.assertEqual(player.profile(self.conn, child).player_id, self.me)
        self.assertEqual(stats.edition_stats(self.conn, ED, child).players[self.me].goals,
                         stats.edition_stats(self.conn, ED, self.b).players[self.me].goals)
        self.assertEqual(len(api.player_view(self.conn, ED, child)["injuries"]), 1)

    def test_player_view(self):
        v = api.player_view(self.conn, ED, self.b)
        self.assertEqual(v["player"]["name"], "Mateo Rivas")
        self.assertEqual(len(v["matches"]), 7)
        self.assertEqual(v["unset"], 7)
        self.assertTrue(any(c["kind"] == "squad_deadline" for c in v["calendar"]))
        self.assertIsNone(api.player_view(self.conn, ED, None)["player"])


if __name__ == "__main__":
    unittest.main()
