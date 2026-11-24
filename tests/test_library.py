import json
import shutil
import tempfile
import unittest
from pathlib import Path

from footsim import api, db, dispatch, library, player

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "footsim.db"
ED = "WC2022"
PROFILE = {"full_name": "Mateo Rivas", "known_as": "Rivas", "birth_date": "2001-03-14", "position": "FW",
           "roles": ["LW", "ST"], "tier": "world_class", "overall": 91}


@unittest.skipUnless(REAL_DB.exists(), "run the importer and ratings loader first")
class Library(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        shutil.copy(REAL_DB, self.tmp)
        self.conn = db.init_db(self.tmp)
        self.pid = library.create_profile(self.conn, PROFILE)["profile"]["profile_id"]

    def tearDown(self):
        self.conn.close()

    def use(self, name, team="ARG", pos_name="Dybala", **extra):
        b = api.create_branch(self.conn, name)["branch_id"]
        rep = next(p for p in api.squad(self.conn, ED, team) if pos_name in p["name"])["player_id"]
        api.create_player(self.conn, ED, b, {**PROFILE, "team_id": team, "replaces_player_id": rep,
                                             "profile_id": self.pid, **extra})
        return b

    def test_profile_roundtrip_and_validation(self):
        got = library.list_profiles(self.conn)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["roles"], ["LW", "ST"])
        self.assertEqual(got[0]["used_in"], [])
        for bad in ({"full_name": " "}, {"position": "XX"}, {"tier": "god"}, {"overall": 120},
                    {"birth_date": "nope"}, {"roles": ["GK"]}):
            with self.assertRaises(api.EditError, msg=bad):
                library.create_profile(self.conn, {**PROFILE, **bad})

    def test_age_is_ignored(self):
        library.create_profile(self.conn, {**PROFILE, "birth_date": "2015-01-01"})   # 7 years old: allowed
        b = self.use("kid", birth_date="2015-01-01")
        self.assertEqual(player.profile(self.conn, b).birth_date, "2015-01-01")

    def test_same_profile_in_many_branches(self):
        a, b = self.use("a"), self.use("b", team="FRA", pos_name="Giroud")
        users = library.list_profiles(self.conn)[0]["used_in"]
        self.assertEqual({u["branch_id"] for u in users}, {a, b})
        self.assertTrue(all(u["differs"] == [] for u in users))
        self.assertNotEqual(player.profile(self.conn, a).player_id, player.profile(self.conn, b).player_id)

    def test_edit_asks_which_branches_to_update(self):
        a, b = self.use("a"), self.use("b", team="FRA", pos_name="Giroud")
        r = library.update_profile(self.conn, self.pid, {"overall": 95, "roles": ["ST"]})
        self.assertEqual({u["branch_id"] for u in r["differing"]}, {a, b})
        self.assertEqual(player.profile(self.conn, a).overall, 91)              # nothing changed yet
        res = library.apply_to_branches(self.conn, self.pid, [a])               # only branch a
        self.assertEqual(res["updated"], [a])
        self.assertEqual(player.profile(self.conn, a).overall, 95)
        self.assertEqual(player.profile(self.conn, a).roles, ("ST",))
        self.assertEqual(player.profile(self.conn, b).overall, 91)
        left = library.list_profiles(self.conn)[0]["used_in"]
        self.assertEqual([u["branch_id"] for u in left if u["differs"]], [b])

    def test_apply_rejects_unrelated_branch(self):
        other = api.create_branch(self.conn, "empty")["branch_id"]
        with self.assertRaises(api.EditError):
            library.apply_to_branches(self.conn, self.pid, [other])

    def test_save_existing_branch_player(self):
        b = api.create_branch(self.conn, "mine")["branch_id"]
        dyb = next(p for p in api.squad(self.conn, ED, "ARG") if "Dybala" in p["name"])["player_id"]
        api.create_player(self.conn, ED, b, {**PROFILE, "full_name": "Old Hero", "team_id": "ARG",
                                             "replaces_player_id": dyb})
        self.assertTrue(api.player_view(self.conn, ED, b)["library"]["can_save"])
        out = library.save_branch_player(self.conn, b)
        self.assertEqual(out["profile"]["full_name"], "Old Hero")
        link = api.player_view(self.conn, ED, b)["library"]
        self.assertEqual(link["profile_id"], out["profile"]["profile_id"])
        self.assertEqual(link["differs"], [])
        with self.assertRaises(api.EditError):
            library.save_branch_player(self.conn, b)                             # already linked

    def test_delete_profile_keeps_branch_players(self):
        b = self.use("a")
        library.delete_profile(self.conn, self.pid)
        self.assertEqual(library.list_profiles(self.conn), [])
        self.assertIsNotNone(player.profile(self.conn, b))
        self.assertIsNone(api.player_view(self.conn, ED, b)["library"]["profile_id"])

    def test_branch_with_player_can_be_deleted(self):
        b = self.use("a")
        api.delete_branch(self.conn, b)
        self.assertEqual(library.list_profiles(self.conn)[0]["used_in"], [])

    def test_http_routes(self):
        code, data = dispatch.handle("POST", "/api/library", PROFILE, self.tmp)
        self.assertEqual(code, 200)
        pid = data["profile"]["profile_id"]
        code, data = dispatch.handle("POST", f"/api/library/{pid}", {"overall": 80}, self.tmp)
        self.assertEqual((code, data["profile"]["overall"]), (200, 80))
        code, data = dispatch.handle("GET", "/api/library", None, self.tmp)
        self.assertEqual(len(data), 2)
        code, data = dispatch.handle("POST", "/api/library", {**PROFILE, "position": "?"}, self.tmp)
        self.assertEqual(code, 400)
        self.assertEqual(dispatch.handle("DELETE", f"/api/library/{pid}", None, self.tmp)[0], 200)


if __name__ == "__main__":
    unittest.main()
