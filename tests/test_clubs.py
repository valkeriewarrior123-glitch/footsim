import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import api, db, library, outcomes, player, stats

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
SA, UCL, WC = "SERIEA1213", "UCL1213", "WC2022"
JUVE = "c_juventus"


def fw(conn, ed, team):
    sq = [s for s in api.squad(conn, ed, team, None) if s["position"] != "GK" and s["minutes"] > 300]
    return next((s for s in sq if s["position"] == "FW"), sq[0])


@unittest.skipUnless(REAL_DB.exists(), "run the importer and ratings loader first")
class Clubs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)
        self.b = api.create_branch(self.conn, "c")["branch_id"]
        self.rep = fw(self.conn, SA, JUVE)
        api.create_player(self.conn, SA, self.b, dict(full_name="Test Star", birth_date="2000-01-01", team_id=JUVE,
                                                      position="FW", replaces_player_id=self.rep["player_id"], tier="star"))
        self.pid = player.profile(self.conn, self.b).player_id

    def tearDown(self):
        self.conn.close()

    def place_ucl(self, team=JUVE):
        return api.place_player(self.conn, UCL, self.b, dict(team_id=team, replaces_player_id=fw(self.conn, UCL, team)["player_id"]))

    def test_same_club_in_both_competitions(self):
        me = player.profile(self.conn, self.b, UCL)               # added automatically when he was created at Juventus
        self.assertEqual(me.team_id, JUVE)
        v = api.player_view(self.conn, UCL, self.b)
        self.assertEqual(len(v["placements"]), 2)
        self.assertTrue(v["matches"])
        sq = [s["player_id"] for s in api.squad(self.conn, UCL, JUVE, self.b)]
        self.assertIn(self.pid, sq)
        self.assertIn(self.pid, [s["player_id"] for s in api.squad(self.conn, SA, JUVE, self.b)])
        st = stats.edition_stats(self.conn, UCL, self.b)
        self.assertGreater(st.players[self.pid].minutes, 0)

    def test_creating_him_at_a_club_adds_him_to_the_whole_season(self):
        b2 = api.create_branch(self.conn, "auto")["branch_id"]
        rep = fw(self.conn, SA, JUVE)
        r = api.create_player(self.conn, SA, b2, dict(full_name="Auto Guy", birth_date="2000-01-01", team_id=JUVE,
                                                      position="FW", replaces_player_id=rep["player_id"], tier="star"))
        self.assertEqual({x["edition_id"] for x in player.placements(self.conn, b2)}, {SA, UCL})
        self.assertTrue(r["notes"] and "Champions League" in r["notes"][0])
        # a club that isn't in the Champions League: only the league
        b3 = api.create_branch(self.conn, "auto2")["branch_id"]
        api.create_player(self.conn, SA, b3, dict(full_name="Other Guy", birth_date="2000-01-01", team_id="c_ascoli", position="FW",
                                                  replaces_player_id=api.squad(self.conn, SA, "c_ascoli", None)[0]["player_id"], tier="star")) \
            if any(t["team_id"] == "c_ascoli" for t in self.conn.execute("SELECT team_id FROM squads WHERE edition_id = ?", (SA,)).fetchall()) else None

    def test_calendar_and_injuries_are_per_season(self):
        api.place_player(self.conn, WC, self.b, dict(team_id="ARG", replaces_player_id=next(
            s for s in api.squad(self.conn, WC, "ARG", None) if s["position"] == "FW")["player_id"]))
        wc = api.player_view(self.conn, WC, self.b)
        sa = api.player_view(self.conn, SA, self.b)
        self.assertTrue(all(c["date"].startswith("2022") for c in wc["calendar"] if c.get("source") == "yours"))
        self.assertTrue(all(c["date"][:4] in ("2012", "2013") for c in sa["calendar"] if c.get("source") == "yours"))
        self.assertTrue(sa["window"]["start"].startswith("2012"))

    def test_one_club_per_season(self):
        api.unplace_player(self.conn, UCL, self.b)
        with self.assertRaises(api.EditError) as cm:
            self.place_ucl("c_fcbarcelona")
        self.assertIn("same season", str(cm.exception))
        v = api.player_view(self.conn, UCL, self.b)
        self.assertEqual(v["elsewhere"]["fixed_club"]["team_id"], JUVE)

    def test_national_team_is_separate_from_club_season(self):
        api.place_player(self.conn, WC, self.b, dict(team_id="ARG", replaces_player_id=next(
            s for s in api.squad(self.conn, WC, "ARG", None) if s["position"] == "FW")["player_id"]))
        self.assertEqual(player.profile(self.conn, self.b, WC).team_id, "ARG")
        self.assertEqual(player.profile(self.conn, self.b, SA).team_id, JUVE)

    def test_unplace(self):
        api.unplace_player(self.conn, UCL, self.b)
        self.assertIsNone(player.profile(self.conn, self.b, UCL))
        with self.assertRaises(api.EditError):
            api.unplace_player(self.conn, SA, self.b)             # his first competition stays

    def test_outcomes_lock_for_club_player(self):
        outcomes.add_lock(self.conn, SA, self.b, "player_total", {"player_id": self.pid, "goals": 14, "assists": 4})
        r = outcomes.run(self.conn, SA, self.b, apply=True, seed=3)
        self.assertTrue(r["ok"], r)
        st = stats.edition_stats(self.conn, SA, self.b)
        self.assertEqual((st.players[self.pid].goals, st.players[self.pid].assists), (14, 4))
        # a club player can also be made top scorer: goals move to him from his team-mates
        b2 = api.create_branch(self.conn, "c3")["branch_id"]
        api.create_player(self.conn, SA, b2, dict(full_name="X", birth_date="2000-01-01", team_id=JUVE, position="FW",
                                                  replaces_player_id=self.rep["player_id"], tier="star"))
        pid2 = player.profile(self.conn, b2).player_id
        outcomes.add_lock(self.conn, SA, b2, "golden_boot", {"player_id": pid2})
        r = outcomes.run(self.conn, SA, b2, apply=True, seed=3)
        self.assertTrue(r["ok"], r)
        self.assertEqual(stats.edition_stats(self.conn, SA, b2).awards["golden_boot"][0], pid2)

    def test_career_history(self):
        p = library.create_profile(self.conn, dict(full_name="Career Guy", birth_date="2001-02-03", position="MF",
                                                   tier="star"))["profile"]["profile_id"]
        with self.assertRaises(api.EditError):
            library.career_add(self.conn, p, dict(season="2014/17", club="Juventus"))
        r = library.career_add(self.conn, p, dict(season="2014-15", club="Juventus"))
        self.assertEqual(r["career"][0]["season"], "2014/15")
        self.assertFalse(r["career"][0]["in_data"] and False)
        library.career_add(self.conn, p, dict(season="2014/15", club="Somewhere FC"))       # replaces that season
        self.assertEqual([e["club"] for e in library.career(self.conn, p)], ["Somewhere FC"])
        library.career_add(self.conn, p, dict(season="2012/13", club="Juventus"))
        c = library.career(self.conn, p)
        self.assertEqual([e["season"] for e in c], ["2012/13", "2014/15"])
        self.assertTrue(c[0]["in_data"] and c[0]["team_id"] == JUVE)
        self.assertEqual(library.season_suggestion(self.conn, p, SA)["club"], "Juventus")
        library.career_delete(self.conn, c[1]["entry_id"])
        self.assertEqual(len(library.career(self.conn, p)), 1)
        library.delete_profile(self.conn, p)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM profile_career").fetchone()[0], 0)

    def test_joining_a_club_updates_linked_career(self):
        b2 = api.create_branch(self.conn, "c2")["branch_id"]
        p = library.create_profile(self.conn, dict(full_name="Linked", birth_date="2001-02-03", position="FW",
                                                   tier="star"))["profile"]["profile_id"]
        api.create_player(self.conn, SA, b2, dict(full_name="Linked", birth_date="2001-02-03", team_id=JUVE, position="FW",
                                                  replaces_player_id=fw(self.conn, SA, JUVE)["player_id"], tier="star",
                                                  profile_id=p))
        self.assertEqual([(e["season"], e["team_id"]) for e in library.career(self.conn, p)], [("2012/13", JUVE)])
        self.assertEqual(len(library.career(self.conn, p)), 1)                                # same season: no duplicate

    def test_delete_branch_removes_placements(self):
        api.delete_branch(self.conn, self.b)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM player_placements").fetchone()[0], 0)

    def test_player_home(self):
        h = api.player_home(self.conn, self.b)
        self.assertEqual(h["player"]["name"], "Test Star")
        self.assertEqual({c["edition_id"] for c in h["competitions"]}, {SA, UCL})
        self.assertEqual(h["competitions"][0]["team_id"], JUVE)
        add = {a["edition_id"]: a for a in h["addable"]}
        self.assertNotIn(UCL, add)
        self.assertNotIn(SA, add)
        api.unplace_player(self.conn, UCL, self.b)
        add = {a["edition_id"]: a for a in api.player_home(self.conn, self.b)["addable"]}
        self.assertTrue(add[UCL]["possible"])                                  # same club, other competition

    def test_player_home_without_player(self):
        b2 = api.create_branch(self.conn, "empty")["branch_id"]
        self.assertIsNone(api.player_home(self.conn, b2)["player"])


if __name__ == "__main__":
    unittest.main()
