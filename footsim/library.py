"""The player library: reusable player profiles that any branch can use.

A profile holds who the player is (name, born, position, roles, tier, overall). A branch's created
player is a copy of a profile (`created_players.profile_id` remembers which one), so editing a profile
never silently changes a branch: after an edit the app lists the branches that now differ and the user
picks which of them to update.
"""
import re

from footsim import player
from footsim.db import ROLE_GROUP, ROLE_ORDER

FIELDS = ("full_name", "known_as", "birth_date", "position", "roles", "detail_position", "tier", "overall")


def _err(msg):
    from footsim.api import EditError
    return EditError(msg)


def _clean(body: dict, partial: bool = False) -> dict:
    """Validate profile fields. With partial=True only the fields given are checked and returned."""
    out = {}
    if not partial or "full_name" in body:
        name = (body.get("full_name") or "").strip()
        if not name:
            raise _err("Give your player a name")
        out["full_name"] = name
    if "known_as" in body or not partial:
        out["known_as"] = (body.get("known_as") or "").strip() or None
    if "birth_date" in body or not partial:
        bd = body.get("birth_date") or None
        if bd:
            player.check_birth_date(bd)
        out["birth_date"] = bd
    if not partial or "position" in body:
        if body.get("position") not in ("GK", "DF", "MF", "FW"):
            raise _err("Position must be GK, DF, MF or FW")
        out["position"] = body["position"]
    if "detail_position" in body or not partial:
        out["detail_position"] = (body.get("detail_position") or "").strip() or None
    if not partial or "tier" in body:
        if body.get("tier") not in player.TIERS:
            raise _err(f"Tier must be one of: {', '.join(player.TIERS)}")
        out["tier"] = body["tier"]
    if "overall" in body or not partial:
        ov = body.get("overall")
        ov = player.TIERS[body["tier"]] if ov in (None, "") and body.get("tier") in player.TIERS else ov
        try:
            ov = int(ov)
        except (TypeError, ValueError):
            raise _err("Overall rating must be 1 to 99")
        if not 1 <= ov <= 99:
            raise _err("Overall rating must be 1 to 99")
        out["overall"] = ov
    if "roles" in body or not partial:
        out["roles"] = body.get("roles") or []
    return out


def _roles_text(roles, position) -> str:
    return ",".join(player._clean_roles(roles if isinstance(roles, list) else
                                        [r for r in (roles or "").split(",") if r], position))


def _row(conn, profile_id: int):
    r = conn.execute("SELECT * FROM player_profiles WHERE profile_id = ?", (profile_id,)).fetchone()
    if r is None:
        raise _err("That profile doesn't exist")
    return r


def _same(profile_row, created_row) -> list:
    """Names of the profile fields that differ from a branch's player."""
    diff = []
    for k in ("full_name", "known_as", "birth_date", "position"):
        if (profile_row[k] or None) != (created_row[k] or None):
            diff.append(k)
    for k in ("tier", "overall", "detail_position", "roles"):
        if (profile_row[k] or None) != (created_row[k] or None):
            diff.append(k)
    return diff


def _users(conn, profile_id: int) -> list:
    """Branches (owners of a created player) whose player is linked to this profile."""
    prof = _row(conn, profile_id)
    rows = conn.execute(
        "SELECT c.branch_id, b.name AS branch_name, c.team_id, c.edition_id, c.tier, c.overall, "
        "c.detail_position, c.roles, p.full_name, p.known_as, p.birth_date, p.position, t.name AS team "
        "FROM created_players c JOIN players p USING(player_id) JOIN branches b ON b.branch_id = c.branch_id "
        "JOIN teams t ON t.team_id = c.team_id WHERE c.profile_id = ? ORDER BY c.branch_id", (profile_id,)).fetchall()
    return [{"branch_id": r["branch_id"], "branch": r["branch_name"], "team": r["team"], "edition_id": r["edition_id"],
             "differs": _same(prof, r)} for r in rows]


def _public(conn, r) -> dict:
    return {"profile_id": r["profile_id"], "full_name": r["full_name"], "known_as": r["known_as"],
            "birth_date": r["birth_date"], "position": r["position"],
            "roles": [x for x in (r["roles"] or "").split(",") if x], "detail_position": r["detail_position"],
            "tier": r["tier"], "tier_label": player.TIER_LABEL[r["tier"]], "overall": r["overall"],
            "used_in": _users(conn, r["profile_id"]), "career": career(conn, r["profile_id"])}


def list_profiles(conn) -> list:
    return [_public(conn, r) for r in
            conn.execute("SELECT * FROM player_profiles ORDER BY COALESCE(known_as, full_name) COLLATE NOCASE")]


