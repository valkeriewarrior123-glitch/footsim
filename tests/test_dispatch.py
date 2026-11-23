"""The request layer shared by the local server and the in-browser (Pyodide) build."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import db, dispatch

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
ED = "WC2022"


@unittest.skipUnless(REAL_DB.exists(), "run the importer first")
class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)

    def call(self, method, url, body=None):
        return dispatch.handle(method, url, body, self.tmp)

    def test_get_edition_and_branch_query(self):
        code, view = self.call("GET", f"/api/edition/{ED}")
        self.assertEqual((code, view["champion"]), (200, "ARG"))
        code, b = self.call("POST", "/api/branches", {"name": "web"})
        self.assertEqual(code, 200)
        code, view = self.call("GET", f"/api/edition/{ED}?branch={b['branch_id']}")
        self.assertEqual((code, view["branch_id"]), (200, b["branch_id"]))

    def test_errors_become_status_codes(self):
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        code, data = self.call("POST", f"/api/pick_podium/{ED}", {"first": "MAR", "second": "ENG"})
        self.assertEqual(code, 400)
        self.assertIn("error", data)
        self.assertEqual(self.call("POST", "/api/branches", {"name": ""})[0], 400)

    def test_edit_goes_through_like_the_server(self):
        _, b = self.call("POST", "/api/branches", {"name": "edit"})
        bid = b["branch_id"]
        _, view = self.call("GET", f"/api/edition/{ED}?branch={bid}")
        fid = next(f["fixture_id"] for g in view["groups"].values() for f in g["fixtures"]
                   if {f["home"], f["away"]} == {"QAT", "ECU"})
        code, r = self.call("POST", f"/api/roll_save/{ED}/{fid}?branch={bid}")
        self.assertEqual(code, 200)
        code, _ = self.call("DELETE", f"/api/fixture/{ED}/{fid}?branch={bid}")
        self.assertEqual(code, 200)

    def test_handle_json_round_trip(self):
        out = json.loads(dispatch.handle_json("GET", "/api/tiers"))
        self.assertEqual(out[0], 200)
        self.assertTrue(out[1]["tiers"])

    def test_start_adds_tables_and_clears_caches(self):
        conn = db.connect(self.tmp)
        conn.execute("DROP TABLE IF EXISTS suspension_log")
        conn.commit()
        conn.close()
        dispatch.start(self.tmp)
        conn = db.connect(self.tmp)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertIn("suspension_log", names)


if __name__ == "__main__":
    unittest.main()
