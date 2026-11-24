"""The created player: profile, squad swap, per-match roles, and the lineup rule.

The one rule everything follows: whoever takes a lineup spot takes that spot's
events. So when the player starts in place of someone who scored in real life,
the real result stands and the goal is now his. Edit or roll the match to change it.

Real (unedited) matches are reshaped at read time from the stored role, so
changing a role is instant and reversible. Edited or rolled matches store their
own lineup, built from the role when they were saved.
"""
from dataclasses import dataclass
from datetime import date

TIERS = {                       # tier -> default overall
    "squad_player": 65,
    "starter": 75,
    "star": 83,
    "world_class": 89,
    "generational": 94,
}
TIER_LABEL = {"squad_player": "Squad player", "starter": "Starter", "star": "Star",
              "world_class": "World-class", "generational": "Generational"}
ROLES = ("auto", "inherit", "start", "sub", "unused")


@dataclass
class Profile:
    player_id: str
    branch_id: int
    team_id: str
    edition_id: str
    replaces: str
    tier: str
    overall: int
    position: str
    name: str
    birth_date: str | None
    roles: tuple = ()


def check_birth_date(birth_date) -> None:
    """Age is ignored on purpose (a once-in-a-million talent): the date only has to be real."""
    from footsim.api import EditError
    try:
        date.fromisoformat(birth_date)
    except (TypeError, ValueError):
        raise EditError("Birth date must be YYYY-MM-DD")


def _root_branch_with_player(conn, branch_id):
    """The branch (this one or an ancestor) that owns the created player."""
    b = branch_id
    while b is not None:
        row = conn.execute("SELECT player_id FROM created_players WHERE branch_id = ?", (b,)).fetchone()
        if row:
            return b
        parent = conn.execute("SELECT parent_branch_id FROM branches WHERE branch_id = ?", (b,)).fetchone()
        b = parent[0] if parent else None
    return None


def profile(conn, branch_id: int | None, edition_id: str | None = None) -> Profile | None:
    """The branch's created player. With `edition_id`, the player as placed in that competition (his club or
    national team and who he replaces there), or None when he isn't in it. Without it, his first competition."""
    if branch_id is None:
        return None
    owner = _root_branch_with_player(conn, branch_id)
    if owner is None:
        return None
    r = conn.execute(
        "SELECT c.*, p.position, COALESCE(p.known_as, p.full_name) AS name, p.birth_date "
        "FROM created_players c JOIN players p USING(player_id) WHERE c.branch_id = ?", (owner,)).fetchone()
    roles = tuple(x for x in (r["roles"] or "").split(",") if x)
    team, ed, rep = r["team_id"], r["edition_id"], r["replaces_player_id"]
    if edition_id is not None and edition_id != ed:
        pl = conn.execute("SELECT * FROM player_placements WHERE branch_id = ? AND edition_id = ?",
                          (owner, edition_id)).fetchone()
        if pl is None:
            return None
        team, ed, rep = pl["team_id"], pl["edition_id"], pl["replaces_player_id"]
    return Profile(r["player_id"], branch_id, team, ed, rep,
                   r["tier"], r["overall"], r["position"], r["name"], r["birth_date"], roles)


def placements(conn, branch_id: int) -> list:
    """Every competition the branch's player is in: [{edition_id, team_id, replaces, home}] (home = first one)."""
    owner = _root_branch_with_player(conn, branch_id)
    if owner is None:
        return []
    r = conn.execute("SELECT edition_id, team_id, replaces_player_id FROM created_players WHERE branch_id = ?",
                     (owner,)).fetchone()
    out = [{"edition_id": r["edition_id"], "team_id": r["team_id"], "replaces": r["replaces_player_id"], "home": True}]
    for p in conn.execute("SELECT edition_id, team_id, replaces_player_id FROM player_placements "
                          "WHERE branch_id = ? ORDER BY rowid", (owner,)):
        out.append({"edition_id": p["edition_id"], "team_id": p["team_id"], "replaces": p["replaces_player_id"],
                    "home": False})
    return out


def _season_kind(conn, edition_id):
    r = conn.execute("SELECT e.season, c.kind FROM editions e JOIN competitions c USING(competition_id) "
                     "WHERE e.edition_id = ?", (edition_id,)).fetchone()
    return (r["season"], r["kind"]) if r else (None, None)