def create_profile(conn, body: dict) -> dict:
    v = _clean(body)
    v["roles"] = _roles_text(v["roles"], v["position"])
    cur = conn.execute(
        "INSERT INTO player_profiles (full_name, known_as, birth_date, position, roles, detail_position, tier, overall) "
        "VALUES (?,?,?,?,?,?,?,?)", tuple(v[k] for k in FIELDS))
    conn.commit()
    return {"ok": True, "profile": _public(conn, _row(conn, cur.lastrowid))}


def update_profile(conn, profile_id: int, body: dict) -> dict:
    """Save the profile only. The answer lists the branches that now differ so the user can choose which to update."""
    cur = _row(conn, profile_id)
    v = _clean(body, partial=True)
    position = v.get("position", cur["position"])
    if "roles" in v or "position" in v:
        v["roles"] = _roles_text(v["roles"] if "roles" in v else cur["roles"], position)
    if v:
        sets = ", ".join(f"{k} = ?" for k in v)
        conn.execute(f"UPDATE player_profiles SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE profile_id = ?",
                     (*v.values(), profile_id))
        conn.commit()
    prof = _public(conn, _row(conn, profile_id))
    return {"ok": True, "profile": prof, "differing": [u for u in prof["used_in"] if u["differs"]]}


def delete_profile(conn, profile_id: int) -> dict:
    """Remove a profile from the library. Branches that used it keep their player (just no longer linked)."""
    _row(conn, profile_id)
    conn.execute("UPDATE created_players SET profile_id = NULL WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM profile_career WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM player_profiles WHERE profile_id = ?", (profile_id,))
    conn.commit()
    return {"ok": True}


def apply_to_branches(conn, profile_id: int, branch_ids: list) -> dict:
    """Copy the profile's current values onto the chosen branches' players, then re-run each branch."""
    from footsim import engine
    prof = _row(conn, profile_id)
    linked = {u["branch_id"]: u for u in _users(conn, profile_id)}
    done = []
    for b in dict.fromkeys(int(x) for x in branch_ids or []):
        if b not in linked:
            raise _err("That branch isn't using this profile")
        ed = conn.execute("SELECT edition_id FROM created_players WHERE branch_id = ?", (b,)).fetchone()[0]
        player.update(conn, b, full_name=prof["full_name"], known_as=prof["known_as"] or "",
                      birth_date=prof["birth_date"], position=prof["position"],
                      roles=[x for x in (prof["roles"] or "").split(",") if x],
                      detail_position=prof["detail_position"] or "", tier=prof["tier"], overall=prof["overall"])
        engine.apply_cascade(conn, ed, b)
        done.append(b)
    return {"ok": True, "updated": done}


def save_branch_player(conn, branch_id: int, name: str | None = None) -> dict:
    """Put a branch's current created player into the library and link the two."""
    if branch_id is None:
        raise _err("Pick a branch first")
    me = player.profile(conn, branch_id)
    if me is None:
        raise _err("This branch has no created player")
    owner = player._root_branch_with_player(conn, branch_id)
    if owner != branch_id:
        raise _err("This player was created in a parent branch; save him from there")
    c = conn.execute("SELECT * FROM created_players WHERE branch_id = ?", (owner,)).fetchone()
    if c["profile_id"]:
        raise _err("This player is already linked to a library profile")
    p = conn.execute("SELECT * FROM players WHERE player_id = ?", (me.player_id,)).fetchone()
    out = create_profile(conn, {"full_name": name or p["full_name"], "known_as": p["known_as"],
                                "birth_date": p["birth_date"], "position": p["position"],
                                "roles": list(me.roles), "detail_position": c["detail_position"],
                                "tier": c["tier"], "overall": c["overall"]})
    conn.execute("UPDATE created_players SET profile_id = ? WHERE branch_id = ?",
                 (out["profile"]["profile_id"], owner))
    conn.commit()
    return {"ok": True, "profile": _public(conn, _row(conn, out["profile"]["profile_id"]))}


def unlink_branch_player(conn, branch_id: int) -> dict:
    conn.execute("UPDATE created_players SET profile_id = NULL WHERE branch_id = ?", (branch_id,))
    conn.commit()
    return {"ok": True}


def branch_link(conn, branch_id: int | None) -> dict | None:
    """What the My player tab shows: is this branch's player linked to a library profile, and does it match?"""
    if branch_id is None:
        return None
    owner = player._root_branch_with_player(conn, branch_id)
    if owner is None:
        return None
    pid = conn.execute("SELECT profile_id FROM created_players WHERE branch_id = ?", (owner,)).fetchone()[0]
    if not pid or not conn.execute("SELECT 1 FROM player_profiles WHERE profile_id = ?", (pid,)).fetchone():
        return {"profile_id": None, "can_save": owner == branch_id}
    u = next((x for x in _users(conn, pid) if x["branch_id"] == owner), None)
    return {"profile_id": pid, "name": _row(conn, pid)["known_as"] or _row(conn, pid)["full_name"],
            "differs": u["differs"] if u else [], "can_save": False}


