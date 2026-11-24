"""Serie A 2012/13 club data pack: the real season comes through intact."""
import shutil, tempfile, unittest
from pathlib import Path

from footsim import db, engine, validate


class ClubPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = Path(cls.tmp) / "t.db"
        cls.conn = db.init_db(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_pack_is_valid(self):
        self.assertEqual(validate.check_edition(self.conn, "SERIEA1213"), [])

    def test_all_380_matches_and_20_teams(self):
        n, t = self.conn.execute("SELECT COUNT(*), COUNT(DISTINCT home_team_id) FROM fixtures WHERE edition_id='SERIEA1213'").fetchone()
        self.assertEqual((n, t), (380, 20))

    def test_real_final_table(self):
        t = engine.resolve(self.conn, "SERIEA1213", None).tables["A"]
        self.assertEqual([r.team for r in t[:3]], ["c_juventus", "c_napoli", "c_milan"])
        j = t[0]
        self.assertEqual((j.won, j.drawn, j.lost, j.gf, j.ga, j.points), (27, 6, 5, 71, 24, 87))

    def test_juventus_squad_and_gaps_are_declared(self):
        n = self.conn.execute("SELECT COUNT(*) FROM squads WHERE edition_id='SERIEA1213' AND team_id='c_juventus'").fetchone()[0]
        self.assertGreaterEqual(n, 33)
        kinds = {r[0] for r in self.conn.execute("SELECT DISTINCT kind FROM data_notes WHERE edition_id='SERIEA1213'")}
        self.assertIn("subs_estimated", kinds)                          # only Cagliari - Roma has no Transfermarkt line-up
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        self.assertEqual(q("SELECT COUNT(*) FROM data_notes WHERE edition_id='SERIEA1213' AND kind='subs_estimated'"), 1)
        self.assertEqual(q("SELECT COUNT(*) FROM lineups WHERE started = 0 AND fixture_id LIKE 'SERIEA1213%' AND fixture_id != 'SERIEA1213_1286999'"), 2194)   # real substitutes
        self.assertEqual(q("SELECT COUNT(*) FROM match_events WHERE event_type IN ('yellow','second_yellow','red') AND fixture_id LIKE 'SERIEA1213%'"), 2009 + q("SELECT COUNT(*) FROM match_events WHERE event_type IN ('yellow','second_yellow','red') AND fixture_id = 'SERIEA1213_1286999'"))
        self.assertGreater(q("SELECT COUNT(*) FROM match_events WHERE assist_player_id IS NOT NULL AND fixture_id LIKE 'SERIEA1213%'"), 770)
        for r in self.conn.execute("SELECT fixture_id, team_id, COUNT(*) FROM lineups WHERE started = 1 AND fixture_id LIKE 'SERIEA1213%' GROUP BY 1, 2 HAVING COUNT(*) != 11"):
            self.assertIn(r[0], {"SERIEA1213_1286999"} | set(x[0] for x in self.conn.execute("SELECT fixture_id FROM data_notes WHERE kind='lineup_incomplete'")), r)

    def test_reimport_is_a_no_op(self):
        before = self.conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0]
        db.init_db(self.path).close()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0], before)



    def test_table_zones_deductions_and_records(self):
        from footsim import api
        conn = self.conn
        v = api.edition_view(conn, "SERIEA1213", None)
        t = v["groups"]["A"]["table"]
        self.assertEqual([r["pts"] for r in t[13:16]], [42, 40, 39])          # Sampdoria, Atalanta, Torino after deductions
        self.assertEqual(next(r for r in t if r["team"] == "c_siena")["pts"], 30)
        self.assertTrue(any(z.get("kind") == "rel" for z in v["zones"]))
        recs = {r["key"]: r["lines"] for r in api.stats_view(conn, "SERIEA1213", None)["season"]}
        self.assertIn("Cavani", recs["top_scorers"][0])
        self.assertIn("1003", recs["total_goals"][0])
        self.assertIn("Juventus", recs["most_cs"][0])

    # ---------------------------------------------------------------- Champions League 2012/13
    def test_ucl_pack_valid_and_complete(self):
        self.assertEqual(validate.check_edition(self.conn, "UCL1213"), [])
        n = self.conn.execute("SELECT COUNT(*) FROM fixtures WHERE edition_id='UCL1213'").fetchone()[0]
        self.assertEqual(n, 125)                                       # 96 group + 8*2 + 4*2 + 2*2 + final

    def test_ucl_groups_follow_uefa_tiebreaks(self):
        t = engine.resolve(self.conn, "UCL1213", None).tables
        self.assertEqual([r.team for r in t["E"][:3]], ["c_juventus", "c_shakhtardonetsk", "c_chelseafc"])  # Chelsea out on away goals
        self.assertEqual([r.team for r in t["H"][:3]], ["c_manchesterunited", "c_galatasaray", "c_cfrcluj"])
        self.assertEqual(t["A"][0].points, 15)

    def test_ucl_ties_aggregate_and_away_goals(self):
        res = engine.resolve(self.conn, "UCL1213", None)
        slot = {s.match_no: s for s in res.slots}
        self.assertTrue(all(s.status == "valid" for s in res.slots))
        arsenal = next(s for s in res.slots if s.home == "c_arsenalfc")
        self.assertEqual((arsenal.fixture.home_score, arsenal.fixture.away_score, arsenal.fixture.decided_by),
                         (3, 3, "away goals"))
        self.assertEqual(arsenal.fixture.winner(), "c_bayernmunchen")
        celtic = next(s for s in res.slots if s.home == "c_celticfc")
        self.assertEqual((celtic.fixture.home_score, celtic.fixture.away_score), (0, 5))
        self.assertEqual((res.champion, res.runner_up), ("c_bayernmunchen", "c_borussiadortmund"))

    def test_ucl_tie_legs_in_view_and_editable(self):
        from footsim import api, timeline
        v = api.edition_view(self.conn, "UCL1213", None)
        tie = next(s for s in v["slots"] if s["home"] == "c_celticfc")
        self.assertEqual(len(tie["legs"]), 2)
        b = timeline.create_branch(self.conn, "t")
        d = api.fixture_detail(self.conn, "UCL1213", tie["legs"][0]["fixture_id"], b)
        self.assertTrue(d["editable"])
        self.assertEqual(d["tie"]["leg"], 1)
        self.assertEqual(d["tie"]["other"]["fixture_id"], tie["legs"][1]["fixture_id"])

    def test_ucl_details_are_declared(self):
        kinds = {r[0] for r in self.conn.execute("SELECT DISTINCT kind FROM data_notes WHERE edition_id='UCL1213'")}
        self.assertEqual(kinds, set())                                 # nothing is estimated or missing any more
        self.assertNotIn("scorer_unknown", kinds)                      # every goal has a scorer from Wikipedia
        unnamed = self.conn.execute("SELECT COUNT(*) FROM match_events WHERE fixture_id LIKE 'UCL1213%' AND event_type IN ('goal','pen_goal','own_goal') AND player_id IS NULL").fetchone()[0]
        self.assertEqual(unnamed, 0)
        goals = self.conn.execute("SELECT COUNT(*) FROM match_events WHERE fixture_id LIKE 'UCL1213%' AND event_type IN ('goal','pen_goal','own_goal')").fetchone()[0]
        self.assertEqual(goals, 368)
        top = self.conn.execute("""SELECT p.full_name, COUNT(*) c FROM match_events e JOIN players p USING(player_id)
                                   WHERE e.fixture_id LIKE 'UCL1213%' AND e.event_type IN ('goal','pen_goal') GROUP BY p.player_id ORDER BY c DESC LIMIT 1""").fetchone()
        self.assertEqual(tuple(top), ("Cristiano Ronaldo", 12))                  # Cristiano Ronaldo, top scorer of 2012/13
        late = self.conn.execute("SELECT MIN(date) FROM fixtures WHERE fixture_id LIKE 'UCL1213_G%'").fetchone()[0]
        self.assertTrue(self.conn.execute("SELECT MAX(date) FROM fixtures WHERE fixture_id LIKE 'UCL1213_G%'").fetchone()[0] < "2013-01-01" and late.startswith("2012"))
        n = self.conn.execute("SELECT COUNT(DISTINCT fixture_id) FROM lineups WHERE started = 1 AND fixture_id LIKE 'UCL1213%'").fetchone()[0]
        self.assertEqual(n, 125)                                       # every match has a real starting eleven
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        self.assertEqual(q("SELECT COUNT(*) FROM lineups WHERE started = 0 AND fixture_id LIKE 'UCL1213%'"), 702)     # real substitutes
        self.assertEqual(q("SELECT COUNT(*) FROM match_events WHERE event_type IN ('yellow','second_yellow','red') AND fixture_id LIKE 'UCL1213%'"), 533)
        self.assertEqual(q("SELECT COUNT(*) FROM match_events WHERE assist_player_id IS NOT NULL AND fixture_id LIKE 'UCL1213%'"), 325)
        # Dinamo Zagreb - Porto: Lucho Gonzalez scored in the 41st minute from an Alex Sandro pass
        row = self.conn.execute("""SELECT s.full_name, a.full_name FROM match_events e JOIN players s ON s.player_id = e.player_id
                                   JOIN players a ON a.player_id = e.assist_player_id WHERE e.fixture_id = 'UCL1213_GA02' AND e.minute = 41""").fetchone()
        self.assertEqual(tuple(row), ("Lucho González", "Alex Sandro"))
        on = q("SELECT minute_on FROM lineups l JOIN players p USING(player_id) WHERE l.fixture_id = 'UCL1213_GA02' AND p.full_name LIKE '%Atsu%'")
        self.assertEqual(on, 72)                                       # Christian Atsu came on in the 72nd minute
        for r in self.conn.execute("SELECT fixture_id, team_id, COUNT(*) FROM lineups WHERE started = 1 AND fixture_id LIKE 'UCL1213%' GROUP BY 1, 2"):
            self.assertEqual(r[2], 11)

    def test_older_pack_data_is_replaced_when_the_pack_version_goes_up(self):
        c = self.conn
        c.execute("DELETE FROM match_events WHERE fixture_id LIKE 'UCL1213_G%'")        # an old save: no scorers at all
        c.execute("UPDATE pack_versions SET data_version = 1 WHERE edition_id = 'UCL1213'")
        c.commit()
        db.apply_club_packs(c)
        n = c.execute("SELECT COUNT(*) FROM match_events WHERE fixture_id LIKE 'UCL1213%' AND player_id IS NOT NULL").fetchone()[0]
        self.assertGreater(n, 300)
        self.assertEqual(c.execute("SELECT data_version FROM pack_versions WHERE edition_id='UCL1213'").fetchone()[0], 4)
        self.assertEqual(c.execute("SELECT data_version FROM pack_versions WHERE edition_id='SERIEA1213'").fetchone()[0], 4)
        self.assertEqual(validate.check_edition(c, "UCL1213"), [])