def place(conn, branch_id: int, edition_id: str, team_id: str, replaces_player_id: str) -> Profile:
    """Put the branch's player into another competition (e.g. his club's Champions League season).
    One club per season: club competitions of the same season must all use the same club."""
    from footsim.api import EditError
    p = profile(conn, branch_id)
    if p is None:
        raise EditError("This branch has no created player")
    owner = _root_branch_with_player(conn, branch_id)
    if owner != branch_id:
        raise EditError("This player was created in a parent branch; add competitions there (forks share him)")
    if any(x["edition_id"] == edition_id for x in placements(conn, branch_id)):
        raise EditError("He is already in this competition")
    ed = conn.execute("SELECT start_date FROM editions WHERE edition_id = ?", (edition_id,)).fetchone()
    if ed is None:
        raise EditError("Unknown competition")
    season, kind = _season_kind(conn, edition_id)
    tn = {r["team_id"]: r["name"] for r in conn.execute("SELECT team_id, name FROM teams")}
    if kind == "club":
        for x in placements(conn, branch_id):
            s2, k2 = _season_kind(conn, x["edition_id"])
            if k2 == "club" and s2 == season and x["team_id"] != team_id:
                raise EditError(f"He plays for {tn.get(x['team_id'])} in {season}, so he can't play for "
                                f"{tn.get(team_id, team_id)} in the same season")
    in_squad = conn.execute("SELECT shirt_number FROM squads WHERE edition_id = ? AND team_id = ? AND player_id = ?",
                            (edition_id, team_id, replaces_player_id)).fetchone()
    if in_squad is None:
        raise EditError("The player being replaced must be in that team's real squad")
    conn.execute("INSERT INTO player_placements (branch_id, edition_id, team_id, replaces_player_id, shirt_number) "
                 "VALUES (?,?,?,?,?)", (owner, edition_id, team_id, replaces_player_id, in_squad[0]))
    for (b,) in conn.execute("SELECT branch_id FROM branches WHERE created_player_id = ?", (p.player_id,)).fetchall():
        seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM squad_overrides WHERE branch_id = ?", (b,)).fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO squad_overrides VALUES (?,?,?,?, 'remove', ?)",
                     (b, edition_id, team_id, replaces_player_id, seq + 1))
        conn.execute("INSERT OR REPLACE INTO squad_overrides VALUES (?,?,?,?, 'add', ?)",
                     (b, edition_id, team_id, p.player_id, seq + 2))
    conn.execute("INSERT INTO player_career_events (branch_id, player_id, date, event_type, team_id, notes) "
                 "VALUES (?,?,?,?,?,?)", (owner, p.player_id, ed[0], "call_up", team_id,
                                          "Named in the squad in place of the real player"))
    conn.commit()
    return profile(conn, branch_id, edition_id)


def unplace(conn, branch_id: int, edition_id: str) -> None:
    """Take him out of a competition he was added to. His first competition can't be removed (delete the branch)."""
    from footsim.api import EditError
    p = profile(conn, branch_id, edition_id)
    owner = _root_branch_with_player(conn, branch_id)
    if p is None:
        raise EditError("He isn't in that competition")
    if owner != branch_id:
        raise EditError("This player was created in a parent branch; change competitions there")
    if profile(conn, branch_id).edition_id == edition_id:
        raise EditError("That is his first competition; it can't be removed")
    used = conn.execute("SELECT COUNT(*) FROM fixture_overrides WHERE edition_id = ? AND branch_id IN "
                        "(SELECT branch_id FROM branches WHERE created_player_id = ?)",
                        (edition_id, p.player_id)).fetchone()[0]
    if used:
        raise EditError("This competition has edited or rolled matches in it. Revert them first, then remove him.")
    conn.execute("DELETE FROM player_roles WHERE fixture_id IN (SELECT fixture_id FROM fixtures WHERE edition_id = ?) "
                 "AND branch_id IN (SELECT branch_id FROM branches WHERE created_player_id = ?)", (edition_id, p.player_id))
    conn.execute("DELETE FROM squad_overrides WHERE edition_id = ? AND team_id = ? AND player_id IN (?, ?) AND "
                 "branch_id IN (SELECT branch_id FROM branches WHERE created_player_id = ?)",
                 (edition_id, p.team_id, p.player_id, p.replaces, p.player_id))
    conn.execute("DELETE FROM player_placements WHERE branch_id = ? AND edition_id = ?", (owner, edition_id))
    conn.commit()