# ------------------------------------------------------------------ career history (clubs by season)

def _start_year(season: str) -> int | None:
    m = re.match(r"\s*(\d{4})", season or "")
    return int(m.group(1)) if m else None


def _season_label(season: str) -> str:
    """'2014-15', '2014/15' or '2014' -> '2014/15' for a club season, kept as '2022' when written with one year."""
    m = re.fullmatch(r"\s*(\d{4})\s*(?:[/-]\s*(\d{2}|\d{4}))?\s*", season or "")
    if not m:
        raise _err("Write the season like 2014/15")
    y = int(m.group(1))
    if m.group(2):
        end = int(m.group(2)[-2:])
        if end != (y + 1) % 100:
            raise _err("A club season runs over two years in a row, like 2014/15")
        return f"{y}/{end:02d}"
    return str(y)


def career(conn, profile_id: int) -> list:
    rows = conn.execute("SELECT c.*, t.name AS team_name FROM profile_career c LEFT JOIN teams t USING(team_id) "
                        "WHERE c.profile_id = ?", (profile_id,)).fetchall()
    out = [{"entry_id": r["entry_id"], "season": r["season"], "club": r["club"], "team_id": r["team_id"],
            "in_data": r["team_id"] is not None, "note": r["note"]} for r in rows]
    return sorted(out, key=lambda e: (_start_year(e["season"]) or 0, e["entry_id"]))


def career_add(conn, profile_id: int, body: dict) -> dict:
    """One club per season: adding a season that already has a club replaces it."""
    _row(conn, profile_id)
    club = (body.get("club") or "").strip()
    if not club:
        raise _err("Write the club's name")
    season = _season_label(body.get("season"))
    team_id = body.get("team_id") or None
    if team_id and not conn.execute("SELECT 1 FROM teams WHERE team_id = ?", (team_id,)).fetchone():
        raise _err("Unknown club")
    if not team_id:                                   # the club is in our data when its name matches
        r = conn.execute("SELECT team_id FROM teams WHERE name = ? COLLATE NOCASE", (club,)).fetchone()
        team_id = r[0] if r else None
    conn.execute("DELETE FROM profile_career WHERE profile_id = ? AND season = ?", (profile_id, season))
    cur = conn.execute("INSERT INTO profile_career (profile_id, season, club, team_id, note) VALUES (?,?,?,?,?)",
                       (profile_id, season, club, team_id, (body.get("note") or "").strip() or None))
    conn.execute("UPDATE player_profiles SET updated_at = CURRENT_TIMESTAMP WHERE profile_id = ?", (profile_id,))
    conn.commit()
    return {"ok": True, "entry_id": cur.lastrowid, "career": career(conn, profile_id)}


def career_delete(conn, entry_id: int) -> dict:
    r = conn.execute("SELECT profile_id FROM profile_career WHERE entry_id = ?", (entry_id,)).fetchone()
    conn.execute("DELETE FROM profile_career WHERE entry_id = ?", (entry_id,))
    conn.commit()
    return {"ok": True, "career": career(conn, r[0]) if r else []}


def season_suggestion(conn, profile_id, edition_id: str) -> dict | None:
    """The career entry whose season matches this edition's season, if any: {club, team_id, season}."""
    if not profile_id:
        return None
    ed = conn.execute("SELECT season FROM editions WHERE edition_id = ?", (edition_id,)).fetchone()
    y = _start_year(ed[0]) if ed else None
    for e in career(conn, profile_id):
        if y is not None and _start_year(e["season"]) == y:
            return e
    return None


def note_season(conn, branch_id: int, edition_id: str, team_id: str) -> None:
    """When a branch's player joins a club for a season, record it in his library career if that season is empty."""
    owner = player._root_branch_with_player(conn, branch_id)
    if owner is None:
        return
    pid = conn.execute("SELECT profile_id FROM created_players WHERE branch_id = ?", (owner,)).fetchone()[0]
    r = conn.execute("SELECT e.season, c.kind FROM editions e JOIN competitions c USING(competition_id) "
                     "WHERE e.edition_id = ?", (edition_id,)).fetchone()
    if not pid or r is None or r["kind"] != "club" or season_suggestion(conn, pid, edition_id):
        return
    y = _start_year(r["season"])
    club = conn.execute("SELECT name FROM teams WHERE team_id = ?", (team_id,)).fetchone()[0]
    career_add(conn, pid, {"season": f"{y}/{(y + 1) % 100:02d}", "club": club, "team_id": team_id})