class SerieA1314(unittest.TestCase):
    def test_the_second_season_is_in_the_pack(self):
        from footsim import api, stats
        d = Path(tempfile.mkdtemp()) / "t.db"
        c = db.init_db(d)
        self.assertEqual(validate.check_edition(c, "SERIEA1314"), [])
        v = api.edition_view(c, "SERIEA1314", None)
        t = v["groups"]["A"]["table"]
        self.assertEqual((v["champion"], t[0]["pts"], t[0]["gf"], t[0]["ga"]), ("c_juventus", 102, 80, 23))
        self.assertEqual([r["team"] for r in t[:3]], ["c_juventus", "c_roma", "c_napoli"])
        self.assertEqual({r["team"] for r in t[-3:]}, {"c_livorno", "c_bologna", "c_catania"})
        self.assertEqual(set(v["promoted"]), {"c_hellasverona", "c_livorno", "c_sassuolo"})
        st = stats.edition_stats(c, "SERIEA1314", None)
        self.assertEqual(max(p.goals for p in st.players.values()), 22)                  # Immobile
        self.assertEqual(c.execute("SELECT COUNT(*) FROM match_events WHERE fixture_id LIKE 'SERIEA1314%' AND event_type IN ('yellow','second_yellow','red')").fetchone()[0], 1906)


if __name__ == "__main__":
    unittest.main()