def create(conn, branch_id: int, *, full_name: str, known_as: str | None, birth_date: str, team_id: str,
           position: str, replaces_player_id: str, tier: str, overall: int | None = None,
           detail_position: str | None = None, edition_id: str = "WC2022", roles: list | None = None) -> Profile:
    from footsim.api import EditError
    if profile(conn, branch_id):
        raise EditError("This branch already has a created player. Make a new branch for another one.")
    if not full_name.strip():
        raise EditError("Give your player a name")
    if position not in ("GK", "DF", "MF", "FW"):
        raise EditError("Position must be GK, DF, MF or FW")
    if tier not in TIERS:
        raise EditError(f"Tier must be one of: {', '.join(TIERS)}")
    overall = TIERS[tier] if overall in (None, "") else int(overall)
    if not 1 <= overall <= 99:
        raise EditError("Overall rating must be 1 to 99")
    start = conn.execute("SELECT start_date FROM editions WHERE edition_id = ?", (edition_id,)).fetchone()[0]
    check_birth_date(birth_date)
    in_squad = conn.execute("SELECT shirt_number FROM squads WHERE edition_id = ? AND team_id = ? AND player_id = ?",
                            (edition_id, team_id, replaces_player_id)).fetchone()
    if in_squad is None:
        raise EditError("The player being replaced must be in that team's real squad")

    n = conn.execute("SELECT COUNT(*) FROM players WHERE is_created = 1").fetchone()[0] + 1
    pid = f"cp_{n}"
    while conn.execute("SELECT 1 FROM players WHERE player_id = ?", (pid,)).fetchone():
        n += 1
        pid = f"cp_{n}"
    nationality = conn.execute("SELECT name FROM teams WHERE team_id = ?", (team_id,)).fetchone()[0]
    conn.execute("INSERT INTO players VALUES (?,?,?,?,?,?,1)",
                 (pid, full_name.strip(), (known_as or "").strip() or None, birth_date, nationality, position))
    roles = _clean_roles(roles, position)
    conn.execute("INSERT INTO created_players (player_id, branch_id, team_id, edition_id, replaces_player_id, tier, "
                 "overall, detail_position, shirt_number, roles) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (pid, branch_id, team_id, edition_id, replaces_player_id, tier, overall, detail_position,
                  in_squad[0], ",".join(roles)))
    seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM squad_overrides WHERE branch_id = ?", (branch_id,)).fetchone()[0]
    conn.execute("INSERT INTO squad_overrides VALUES (?,?,?,?, 'remove', ?)",
                 (branch_id, edition_id, team_id, replaces_player_id, seq + 1))
    conn.execute("INSERT INTO squad_overrides VALUES (?,?,?,?, 'add', ?)",
                 (branch_id, edition_id, team_id, pid, seq + 2))
    conn.execute("UPDATE branches SET created_player_id = ? WHERE branch_id = ?", (pid, branch_id))
    conn.execute("INSERT INTO player_career_events (branch_id, player_id, date, event_type, team_id, notes) "
                 "VALUES (?,?,?,?,?,?)", (branch_id, pid, start, "call_up", team_id,
                                          f"Named in the squad in place of the real player"))
    conn.commit()
    return profile(conn, branch_id)


def _clean_roles(roles, position):
    from footsim.api import EditError
    from footsim.db import ROLE_GROUP, ROLE_ORDER
    roles = [r for r in (roles or []) if r]
    bad = [r for r in roles if r not in ROLE_GROUP]
    if bad:
        raise EditError(f"Unknown role(s): {', '.join(bad)}")
    if position == "GK" and any(r != "GK" for r in roles):
        raise EditError("A goalkeeper's only role is GK")
    if position and position != "GK" and "GK" in roles:
        raise EditError("Only goalkeepers can have the GK role")
    if not roles:
        roles = {"GK": ["GK"], "DF": ["CB"], "MF": ["CM"], "FW": ["ST"]}.get(position, [])
    return sorted(dict.fromkeys(roles), key=ROLE_ORDER.index)


