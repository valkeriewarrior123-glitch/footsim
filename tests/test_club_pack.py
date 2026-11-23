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
        self.assertIn("subs_estimated", kinds)

    def test_reimport_is_a_no_op(self):
        before = self.conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0]
        db.init_db(self.path).close()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
