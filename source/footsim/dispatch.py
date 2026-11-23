"""Request dispatcher shared by the local server and the in-browser (Pyodide) build.

No HTTP here: `handle(method, url, body)` maps an /api/... request to an `api.py` call and
returns (status, data). `handle_json` is the string-in, string-out form used from JavaScript.
"""
import json
import re
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from footsim import api, db

ROUTES = [
    ("GET", r"/api/branches", lambda c, q, b: api.list_branches(c)),
    ("POST", r"/api/branches", lambda c, q, b: api.create_branch(c, b.get("name"), b.get("parent_branch_id"))),
    ("DELETE", r"/api/branches/(\d+)", lambda c, q, b, bid: api.delete_branch(c, int(bid))),
    ("GET", r"/api/editions", lambda c, q, b: api.editions(c)),
    ("GET", r"/api/edition/(\w+)", lambda c, q, b, ed: api.edition_view(c, ed, q)),
    ("GET", r"/api/stats/(\w+)", lambda c, q, b, ed: api.stats_view(c, ed, q)),
    ("GET", r"/api/world_diff", lambda c, q, b: api.world_diff(c, q)),
    ("GET", r"/api/history_diff/(\w+)", lambda c, q, b, ed: api.history_diff(c, ed, q)),
    ("GET", r"/api/displacements/(\w+)", lambda c, q, b, ed: api.displacements(c, ed, q) if q else []),
    ("GET", r"/api/fixture/(\w+)/([\w-]+)", lambda c, q, b, ed, f: api.fixture_detail(c, ed, f, q)),
    ("POST", r"/api/fixture/(\w+)/([\w-]+)", lambda c, q, b, ed, f: api.edit_fixture(c, ed, q, f, b)),
    ("DELETE", r"/api/fixture/(\w+)/([\w-]+)", lambda c, q, b, ed, f: api.revert_fixture(c, ed, q, f)),
    ("GET", r"/api/roll/(\w+)/([\w-]+)", lambda c, q, b, ed, f: api.roll_preview(c, ed, q, f)),
    ("POST", r"/api/roll/(\w+)/([\w-]+)", lambda c, q, b, ed, f: api.roll_constrained(c, ed, q, f, b)),
    ("POST", r"/api/roll_pending/(\w+)", lambda c, q, b, ed: api.roll_pending(c, ed, q)),
    ("POST", r"/api/roll_save/(\w+)/([\w-]+)", lambda c, q, b, ed, f: api.roll_save(c, ed, q, f)),
    ("GET", r"/api/tiers", lambda c, q, b: api.tiers()),
    ("POST", r"/api/mode", lambda c, q, b: api.set_mode(c, q, b.get("mode"))),
    ("POST", r"/api/pick_winner/(\w+)", lambda c, q, b, ed: api.pick_winner(c, ed, q, b.get("match_no"), b.get("team"))),
    ("POST", r"/api/pick_podium/(\w+)", lambda c, q, b, ed: api.pick_podium(c, ed, q, b.get("first"), b.get("second"),
                                                                             b.get("third") or None)),
    ("GET", r"/api/player/(\w+)", lambda c, q, b, ed: api.player_view(c, ed, q)),
    ("POST", r"/api/player/(\w+)", lambda c, q, b, ed: api.create_player(c, ed, q, b)),
    ("POST", r"/api/player_update", lambda c, q, b: api.update_player(c, q, b, "WC2022")),
    ("POST", r"/api/role/(\w+)/([\w-]+)", lambda c, q, b, ed, f: api.set_role(c, ed, q, f, b)),
    ("POST", r"/api/injury/(\w+)", lambda c, q, b, ed: api.add_injury(c, ed, q, b)),
    ("DELETE", r"/api/injury/(\d+)", lambda c, q, b, i: api.delete_injury(c, q, int(i))),
    ("POST", r"/api/career_event", lambda c, q, b: api.add_career_event(c, q, b)),
    ("DELETE", r"/api/career_event/(\d+)", lambda c, q, b, i: api.delete_career_event(c, q, int(i))),
    ("GET", r"/api/squad/(\w+)/(\w+)", lambda c, q, b, ed, t: api.squad(c, ed, t, q)),
    ("POST", r"/api/award/(\w+)", lambda c, q, b, ed: api.set_award(c, ed, q, b.get("award"), b.get("player_id"),
                                                                   int(b.get("rank", 1)))),
]


def handle(method: str, url: str, body=None, db_path: Path | None = None):
    u = urlparse(url)
    q = parse_qs(u.query).get("branch", [""])[0]
    branch = int(q) if q.isdigit() else None
    for m, pattern, fn in ROUTES:
        match = re.fullmatch(pattern, u.path)
        if m == method and match:
            conn = db.connect(db_path or db.DEFAULT_DB)
            try:
                return 200, fn(conn, branch, body or {}, *match.groups())
            except api.EditError as e:
                return 400, {"error": str(e)}
            except Exception as e:          # keep the editor alive; show the error
                traceback.print_exc()
                return 500, {"error": f"{type(e).__name__}: {e}"}
            finally:
                conn.close()
    return 404, {"error": "unknown endpoint"}


def handle_json(method: str, url: str, body_json: str = "") -> str:
    code, data = handle(method, url, json.loads(body_json) if body_json else {})
    return json.dumps([code, data], ensure_ascii=False)


def upgrade_branches(conn) -> None:
    """Bring saved branches up to the current rules (e.g. suspensions carrying forward)."""
    from footsim import engine
    editions = [r[0] for r in conn.execute("SELECT edition_id FROM rulesets")]
    for (b,) in conn.execute("SELECT branch_id FROM branches").fetchall():
        for ed in editions:
            try:
                engine.apply_cascade(conn, ed, b)
            except Exception as e:          # never block startup on one bad branch
                print(f"Couldn't update branch {b}: {e}")


def reset_caches() -> None:
    """Forget cached per-database lookups (call after swapping in a different footsim.db)."""
    from footsim import roller
    api._MIN_CACHE.clear()
    roller._INFO_CACHE.clear()


def start(db_path: Path | None = None) -> None:
    """Open the database, add any newer tables and bring saved branches up to the current rules."""
    reset_caches()
    conn = db.init_db(db_path or db.DEFAULT_DB)
    upgrade_branches(conn)
    conn.close()