def update(conn, branch_id: int, *, full_name=None, known_as=None, birth_date=None, position=None,
           detail_position=None, tier=None, overall=None, replaces_player_id=None, roles=None,
           edition_id=None) -> Profile:
    """Edit the created player. Everything except his national team can change at any time."""
    from footsim.api import EditError
    p = profile(conn, branch_id, edition_id)
    if p is None:
        p = profile(conn, branch_id)
        replaces_player_id = None            # he isn't in that competition: only the shared details can change
    owner = _root_branch_with_player(conn, branch_id)
    if owner != branch_id:
        raise EditError("This player was created in a parent branch; edit him there (forks share him)")
    if full_name is not None:
        if not full_name.strip():
            raise EditError("Give your player a name")
        conn.execute("UPDATE players SET full_name = ? WHERE player_id = ?", (full_name.strip(), p.player_id))
    if known_as is not None:
        conn.execute("UPDATE players SET known_as = ? WHERE player_id = ?", (known_as.strip() or None, p.player_id))
    if birth_date is not None:
        check_birth_date(birth_date)
        conn.execute("UPDATE players SET birth_date = ? WHERE player_id = ?", (birth_date, p.player_id))
    if position is not None:
        if position not in ("GK", "DF", "MF", "FW"):
            raise EditError("Position must be GK, DF, MF or FW")
        conn.execute("UPDATE players SET position = ? WHERE player_id = ?", (position, p.player_id))
    if roles is not None:
        cur_pos = position or p.position
        conn.execute("UPDATE created_players SET roles = ? WHERE branch_id = ?",
                     (",".join(_clean_roles(roles, cur_pos)), owner))
    if detail_position is not None:
        conn.execute("UPDATE created_players SET detail_position = ? WHERE branch_id = ?",
                     (detail_position.strip() or None, owner))
    if tier:
        if tier not in TIERS:
            raise EditError(f"Tier must be one of: {', '.join(TIERS)}")
        conn.execute("UPDATE created_players SET tier = ? WHERE branch_id = ?", (tier, owner))
    if overall not in (None, ""):
        if not 1 <= int(overall) <= 99:
            raise EditError("Overall rating must be 1 to 99")
        conn.execute("UPDATE created_players SET overall = ? WHERE branch_id = ?", (int(overall), owner))
    if replaces_player_id and replaces_player_id != p.replaces:
        ok = conn.execute("SELECT 1 FROM squads WHERE edition_id = ? AND team_id = ? AND player_id = ?",
                          (p.edition_id, p.team_id, replaces_player_id)).fetchone()
        if not ok:
            raise EditError("The replaced player must be in his team's real squad")
        # the old one comes back into the squad; the new one leaves
        conn.execute("DELETE FROM squad_overrides WHERE edition_id = ? AND team_id = ? AND player_id = ? "
                     "AND action = 'remove' AND branch_id IN (SELECT branch_id FROM branches WHERE "
                     "created_player_id = ?)", (p.edition_id, p.team_id, p.replaces, p.player_id))
        for (b,) in conn.execute("SELECT branch_id FROM branches WHERE created_player_id = ?",
                                 (p.player_id,)).fetchall():
            seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM squad_overrides WHERE branch_id = ?", (b,)).fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO squad_overrides VALUES (?,?,?,?, 'remove', ?)",
                         (b, p.edition_id, p.team_id, replaces_player_id, seq + 1))
        if p.edition_id == profile(conn, branch_id).edition_id:
            conn.execute("UPDATE created_players SET replaces_player_id = ? WHERE branch_id = ?",
                         (replaces_player_id, owner))
        else:
            conn.execute("UPDATE player_placements SET replaces_player_id = ? WHERE branch_id = ? AND edition_id = ?",
                         (replaces_player_id, owner, p.edition_id))
        conn.execute("DELETE FROM player_roles WHERE target_player_id = ?", (replaces_player_id,))
    conn.commit()
    return profile(conn, branch_id)


# ------------------------------------------------------------------ ability

def impact_elo(p: Profile) -> float:
    """How much stronger the team is with him on the pitch for 90 minutes."""
    base = {"FW": 3.0, "MF": 3.0, "DF": 2.4, "GK": 2.6}[p.position]
    return max(-40.0, (p.overall - 75) * base)


def scoring_info(p: Profile) -> dict:
    """Same shape as roller._player_info entries, derived from the rating."""
    x = max(0, p.overall - 60)
    g = {"FW": 1.0, "MF": 0.45, "DF": 0.15, "GK": 0.0}[p.position]
    a = {"FW": 1.0, "MF": 1.3, "DF": 0.6, "GK": 0.05}[p.position]
    return {"pos": p.position, "roles": tuple(p.roles), "g90": round((0.10 + 0.025 * x) * g, 3),
            "a90": round((0.05 + 0.012 * x) * a, 3),
            "pens": 3 if p.overall >= 85 and p.position in ("FW", "MF") else 0}


# ------------------------------------------------------------------ roles

def get_role(conn, branch_id: int, fixture_id: str) -> dict:
    r = conn.execute("SELECT role, target_player_id, minute FROM player_roles WHERE branch_id = ? AND fixture_id = ?",
                     (branch_id, fixture_id)).fetchone()
    return dict(r) if r else {"role": "auto", "target_player_id": None, "minute": None}


def injured_on(conn, branch_id: int, player_id: str, day: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM injuries WHERE branch_id = ? AND player_id = ? AND start_date <= ? "
        "AND (end_date IS NULL OR end_date >= ?)", (branch_id, player_id, day, day)).fetchone() is not None


def set_role(conn, branch_id: int, fixture_id: str, role: str, target: str | None = None,
             minute: int | None = None) -> None:
    from footsim.api import EditError
    if role not in ROLES:
        raise EditError(f"Role must be one of {ROLES}")
    fe = conn.execute("SELECT edition_id FROM fixtures WHERE fixture_id = ?", (fixture_id,)).fetchone()
    p = profile(conn, branch_id, fe[0] if fe else None)
    if p is None:
        raise EditError("This branch has no created player in that competition")
    if role == "sub" and (minute is None or not 1 <= int(minute) <= 119):
        raise EditError("Pick the minute he comes on (1-119)")
    from footsim import timeline
    fx = timeline.active_fixtures(conn, p.edition_id, branch_id, raw=True)
    f = fx.get(fixture_id)
    if f is None or p.team_id not in (f.home, f.away):
        raise EditError("That match doesn't involve your player's team")
    if role in ("start", "sub", "inherit") and injured_on(conn, branch_id, p.player_id, f.date):
        raise EditError("Your player is injured on that date")
    conn.execute("INSERT OR REPLACE INTO player_roles VALUES (?,?,?,?,?)",
                 (branch_id, fixture_id, role, target, None if minute is None else int(minute)))
    conn.commit()


# ------------------------------------------------------------------ the lineup rule

def _bench_pick(lineup, squad_ids, info, position, exclude):
    in_lineup = {l["player_id"] for l in lineup}
    cands = [p for p in squad_ids if p not in in_lineup and p not in exclude]
    same = [p for p in cands if info.get(p, {}).get("pos") == position]
    pool = same or [p for p in cands if info.get(p, {}).get("pos") != "GK"] or cands
    return max(pool, key=lambda p: (info.get(p, {}).get("g90", 0), p)) if pool else None


def _take_slot(lineup, events, target, newp, from_minute=None):
    """newp takes target's place (all of it, or from a minute on) and that slot's events."""
    entry = next((l for l in lineup if l["player_id"] == target), None)
    if entry is None:
        return
    if from_minute is None:
        entry["player_id"] = newp
        for e in events:
            if e.get("player_id") == target:
                e["player_id"] = newp
            if e.get("assist_player_id") == target:
                e["assist_player_id"] = newp
    else:
        orig_off = entry["minute_off"]
        entry["minute_off"] = from_minute
        lineup.append({"team_id": entry["team_id"], "player_id": newp, "started": 0,
                       "minute_on": from_minute, "minute_off": orig_off})
        for e in events:
            if e["minute"] >= from_minute:
                if e.get("player_id") == target:
                    e["player_id"] = newp
                if e.get("assist_player_id") == target:
                    e["assist_player_id"] = newp
    # never assist yourself
    for e in events:
        if e.get("assist_player_id") and e.get("assist_player_id") == e.get("player_id"):
            e["assist_player_id"] = None


def _on_pitch(l, minute):
    on = 0 if l["started"] else (l["minute_on"] or 0)
    return on <= minute and (l["minute_off"] is None or l["minute_off"] > minute)


def auto_target(lineup, team, me_pos, info, exclude=(), me_roles=()):
    """Who the created player replaces by default: a starter who plays one of his roles,
    else one in his broad position; among those, the least productive."""
    starters = [l["player_id"] for l in lineup if l["team_id"] == team and l["started"] and l["player_id"] not in exclude]
    by_role = [p for p in starters if set(info.get(p, {}).get("roles", ())) & set(me_roles)]
    same = [p for p in starters if info.get(p, {}).get("pos") == me_pos]
    pool = by_role or same or [p for p in starters if info.get(p, {}).get("pos") != "GK"]
    def key(p):
        r = info.get(p, {}).get("roles", ())
        main_match = bool(r) and r[0] in me_roles          # his main role is one of mine
        return (not main_match, info.get(p, {}).get("g90", 0), p)
    return min(pool, key=key) if pool else None


def apply_role(lineup: list, events: list, p: Profile, role: dict, squad_ids: list, info: dict) -> tuple:
    """Return (lineup, events) with the created player placed by his role."""
    lineup = [dict(l) for l in lineup]
    events = [dict(e) for e in events]
    me, R, team = p.player_id, p.replaces, p.team_id
    if not any(l["team_id"] == team for l in lineup):
        return lineup, events
    in_lineup = {l["player_id"] for l in lineup}
    kind = role["role"]
    if kind == "auto":
        kind = "inherit" if R in in_lineup else "unused"
    if kind == "inherit":
        if R in in_lineup:
            _take_slot(lineup, events, R, me)
            return lineup, events
        kind = "unused"
    # the replaced player has left the squad: someone from the bench fills his spot
    filler = None
    if R in in_lineup:
        filler = _bench_pick(lineup, squad_ids, info, info.get(R, {}).get("pos", "MF"), {me, R})
        if filler:
            _take_slot(lineup, events, R, filler)
        else:
            lineup = [l for l in lineup if l["player_id"] != R]
    target = role.get("target_player_id")
    if target == R:
        target = filler
    if kind == "start":
        valid = {l["player_id"] for l in lineup if l["team_id"] == team and l["started"]}
        if target not in valid:
            target = auto_target(lineup, team, p.position, info, exclude={me}, me_roles=p.roles)
        if target:
            _take_slot(lineup, events, target, me)
    elif kind == "sub":
        minute = role.get("minute") or 60
        on = {l["player_id"] for l in lineup if l["team_id"] == team and _on_pitch(l, minute)}
        if target not in on:
            target = auto_target([l for l in lineup if l["player_id"] in on], team, p.position, info,
                                 exclude={me}, me_roles=p.roles) or \
                next(iter(sorted(on)), None)
        if target:
            _take_slot(lineup, events, target, me, from_minute=minute)
    return lineup, events


def strip(lineup: list, events: list, p: Profile, squad_ids: list, info: dict) -> tuple:
    """Take the created player out of a lineup (a bench player takes his spot and events)."""
    if not any(l["player_id"] == p.player_id for l in lineup):
        return lineup, events
    lineup = [dict(l) for l in lineup]
    events = [dict(e) for e in events]
    filler = _bench_pick(lineup, squad_ids, info, p.position, {p.player_id, p.replaces})
    if filler:
        _take_slot(lineup, events, p.player_id, filler)
    return lineup, events


def transform_fixtures(conn, edition_id: str, branch_id: int, fixtures: dict) -> None:
    """Called by timeline.active_fixtures: place the player in real-result matches."""
    p = profile(conn, branch_id, edition_id)
    if p is None:
        return
    from footsim.api import squad
    from footsim.roller import _player_info
    squad_ids = [s["player_id"] for s in squad(conn, edition_id, p.team_id, branch_id)]
    info = _player_info(conn, edition_id)
    info[p.player_id] = scoring_info(p)
    for f in fixtures.values():
        if p.team_id not in (f.home, f.away) or not f.lineup:
            continue
        has_me = any(l["player_id"] == p.player_id for l in f.lineup)
        if has_me and f.origin not in ("baseline", "reused_historical"):
            continue                      # edited/rolled: its saved lineup already reflects the role
        role = get_role(conn, branch_id, f.fixture_id)
        if injured_on(conn, branch_id, p.player_id, f.date):
            role = {"role": "unused", "target_player_id": None, "minute": None}
        f.lineup, f.events = apply_role(f.lineup, f.events, p, role, squad_ids, info)
