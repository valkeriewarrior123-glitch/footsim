"""Editor operations as plain functions returning JSON-ready dicts.

server.py exposes these over HTTP; tests call them directly.
"""
from footsim import engine, player, season_stats, stats, timeline
from footsim.timeline import Fixture

GOAL_TYPES = ("goal", "pen_goal")
EVENT_TYPES = ("goal", "pen_goal", "own_goal", "pen_miss", "yellow", "second_yellow", "red",
               "shootout_score", "shootout_miss")


class EditError(ValueError):
    pass


# ------------------------------------------------------------------ lookups

def names(conn) -> dict:
    return dict(conn.execute("SELECT player_id, COALESCE(known_as, full_name) FROM players"))


def team_names(conn) -> dict:
    return dict(conn.execute("SELECT team_id, name FROM teams"))


def list_branches(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT b.branch_id, b.name, b.parent_branch_id, b.created_at, "
        "(SELECT COUNT(*) FROM fixture_overrides o WHERE o.branch_id = b.branch_id AND o.origin = 'user_edit') AS edits "
        "FROM branches b ORDER BY b.branch_id")]


def create_branch(conn, name: str, parent_branch_id: int | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise EditError("Give the branch a name")
    bid = timeline.create_branch(conn, name, parent_branch_id)
    return {"branch_id": bid, "name": name, "parent_branch_id": parent_branch_id}


def delete_branch(conn, branch_id: int) -> dict:
    kids = conn.execute("SELECT COUNT(*) FROM branches WHERE parent_branch_id = ?", (branch_id,)).fetchone()[0]
    if kids:
        raise EditError("Other branches were forked from this one; delete those first")
    for t in ("award_overrides", "displacement_log", "injuries", "player_career_events", "squad_overrides"):
        conn.execute(f"DELETE FROM {t} WHERE branch_id = ?", (branch_id,))
    conn.execute("DELETE FROM event_overrides WHERE override_id IN "
                 "(SELECT override_id FROM fixture_overrides WHERE branch_id = ?)", (branch_id,))
    conn.execute("DELETE FROM lineup_overrides WHERE override_id IN "
                 "(SELECT override_id FROM fixture_overrides WHERE branch_id = ?)", (branch_id,))
    conn.execute("DELETE FROM fixture_overrides WHERE branch_id = ?", (branch_id,))
    conn.execute("DELETE FROM branches WHERE branch_id = ?", (branch_id,))
    conn.commit()
    return {"deleted": branch_id}


def squad(conn, edition_id: str, team_id: str, branch_id: int | None = None) -> list:
    rows = conn.execute(
        "SELECT s.player_id, COALESCE(p.known_as, p.full_name) AS name, p.position, s.shirt_number "
        "FROM squads s JOIN players p USING(player_id) WHERE s.edition_id = ? AND s.team_id = ?",
        (edition_id, team_id)).fetchall()
    out = {r["player_id"]: dict(r) for r in rows}
    if branch_id is not None:
        for r in conn.execute(
                "SELECT o.player_id, o.action, COALESCE(p.known_as, p.full_name) AS name, p.position "
                "FROM squad_overrides o JOIN players p USING(player_id) "
                "WHERE o.branch_id = ? AND o.edition_id = ? AND o.team_id = ? ORDER BY o.seq",
                (branch_id, edition_id, team_id)):
            if r["action"] == "add":
                out[r["player_id"]] = {"player_id": r["player_id"], "name": r["name"],
                                       "position": r["position"], "shirt_number": None}
            else:
                out.pop(r["player_id"], None)
    from footsim import db as _db
    mins = _real_minutes(conn, edition_id)
    for pid, p in out.items():
        p["roles"] = _db.roles_of(conn, pid)
        p["minutes"] = mins.get(pid, 0)
    return sorted(out.values(), key=lambda p: (p["name"] or "").casefold())


_MIN_CACHE = {}


def _real_minutes(conn, edition_id):
    key = (edition_id, conn.execute("PRAGMA database_list").fetchone()[2])
    if key not in _MIN_CACHE:
        from footsim import stats as _stats
        _MIN_CACHE[key] = {p.player_id: p.minutes for p in _stats.edition_stats(conn, edition_id, None).players.values()}
    return _MIN_CACHE[key]


def default_lineup(conn, edition_id: str, team_id: str, branch_id: int | None, before_date: str) -> list:
    """The team's most recent lineup before this date, used for brand-new fixtures."""
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    prior = sorted((f for f in fx.values() if team_id in (f.home, f.away) and f.lineup and f.date <= before_date),
                   key=lambda f: f.date)
    if not prior:
        return []
    return [{**l, "minute_off": l["minute_off"]} for l in prior[-1].lineup if l["team_id"] == team_id]


# ------------------------------------------------------------------ views

def _fixture_json(f: Fixture, nm: dict) -> dict:
    return {
        "fixture_id": f.fixture_id, "stage": f.stage, "group": f.grp, "match_no": f.match_no, "date": f.date,
        "home": f.home, "away": f.away, "home_score": f.home_score, "away_score": f.away_score,
        "extra_time": bool(f.extra_time), "pen_home": f.pen_home, "pen_away": f.pen_away,
        "status": f.status, "origin": f.origin, "winner": f.winner(),
        "events": sorted(({**e, "player_name": nm.get(e["player_id"]),
                           "assist_name": nm.get(e.get("assist_player_id"))} for e in f.events),
                         key=lambda e: e["minute"]),
    }


def edition_view(conn, edition_id: str, branch_id: int | None = None) -> dict:
    nm, tn = names(conn), team_names(conn)
    res = engine.resolve(conn, edition_id, branch_id)
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    groups = {}
    for g, rows in res.tables.items():
        groups[g] = {
            "table": [{"team": r.team, "name": tn.get(r.team, r.team), "p": r.played, "w": r.won, "d": r.drawn,
                       "l": r.lost, "gf": r.gf, "ga": r.ga, "gd": r.gd, "pts": r.points, "fp": r.fair_play,
                       "lots": r.decided_by_lots, "adj": r.adjust} for r in rows],
            "fixtures": [_fixture_json(f, nm) for f in sorted(fx.values(), key=lambda f: f.date)
                         if f.stage == "group" and f.grp == g],
        }
    slots = []
    for s in res.slots:
        item = {"match_no": s.match_no, "stage": s.stage, "home": s.home, "away": s.away,
                "status": s.status, "reason": s.reason, "fixture": None, "num_legs": s.legs}
        if s.fixture is not None:
            item["fixture"] = _fixture_json(s.fixture, nm)
            if s.fixture.legs:                 # a two-legged tie: both real legs, and how it was decided
                item["legs"] = [_fixture_json(l, nm) for l in s.fixture.legs]
                item["decided_by"] = s.fixture.decided_by
        else:
            stub = next((f for f in fx.values() if f.match_no == s.match_no), None)
            if stub is not None:
                item["fixture_id"] = stub.fixture_id
                item["date"] = stub.date
        slots.append(item)
    # Display order: walk back from the final so each match sits beside the two that feed it.
    from footsim import db as _db
    feeds = {b["match_no"]: [int(c[1:]) for c in (b["home"], b["away"]) if c[0] == "W" and c[1:].isdigit()]
             for b in _db.get_ruleset(conn, edition_id)["knockout"]["bracket"]}
    order = []

    def walk(n):
        for child in feeds.get(n, []):
            walk(child)
        order.append(n)
    final = next((s.match_no for s in res.slots if s.stage == "F"), None)
    if final is not None:
        walk(final)
    order += [s.match_no for s in res.slots if s.match_no not in order]
    rank = {n: i for i, n in enumerate(order)}
    slots.sort(key=lambda s: rank[s["match_no"]])
    losers = {b["match_no"]: [int(c[1:]) for c in (b["home"], b["away"]) if c[0] == "L" and c[1:].isdigit()]
              for b in _db.get_ruleset(conn, edition_id)["knockout"]["bracket"]}
    for s in slots:
        s["feeds"] = feeds.get(s["match_no"], [])
        s["loser_feeds"] = losers.get(s["match_no"], [])

    me = player.profile(conn, branch_id)
    rules = _db.get_ruleset(conn, edition_id)
    champion, runner_up, third = res.champion, res.runner_up, res.third
    league = rules.get("format") == "league"
    if league:                         # a league has no final: the champion is the table leader once every match is played
        t = res.tables.get("A", [])
        done = bool(t) and all(r.played == 2 * (len(t) - 1) for r in t)
        champion, runner_up, third = ((t[0].team, t[1].team, t[2].team) if done else (None, None, None))
    return {"edition_id": edition_id, "branch_id": branch_id, "teams": tn, "groups": groups, "slots": slots,
            "format": rules.get("format", "groups_then_knockout"), "club_focus": rules.get("focus_team"), "zones": rules.get("zones") or ([] if league else [
                {"positions": [1, 2], "kind": "adv", "label": "Advance to the knockout round"},
                {"positions": [3, 4], "kind": "out", "label": "Eliminated"}]),
            "adjust_notes": {t: a.get("reason", "") for t, a in (rules.get("group_stage", {}).get("point_adjustments") or {}).items()},
            "classification": rules.get("classification_note", ""),
            "focus_team": me.team_id if me else None, "player_name": me.name if me else None,
            "mode": branch_mode(conn, branch_id),
            "champion": champion, "runner_up": runner_up, "third": third,
            "violations": [{"fixture_id": f, "player": nm.get(p, p)} for f, p in res.violations]}


def editions(conn) -> list:
    """Every tournament in the database, oldest first, for the edition switcher."""
    from footsim import db as _db
    out = []
    for r in conn.execute("SELECT e.edition_id, e.season, e.start_date, c.name, c.kind FROM editions e "
                          "JOIN competitions c USING(competition_id) JOIN rulesets USING(edition_id) ORDER BY e.start_date DESC"):
        rules = _db.get_ruleset(conn, r["edition_id"])
        out.append({"edition_id": r["edition_id"], "name": rules.get("display_name") or f"{r['name']} {r['season']}",
                    "kind": r["kind"], "format": rules.get("format", "groups_then_knockout"), "focus_team": rules.get("focus_team")})
    return out


def fixture_detail(conn, edition_id: str, fixture_id: str, branch_id: int | None = None) -> dict:
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    if fixture_id not in fx:
        raise EditError(f"No fixture {fixture_id}")
    f = fx[fixture_id]
    nm = names(conn)
    out = _fixture_json(f, nm)
    out["squads"] = {t: squad(conn, edition_id, t, branch_id) for t in (f.home, f.away)}
    lineup = f.lineup or (default_lineup(conn, edition_id, f.home, branch_id, f.date) +
                          default_lineup(conn, edition_id, f.away, branch_id, f.date))
    out["lineup"] = [{**l, "name": nm.get(l["player_id"])} for l in lineup]
    out["is_historical"] = f.status == "historical"
    me = player.profile(conn, branch_id)
    out["editable"] = (me is None or me.team_id in (f.home, f.away) or branch_mode(conn, branch_id) == "free") \
        and not (f.leg and f.stage != "group")        # home-and-away knockout ties: editing comes in a later step
    out["leg"] = f.leg
    out["focus_team"] = me.team_id if me else None
    res = engine.resolve(conn, edition_id, branch_id)
    out["suspended"] = {t: [nm.get(p, p) for p in ps] for t, ps in res.suspensions.get(fixture_id, {}).items()}
    out["suspended_ids"] = [p for ps in res.suspensions.get(fixture_id, {}).values() for p in ps]
    if f.played:
        from footsim import match_ratings as mr
        pos = dict(conn.execute("SELECT player_id, position FROM players"))
        rt = mr.rate_match(f, pos)
        out["ratings"] = sorted(({"player_id": p, "name": nm.get(p), "team": next((l["team_id"] for l in f.lineup
                                  if l["player_id"] == p), None), "rating": r} for p, r in rt.items()),
                                key=lambda x: -x["rating"])
    return out


def _named_records(conn, edition_id, branch_id, records, tn):
    """The records with team names instead of ids."""
    out = dict(records)
    hs = out.get("highest_scoring_match")
    if hs:
        f = timeline.active_fixtures(conn, edition_id, branch_id).get(hs[0])
        if f is not None:
            out["highest_scoring_match"] = (hs[0], f"{tn.get(f.home, f.home)} {f.home_score}-{f.away_score} {tn.get(f.away, f.away)}")
    return out


def _db_rules(conn, edition_id):
    from footsim import db as _db
    return _db.get_ruleset(conn, edition_id)


def stats_view(conn, edition_id: str, branch_id: int | None = None) -> dict:
    nm, tn = names(conn), team_names(conn)
    st = stats.edition_stats(conn, edition_id, branch_id)
    scorers = sorted((p for p in st.players.values() if p.goals or p.assists),
                     key=lambda p: (-p.goals, -p.assists, p.minutes))[:25]
    finish_order = ["Champion", "Runner-up", "Third", "Fourth", "Quarter-finals", "Round of 16", "Group stage", ""]
    return {
        "scorers": [{"player_id": p.player_id, "name": nm.get(p.player_id), "team": p.team_id, "goals": p.goals,
                     "pens": p.pen_goals, "assists": p.assists, "minutes": p.minutes, "apps": p.apps,
                     "rating": st.ratings.get(p.player_id, {}).get("avg")}
                    for p in scorers],
        "top_rated": [{"player_id": pid, "name": nm.get(pid), "team": st.players[pid].team_id, "avg": r["avg"],
                       "matches": r["matches"]}
                      for pid, r in sorted(((k, v) for k, v in st.ratings.items() if v["share"] >= 0.5),
                                           key=lambda kv: -kv[1]["score"])[:10]],
        "awards_calculated": branch_id is not None,
        "awards": {k: [{"player_id": i, "name": nm.get(i, i)} for i in v] for k, v in st.awards.items()},
        "teams": [{"team": t.team_id, "name": tn.get(t.team_id), "finish": t.finish, "w": t.won, "d": t.drawn,
                   "l": t.lost, "gf": t.gf, "ga": t.ga, "cs": t.clean_sheets}
                  for t in sorted(st.teams.values(), key=lambda t: finish_order.index(t.finish))],
        "records": _named_records(conn, edition_id, branch_id, st.records, tn),
        "generated": bool(conn.execute("SELECT 1 FROM data_notes WHERE edition_id = ? AND kind IN ('generated_details','lineup_incomplete','subs_unknown') LIMIT 1",
                                       (edition_id,)).fetchone()),
        "season": (season_stats.season_records(conn, edition_id, branch_id, nm, tn)
                   if _db_rules(conn, edition_id).get("format") == "league" else []),
    }


# ------------------------------------------------------------------ snapshot + diff

def snapshot(conn, edition_id: str, branch_id: int | None) -> dict:
    st = stats.edition_stats(conn, edition_id, branch_id)
    res = st.resolution
    return {
        "positions": {g: [r.team for r in rows] for g, rows in res.tables.items()},
        "slots": {s.match_no: {"stage": s.stage, "home": s.home, "away": s.away, "status": s.status,
                               "score": (f"{s.fixture.home_score}-{s.fixture.away_score}"
                                         + (f" ({s.fixture.pen_home}-{s.fixture.pen_away}p)"
                                            if s.fixture.pen_home is not None else ""))
                               if s.fixture else None,
                               "winner": s.fixture.winner() if s.fixture else None}
                  for s in res.slots},
        "champion": res.champion,
        "golden_boot": st.awards.get("golden_boot", []),
        "players": {p.player_id: (p.goals, p.assists) for p in st.players.values()},
        "finishes": {t.team_id: t.finish for t in st.teams.values()},
    }


def diff(conn, before: dict, after: dict, fixes: list | None = None) -> dict:
    nm, tn = names(conn), team_names(conn)
    out = {"groups": [], "bracket": [], "champion": None, "golden_boot": None, "players": [], "finishes": [],
           "suspensions": [{"player": nm.get(f["player_id"], f["player_id"]),
                            "replacement": nm.get(f["replacement"]) if f.get("replacement") else None,
                            "match": f"{tn.get(f['home'], f['home'])} v {tn.get(f['away'], f['away'])}",
                            "date": f["date"]} for f in (fixes or [])]}
    for g in after["positions"]:
        b, a = before["positions"].get(g, []), after["positions"][g]
        if b != a:
            out["groups"].append({"group": g, "before": b, "after": a})
    for n, a in after["slots"].items():
        b = before["slots"].get(n)
        if b != a:
            out["bracket"].append({"match_no": n, "stage": a["stage"], "before": b, "after": a})
    if before["champion"] != after["champion"]:
        out["champion"] = {"before": before["champion"], "after": after["champion"]}
    if before["golden_boot"][:1] != after["golden_boot"][:1]:
        out["golden_boot"] = {"before": [nm.get(p, p) for p in before["golden_boot"]],
                              "after": [nm.get(p, p) for p in after["golden_boot"]]}
    for pid in set(before["players"]) | set(after["players"]):
        b, a = before["players"].get(pid, (0, 0)), after["players"].get(pid, (0, 0))
        if b != a:
            out["players"].append({"name": nm.get(pid, pid), "goals": [b[0], a[0]], "assists": [b[1], a[1]]})
    out["players"].sort(key=lambda p: -abs(p["goals"][1] - p["goals"][0]))
    for t in set(before["finishes"]) | set(after["finishes"]):
        b, a = before["finishes"].get(t, ""), after["finishes"].get(t, "")
        if b != a:
            out["finishes"].append({"team": tn.get(t, t), "before": b, "after": a})
    out["empty"] = not any(out[k] for k in ("groups", "bracket", "champion", "golden_boot", "players", "finishes",
                                             "suspensions"))
    return out


def _log_displacements(conn, edition_id: str, branch_id: int) -> None:
    """Record every knockout place now held by a different team than in real history."""
    real = engine.resolve(conn, edition_id, None).tables
    now = engine.resolve(conn, edition_id, branch_id).tables
    conn.execute("DELETE FROM displacement_log WHERE branch_id = ? AND edition_id = ?", (branch_id, edition_id))
    for g, rows in now.items():
        for pos in (0, 1):
            was, is_ = real[g][pos].team, rows[pos].team
            if was != is_:
                conn.execute("INSERT INTO displacement_log (branch_id, edition_id, slot, historical_team_id, "
                             "new_team_id) VALUES (?,?,?,?,?)", (branch_id, edition_id, f"{pos + 1}{g}", was, is_))
    conn.commit()


# ------------------------------------------------------------------ edits

def _validate(f: Fixture, payload: dict) -> list:
    hs, as_ = payload.get("home_score"), payload.get("away_score")
    if not isinstance(hs, int) or not isinstance(as_, int) or hs < 0 or as_ < 0:
        raise EditError("Scores must be whole numbers, 0 or more")
    events = payload.get("events", [])
    for e in events:
        if e.get("event_type") not in EVENT_TYPES:
            raise EditError(f"Unknown event type {e.get('event_type')}")
        if e.get("team_id") not in (f.home, f.away):
            raise EditError("Every event needs one of the two teams")
        if not isinstance(e.get("minute"), int) or not 0 <= e["minute"] <= 130:
            raise EditError("Minutes must be between 0 and 130")
        if e.get("assist_player_id") and e.get("assist_player_id") == e.get("player_id"):
            raise EditError("A player can't assist their own goal")
    for team, score in ((f.home, hs), (f.away, as_)):
        other = f.away if team == f.home else f.home
        goals = sum(1 for e in events if e["team_id"] == team and e["event_type"] in GOAL_TYPES)
        goals += sum(1 for e in events if e["team_id"] == other and e["event_type"] == "own_goal")
        if goals != score:
            raise EditError(f"{team} scored {score} but {goals} goal event(s) are listed. "
                            "Add or remove scorers (an own goal belongs to the defender's team).")
    ph, pa = payload.get("pen_home"), payload.get("pen_away")
    if f.stage != "group":
        if hs == as_:
            if not payload.get("extra_time"):
                raise EditError("A drawn knockout match must go to extra time")
            if ph is None or pa is None or ph == pa:
                raise EditError("A drawn knockout match needs a penalty shootout winner")
        elif ph is not None or pa is not None:
            raise EditError("Only drawn matches have a shootout")
    elif ph is not None or pa is not None or payload.get("extra_time"):
        raise EditError("Group matches have no extra time or shootout")
    return events


def edit_fixture(conn, edition_id: str, branch_id: int, fixture_id: str, payload: dict) -> dict:
    _leg = conn.execute("SELECT leg, stage FROM fixtures WHERE fixture_id = ?", (fixture_id,)).fetchone()
    if _leg and _leg["leg"] and _leg["stage"] != "group":
        raise EditError("Editing the legs of a home-and-away knockout tie isn't available yet")
    if branch_id is None:
        raise EditError("Real history can't be edited. Create or pick a branch first.")
    locked = conn.execute("SELECT locked FROM editions WHERE edition_id = ?", (edition_id,)).fetchone()
    if locked and locked[0]:
        raise EditError("This tournament began before 2001 and is locked canon")
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    if fixture_id not in fx:
        raise EditError(f"No fixture {fixture_id}")
    f = fx[fixture_id]
    me = player.profile(conn, branch_id)
    check_ripple(me, f, branch_mode(conn, branch_id))
    events = _validate(f, payload)
    lineup = payload.get("lineup") or f.lineup or (
        default_lineup(conn, edition_id, f.home, branch_id, f.date) +
        default_lineup(conn, edition_id, f.away, branch_id, f.date))
    if me is not None and me.team_id in (f.home, f.away) and not payload.get("lineup"):
        lineup = _role_lineup(conn, edition_id, branch_id, me, f, lineup)
    if me is not None:
        on = {l["player_id"] for l in lineup}
        if any(me.player_id in (e.get("player_id"), e.get("assist_player_id")) for e in events) \
                and me.player_id not in on:
            raise EditError(f"{me.name} isn't playing in this match (see his role), so he can't have events in it")

    before = snapshot(conn, edition_id, branch_id)
    timeline.set_result(conn, branch_id, f, payload["home_score"], payload["away_score"], events=events,
                        lineup=lineup, extra_time=int(bool(payload.get("extra_time"))),
                        pen_home=payload.get("pen_home"), pen_away=payload.get("pen_away"),
                        status="overridden", origin="user_edit", reason=payload.get("reason"),
                        edition_id=edition_id)
    fixes = engine.apply_cascade(conn, edition_id, branch_id).fixes
    _log_displacements(conn, edition_id, branch_id)
    return {"ok": True, "diff": diff(conn, before, snapshot(conn, edition_id, branch_id), fixes)}


def branch_mode(conn, branch_id) -> str:
    if branch_id is None:
        return "focus"
    row = conn.execute("SELECT mode FROM branches WHERE branch_id = ?", (branch_id,)).fetchone()
    return (row[0] if row else None) or "focus"


def set_mode(conn, branch_id: int, mode: str) -> dict:
    if branch_id is None:
        raise EditError("Pick a branch first")
    if mode not in ("focus", "free"):
        raise EditError("Mode must be focus or free")
    conn.execute("UPDATE branches SET mode = ? WHERE branch_id = ?", (mode, branch_id))
    conn.commit()
    return {"ok": True, "mode": mode}


def check_ripple(me, f: Fixture, mode: str = "focus") -> None:
    """Charter rule (Focus mode): only matches involving the created player's team can be edited by hand.
    Free mode lifts it for this branch."""
    if mode == "free":
        return
    if me is not None and me.team_id not in (f.home, f.away):
        raise EditError(f"Only {me.team_id} matches can be edited in Focus mode. Switch this branch to Free mode to edit any match. "
                        "Other matches change through the knock-on effects of your edits"
                        + (", or roll this one." if f.status == "pending" else "."))


def _role_lineup(conn, edition_id, branch_id, me, f, lineup):
    from footsim.roller import _player_info
    info = _player_info(conn, edition_id)
    info[me.player_id] = player.scoring_info(me)
    squad_ids = [s["player_id"] for s in squad(conn, edition_id, me.team_id, branch_id)]
    role = player.get_role(conn, branch_id, f.fixture_id)
    if player.injured_on(conn, branch_id, me.player_id, f.date):
        role = {"role": "unused", "target_player_id": None, "minute": None}
    lineup, _ = player.strip(lineup, [], me, squad_ids, info)
    lineup, _ = player.apply_role(lineup, [], me, role, squad_ids, info)
    return lineup


def revert_fixture(conn, edition_id: str, branch_id: int, fixture_id: str) -> dict:
    if branch_id is None:
        raise EditError("Nothing to revert in real history")
    before = snapshot(conn, edition_id, branch_id)
    row = conn.execute("SELECT override_id FROM fixture_overrides WHERE branch_id = ? AND fixture_id = ?",
                       (branch_id, fixture_id)).fetchone()
    if row is None:
        raise EditError("This match isn't changed in this branch")
    conn.execute("DELETE FROM fixture_overrides WHERE override_id = ?", (row[0],))
    conn.commit()
    fixes = engine.apply_cascade(conn, edition_id, branch_id).fixes
    _log_displacements(conn, edition_id, branch_id)
    return {"ok": True, "diff": diff(conn, before, snapshot(conn, edition_id, branch_id), fixes)}


def roll_preview(conn, edition_id: str, branch_id: int | None, fixture_id: str, seed: int | None = None) -> dict:
    """A rolled result for the editor to show; nothing is saved."""
    from footsim import roller
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    if fixture_id not in fx:
        raise EditError(f"No fixture {fixture_id}")
    f = fx[fixture_id]
    if f.status != "pending":
        check_ripple(player.profile(conn, branch_id), f, branch_mode(conn, branch_id))
    out = roller.roll(conn, edition_id, branch_id, f, seed=seed)
    nm = names(conn)
    for e in out["events"]:
        e["player_name"], e["assist_name"] = nm.get(e["player_id"]), nm.get(e.get("assist_player_id"))
    return out


def roll_constrained(conn, edition_id: str, branch_id: int | None, fixture_id: str, constraints: dict,
                     seed: int | None = None) -> dict:
    """Preview a roll built around locks / a target outcome. Nothing is saved."""
    from footsim import roller
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    if fixture_id not in fx:
        raise EditError(f"No fixture {fixture_id}")
    f = fx[fixture_id]
    if f.status != "pending":
        check_ripple(player.profile(conn, branch_id), f, branch_mode(conn, branch_id))
    out = roller.roll_with(conn, edition_id, branch_id, f, constraints, seed=seed)
    _validate(f, out)
    nm = names(conn)
    for e in out["events"]:
        e["player_name"], e["assist_name"] = nm.get(e["player_id"]), nm.get(e.get("assist_player_id"))
    for n in out["notes"]:
        n["event"]["player_name"] = nm.get(n["event"].get("player_id"))
        # show the assist as it ends up in this version
        final = next((e for e in out["events"] if e["minute"] == n["event"]["minute"]
                      and e["team_id"] == n["event"]["team_id"] and e["event_type"] == n["event"]["event_type"]), None)
        src = final if (final and n["kind"] != "removed") else n["event"]
        n["event"]["assist_name"] = nm.get(src.get("assist_player_id"))
    out["source"] = "real result" if f.origin in ("baseline", "suspension_fix") else "current result"
    return out


def roll_save(conn, edition_id: str, branch_id: int, fixture_id: str) -> dict:
    """Roll one match and save it (the only way to fill other teams' pending matches)."""
    from footsim import roller
    if branch_id is None:
        raise EditError("Pick a branch first")
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    f = fx.get(fixture_id)
    if f is None:
        raise EditError(f"No fixture {fixture_id}")
    if f.status != "pending":
        check_ripple(player.profile(conn, branch_id), f, branch_mode(conn, branch_id))
    before = snapshot(conn, edition_id, branch_id)
    roller.roll_and_save(conn, edition_id, branch_id, fixture_id, origin="second_order_generated")
    fixes = engine.apply_cascade(conn, edition_id, branch_id).fixes
    _log_displacements(conn, edition_id, branch_id)
    return {"ok": True, "diff": diff(conn, before, snapshot(conn, edition_id, branch_id), fixes)}


def roll_pending(conn, edition_id: str, branch_id: int, seed: int | None = None) -> dict:
    """Generate every match still needing a result, so the tournament reaches a champion."""
    from footsim import roller
    if branch_id is None:
        raise EditError("Pick a branch first")
    before = snapshot(conn, edition_id, branch_id)
    rolled = roller.roll_pending(conn, edition_id, branch_id, seed=seed)
    fixes = engine.apply_cascade(conn, edition_id, branch_id).fixes
    _log_displacements(conn, edition_id, branch_id)
    return {"ok": True, "rolled": rolled, "diff": diff(conn, before, snapshot(conn, edition_id, branch_id), fixes)}


def set_award(conn, edition_id: str, branch_id: int, award: str, player_id: str, rank: int = 1) -> dict:
    if branch_id is None:
        raise EditError("Pick a branch first")
    try:
        stats.set_award(conn, branch_id, edition_id, award, player_id, rank)
    except ValueError as e:
        raise EditError(str(e))
    return {"ok": True}


def history_diff(conn, edition_id: str, branch_id: int | None) -> dict:
    """Everything in this branch that differs from real history."""
    if branch_id is None:
        raise EditError("Pick a branch to compare with real history")
    out = diff(conn, snapshot(conn, edition_id, None), snapshot(conn, edition_id, branch_id))
    nm, tn = names(conn), team_names(conn)
    real = timeline.active_fixtures(conn, edition_id, None)
    now = timeline.active_fixtures(conn, edition_id, branch_id)
    res = engine.resolve(conn, edition_id, branch_id)
    counts = engine.played_fixture_ids(res)
    pending = {s.match_no for s in res.slots if s.status == "pending"}
    label = {"user_edit": "edited by you", "rolled": "rolled", "second_order_generated": "rolled",
             "reused_historical": "real result from another meeting", "suspension_fix": "lineup fixed (suspension)",
             "cascade": "needs a result"}
    matches = []
    for fid, f in now.items():
        r = real.get(fid)
        if f.origin == "baseline":
            continue
        if f.stage != "group" and fid not in counts and f.match_no not in pending:
            continue
        def line(x):
            if x is None or x.home_score is None:
                return None
            p = f" ({x.pen_home}-{x.pen_away}p)" if x.pen_home is not None else ""
            return f"{tn.get(x.home, x.home)} {x.home_score}-{x.away_score} {tn.get(x.away, x.away)}{p}"
        matches.append({"fixture_id": fid, "date": f.date, "stage": f.stage, "group": f.grp, "match_no": f.match_no,
                        "now": line(f) or f"{tn.get(f.home)} v {tn.get(f.away)}",
                        "real": line(r) if r and r.teams() == f.teams() else (line(r) and f"{line(r)} (different teams)"),
                        "how": label.get(f.origin, f.origin)})
    matches.sort(key=lambda m: (m["date"], m["match_no"] or 0))
    out["matches"] = matches
    out["displacements"] = displacements(conn, edition_id, branch_id)
    me = player.profile(conn, branch_id)
    if me is not None:
        st_now = stats.edition_stats(conn, edition_id, branch_id).players.get(me.player_id)
        st_real = stats.edition_stats(conn, edition_id, None).players.get(me.replaces)
        pick = lambda x: {k: getattr(x, k) if x else 0 for k in ("apps", "starts", "minutes", "goals", "assists")}
        out["player"] = {"name": me.name, "replaces": nm.get(me.replaces), "now": pick(st_now), "real": pick(st_real)}
    return out


def displacements(conn, edition_id: str, branch_id: int) -> list:
    tn = team_names(conn)
    return [{"slot": r["slot"], "was": tn.get(r["historical_team_id"]), "now": tn.get(r["new_team_id"])}
            for r in conn.execute("SELECT * FROM displacement_log WHERE branch_id = ? AND edition_id = ? "
                                  "ORDER BY slot", (branch_id, edition_id))]


# ------------------------------------------------------------------ created player

def tiers() -> dict:
    from footsim.db import ROLE_GROUP, ROLE_ORDER
    return {"tiers": [{"id": k, "label": player.TIER_LABEL[k], "overall": v} for k, v in player.TIERS.items()],
            "roles": [{"id": r, "group": ROLE_GROUP[r]} for r in ROLE_ORDER]}


def create_player(conn, edition_id: str, branch_id: int, body: dict) -> dict:
    if branch_id is None:
        raise EditError("Create or pick a branch first")
    p = player.create(conn, branch_id, full_name=body.get("full_name", ""), known_as=body.get("known_as"),
                      birth_date=body.get("birth_date"), team_id=body.get("team_id"),
                      position=body.get("position"), replaces_player_id=body.get("replaces_player_id"),
                      tier=body.get("tier"), overall=body.get("overall"),
                      detail_position=body.get("detail_position"), edition_id=edition_id,
                      roles=body.get("roles"))
    return {"ok": True, "player_id": p.player_id}


def update_player(conn, branch_id: int, body: dict, edition_id: str = "WC2022") -> dict:
    """Change anything about the created player except his national team."""
    if player.profile(conn, branch_id) is None:
        raise EditError("This branch has no created player")
    before = snapshot(conn, edition_id, branch_id)
    player.update(conn, branch_id, **{k: body.get(k) for k in (
        "full_name", "known_as", "birth_date", "position", "detail_position", "tier", "overall",
        "replaces_player_id", "roles") if k in body})
    fixes = engine.apply_cascade(conn, edition_id, branch_id).fixes
    return {"ok": True, "diff": diff(conn, before, snapshot(conn, edition_id, branch_id), fixes)}


def info_cache(conn, edition_id, me):
    from footsim.roller import _player_info
    info = _player_info(conn, edition_id)
    info[me.player_id] = player.scoring_info(me)
    return info


def set_role(conn, edition_id: str, branch_id: int, fixture_id: str, body: dict) -> dict:
    """Set the player's role in one match. Edited/rolled matches have their saved lineup rewritten."""
    me = player.profile(conn, branch_id)
    if me is None:
        raise EditError("This branch has no created player")
    before = snapshot(conn, edition_id, branch_id)
    if body.get("role") in ("start", "sub", "inherit"):
        res = engine.resolve(conn, edition_id, branch_id)
        if any(me.player_id in ps for ps in res.suspensions.get(fixture_id, {}).values()):
            raise EditError(f"{me.name} is suspended for this match")
    player.set_role(conn, branch_id, fixture_id, body.get("role"), body.get("target_player_id") or None,
                    body.get("minute"))
    row = conn.execute("SELECT override_id, origin FROM fixture_overrides WHERE branch_id = ? AND fixture_id = ?",
                       (branch_id, fixture_id)).fetchone()
    if row and row["origin"] not in ("cascade", "reused_historical"):
        raw = timeline.active_fixtures(conn, edition_id, branch_id, raw=True)[fixture_id]
        from footsim.roller import _player_info
        info = _player_info(conn, edition_id)
        info[me.player_id] = player.scoring_info(me)
        squad_ids = [s["player_id"] for s in squad(conn, edition_id, me.team_id, branch_id)]
        role = player.get_role(conn, branch_id, fixture_id)
        lineup, events = player.strip(raw.lineup, raw.events, me, squad_ids, info)
        lineup, events = player.apply_role(lineup, events, me, role, squad_ids, info)
        conn.execute("DELETE FROM lineup_overrides WHERE override_id = ?", (row["override_id"],))
        conn.execute("DELETE FROM event_overrides WHERE override_id = ?", (row["override_id"],))
        for l in lineup:
            conn.execute("INSERT INTO lineup_overrides VALUES (?,?,?,?,?,?)",
                         (row["override_id"], l["team_id"], l["player_id"], l["started"], l.get("minute_on"),
                          l.get("minute_off")))
        for e in events:
            conn.execute("INSERT INTO event_overrides (override_id, minute, added_time, team_id, player_id, "
                         "event_type, assist_player_id) VALUES (?,?,?,?,?,?,?)",
                         (row["override_id"], e["minute"], e.get("added_time"), e["team_id"], e.get("player_id"),
                          e["event_type"], e.get("assist_player_id")))
        conn.commit()
    fixes = engine.apply_cascade(conn, edition_id, branch_id).fixes
    return {"ok": True, "diff": diff(conn, before, snapshot(conn, edition_id, branch_id), fixes)}


def player_view(conn, edition_id: str, branch_id: int | None) -> dict:
    me = player.profile(conn, branch_id)
    if me is None:
        return {"player": None}
    nm, tn = names(conn), team_names(conn)
    st = stats.edition_stats(conn, edition_id, branch_id)
    res = st.resolution
    valid = engine.played_fixture_ids(res)
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    raw = timeline.active_fixtures(conn, edition_id, branch_id, raw=True)
    sq = squad(conn, edition_id, me.team_id, branch_id)
    matches = []
    for f in sorted(fx.values(), key=lambda f: f.date):
        if me.team_id not in (f.home, f.away):
            continue
        counts = f.stage == "group" or f.fixture_id in valid
        if not counts and f.status != "pending":
            continue
        mine = next((l for l in f.lineup if l["player_id"] == me.player_id), None)
        if mine:
            on = 0 if mine["started"] else mine["minute_on"]
            off = mine["minute_off"] if mine["minute_off"] is not None else (120 if f.extra_time else 90)
            played = f"Started, {off - on} min" if mine["started"] else f"Sub on {on}', {off - on} min"
        else:
            played = "Didn't play"
        ev = [{"minute": e["minute"], "type": e["event_type"]} for e in f.events if e["player_id"] == me.player_id]
        ev += [{"minute": e["minute"], "type": "assist"} for e in f.events if e.get("assist_player_id") == me.player_id]
        opp = f.away if f.home == me.team_id else f.home
        ours = f.home_score if f.home == me.team_id else f.away_score
        theirs = f.away_score if f.home == me.team_id else f.home_score
        rawf = raw.get(f.fixture_id)
        pos = {r[0]: r[1] for r in conn.execute("SELECT player_id, position FROM players")}
        starters = [{"player_id": l["player_id"], "name": nm.get(l["player_id"]), "position": pos.get(l["player_id"]),
                     "same": pos.get(l["player_id"]) == me.position}
                    for l in (rawf.lineup if rawf else []) if l["team_id"] == me.team_id and l["started"]
                    and l["player_id"] not in (me.player_id, me.replaces)]
        starters.sort(key=lambda x: (not x["same"], (x["name"] or "").casefold()))
        _info = info_cache(conn, edition_id, me)
        for st_ in starters:
            st_["roles"] = list(_info.get(st_["player_id"], {}).get("roles", ()))
            st_["same"] = bool(set(st_["roles"]) & set(me.roles)) or st_["same"]
        starters.sort(key=lambda x: (not x["same"], (x["name"] or "").casefold()))
        auto_pick = player.auto_target(rawf.lineup if rawf else [], me.team_id, me.position, _info,
                                       exclude={me.player_id, me.replaces}, me_roles=me.roles)
        role = player.get_role(conn, branch_id, f.fixture_id)
        matches.append({
            "fixture_id": f.fixture_id, "date": f.date, "stage": f.stage, "group": f.grp, "opponent": opp,
            "opponent_name": tn.get(opp), "result": None if ours is None else f"{ours}-{theirs}",
            "pens": None if f.pen_home is None else (f"{f.pen_home}-{f.pen_away}" if f.home == me.team_id
                                                      else f"{f.pen_away}-{f.pen_home}"),
            "status": f.status, "origin": f.origin, "role": role, "played": played, "events": ev,
            "rating": st.match_ratings.get(f.fixture_id, {}).get(me.player_id),
            "injured": player.injured_on(conn, branch_id, me.player_id, f.date),
            "suspended": me.player_id in res.suspensions.get(f.fixture_id, {}).get(me.team_id, []),
            "starters": starters, "auto_target": nm.get(auto_pick), "auto_target_pos": pos.get(auto_pick),
            "replaced_played": any(l["player_id"] == me.replaces for l in (rawf.lineup if rawf else [])),
        })
    line = st.players.get(me.player_id)
    totals = {k: v for k, v in vars(line).items() if isinstance(v, int)} if line else {}
    rank = st.awards.get("golden_boot", [])
    return {
        "player": {"player_id": me.player_id, "name": me.name, "team_id": me.team_id, "team": tn.get(me.team_id),
                   "position": me.position, "tier": me.tier, "tier_label": player.TIER_LABEL[me.tier],
                   "overall": me.overall, "birth_date": me.birth_date, "replaces": nm.get(me.replaces),
                   "replaces_id": me.replaces, "roles": list(me.roles), **dict(zip(("full_name", "known_as", "detail_position"), conn.execute(
                       "SELECT p.full_name, p.known_as, c.detail_position FROM players p JOIN created_players c "
                       "USING(player_id) WHERE p.player_id = ?", (me.player_id,)).fetchone())),
                   "impact_elo": round(player.impact_elo(me))},
        "rating": st.ratings.get(me.player_id, {}).get("avg"),
        "totals": totals, "golden_boot_rank": rank.index(me.player_id) + 1 if me.player_id in rank else None,
        "matches": matches, "squad": [s for s in sq if s["player_id"] != me.player_id],
        "unset": sum(1 for m in matches if m["role"]["role"] == "auto"),
        "injuries": [dict(r) for r in conn.execute(
            "SELECT injury_id, start_date, end_date, description FROM injuries WHERE branch_id = ? AND player_id = ? "
            "ORDER BY start_date", (branch_id, me.player_id))],
        "calendar": calendar(conn, edition_id, branch_id, me),
    }


def calendar(conn, edition_id: str, branch_id: int, me) -> list:
    items = [{"date": r["date"], "kind": r["event_type"], "notes": r["notes"], "source": "real"}
             for r in conn.execute("SELECT * FROM calendar_events WHERE edition_id = ? AND "
                                   "(team_id IS NULL OR team_id = ?)", (edition_id, me.team_id))]
    items += [{"date": r["date"], "kind": r["event_type"], "notes": r["notes"], "source": "yours",
               "id": r["career_event_id"]}
              for r in conn.execute("SELECT * FROM player_career_events WHERE branch_id = ? AND player_id = ?",
                                    (branch_id, me.player_id))]
    items += [{"date": r["start_date"], "kind": "injury",
               "notes": f"{r['description'] or 'Injury'} (out until {r['end_date'] or 'unknown'})", "source": "yours"}
              for r in conn.execute("SELECT * FROM injuries WHERE branch_id = ? AND player_id = ?",
                                    (branch_id, me.player_id))]
    return sorted(items, key=lambda x: x["date"])


def add_injury(conn, edition_id: str, branch_id: int, body: dict) -> dict:
    me = player.profile(conn, branch_id)
    if me is None:
        raise EditError("This branch has no created player")
    start, end = body.get("start_date"), body.get("end_date") or None
    if not start or (end and end < start):
        raise EditError("Give a start date, and an end date on or after it")
    before = snapshot(conn, edition_id, branch_id)
    conn.execute("INSERT INTO injuries (branch_id, player_id, start_date, end_date, description) VALUES (?,?,?,?,?)",
                 (branch_id, me.player_id, start, end, body.get("description") or "Injury"))
    conn.commit()
    fixes = engine.apply_cascade(conn, edition_id, branch_id).fixes
    return {"ok": True, "diff": diff(conn, before, snapshot(conn, edition_id, branch_id), fixes)}


def delete_injury(conn, branch_id: int, injury_id: int) -> dict:
    conn.execute("DELETE FROM injuries WHERE branch_id = ? AND injury_id = ?", (branch_id, injury_id))
    conn.commit()
    return {"ok": True}


def add_career_event(conn, branch_id: int, body: dict) -> dict:
    me = player.profile(conn, branch_id)
    if me is None:
        raise EditError("This branch has no created player")
    if not body.get("date") or not body.get("event_type"):
        raise EditError("Give a date and a type")
    conn.execute("INSERT INTO player_career_events (branch_id, player_id, date, event_type, team_id, notes) "
                 "VALUES (?,?,?,?,?,?)", (branch_id, me.player_id, body["date"], body["event_type"], me.team_id,
                                          body.get("notes")))
    conn.commit()
    return {"ok": True}


def delete_career_event(conn, branch_id: int, event_id: int) -> dict:
    conn.execute("DELETE FROM player_career_events WHERE branch_id = ? AND career_event_id = ?", (branch_id, event_id))
    conn.commit()
    return {"ok": True}


# ------------------------------------------------------------------ bracket picker

def _force_winner(conn, edition_id, branch_id, f, team, seed):
    """Save a realistic result for f in which `team` wins (keeps the current one if it already does)."""
    from footsim import roller
    if f.played and f.winner() == team:
        return False
    info = roller._player_info(conn, edition_id)
    rng = __import__("random").Random(seed)
    best = None
    for _ in range(150):
        r = roller.roll(conn, edition_id, branch_id, f, seed=rng.randrange(1 << 30), info=info)
        w = Fixture("x", f.stage, None, None, f.date, f.home, f.away, r["home_score"], r["away_score"], 0,
                    r["pen_home"], r["pen_away"]).winner()
        if w == team:
            best = r
            break
    if best is None:                       # a huge underdog: build the win around a fixed score
        hs, as_ = (1, 0) if f.home == team else (0, 1)
        best = roller.roll_with(conn, edition_id, branch_id, f, {"home_score": hs, "away_score": as_}, seed=seed)
    _validate(f, best)
    timeline.set_result(conn, branch_id, f, best["home_score"], best["away_score"], events=best["events"],
                        lineup=best["lineup"], extra_time=int(best["extra_time"]), pen_home=best["pen_home"],
                        pen_away=best["pen_away"], status="generated", origin="picked",
                        reason=f"winner picked: {team}", edition_id=edition_id)
    return True


def pick_winner(conn, edition_id: str, branch_id: int, match_no: int, team: str, seed: int | None = None) -> dict:
    """Make `team` win one knockout match; later rounds update."""
    if branch_id is None:
        raise EditError("Pick a branch first")
    before = snapshot(conn, edition_id, branch_id)
    res = engine.apply_cascade(conn, edition_id, branch_id)
    s = next((x for x in res.slots if x.match_no == int(match_no)), None)
    if s is None or s.home is None or s.away is None:
        raise EditError("Both teams in that match have to be known first")
    if team not in (s.home, s.away):
        raise EditError("That team isn't in this match")
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    f = next(x for x in fx.values() if x.match_no == int(match_no))
    _force_winner(conn, edition_id, branch_id, f, team, seed)
    fixes = engine.apply_cascade(conn, edition_id, branch_id).fixes
    _log_displacements(conn, edition_id, branch_id)
    return {"ok": True, "diff": diff(conn, before, snapshot(conn, edition_id, branch_id), fixes)}


def pick_podium(conn, edition_id: str, branch_id: int, first: str, second: str, third: str | None = None,
                seed: int | None = None) -> dict:
    """Choose the champion, runner-up and (optionally) third place; every other match fills in around them."""
    from footsim import roller
    import random
    if branch_id is None:
        raise EditError("Pick a branch first")
    picks = [t for t in (first, second, third) if t]
    if len(set(picks)) != len(picks) or not first or not second:
        raise EditError("Pick a different team for each place (champion and runner-up are required)")
    tn = team_names(conn)
    rng = random.Random(seed)
    before = snapshot(conn, edition_id, branch_id)
    res = engine.apply_cascade(conn, edition_id, branch_id)
    in_ko = {t for s in res.slots if s.stage == "R16" for t in (s.home, s.away) if t}
    if len(in_ko) < 16:
        raise EditError("Some round-of-16 places are still open; finish the group stage first")
    out = [t for t in picks if t not in in_ko]
    if out:
        raise EditError(f"{', '.join(tn.get(t, t) for t in out)} didn't reach the round of 16 in this branch. "
                        "Change their group matches first, or pick teams that got through.")

    # which semi-final each pick heads toward
    from footsim import db as _db
    bracket = {b["match_no"]: b for b in _db.get_ruleset(conn, edition_id)["knockout"]["bracket"]}
    parent = {}
    for n, b in bracket.items():
        for code in (b["home"], b["away"]):
            if code[0] == "W" and code[1:].isdigit():
                parent[int(code[1:])] = n

    def semi_of(team):
        n = next(x.match_no for x in res.slots if x.stage == "R16" and team in (x.home, x.away))
        while bracket[n]["stage"] != "SF":
            n = parent[n]
        return n
    sf = {t: semi_of(t) for t in picks}
    if sf[first] == sf[second]:
        raise EditError(f"{tn.get(first, first)} and {tn.get(second, second)} are in the same half of the bracket, "
                        "so they can't both reach the final. Pick a runner-up from the other half.")
    if third and sf[third] not in (sf[first], sf[second]):
        raise EditError(f"{tn.get(third, third)} can't finish third: a third-placed team loses its semi-final to "
                        "the champion or runner-up, so it has to be in one of their halves.")

    def must_win(s):
        teams = {s.home, s.away}
        if s.stage == "F":
            if teams != {first, second}:
                return "conflict"
            return first
        if s.stage == "3P":
            if third and third in teams:
                return third
            return None
        if s.stage == "SF":
            if first in teams and second in teams:
                return "conflict"
            for t in (first, second):
                if t in teams:
                    return t
            if third and third in teams:
                return "conflict"       # third must lose its semi to the champion or runner-up
            return None
        hits = [t for t in picks if t in teams]
        if len(hits) == 2:
            return "conflict"
        return hits[0] if hits else None

    for _ in range(12):
        res = engine.apply_cascade(conn, edition_id, branch_id)
        changed = False
        fx = timeline.active_fixtures(conn, edition_id, branch_id)
        for s in sorted(res.slots, key=lambda s: s.match_no):
            if s.home is None or s.away is None:
                continue
            w = must_win(s)
            if w == "conflict":
                a, b = s.home, s.away
                raise EditError(f"{tn.get(a, a)} and {tn.get(b, b)} meet in the {s.stage} (match {s.match_no}), "
                                "so that podium can't happen. Pick teams from different parts of the bracket.")
            f = next(x for x in fx.values() if x.match_no == s.match_no)
            if w is not None:
                if _force_winner(conn, edition_id, branch_id, f, w, rng.randrange(1 << 30)):
                    changed = True
                    break
            elif s.status == "pending":
                roller.roll_and_save(conn, edition_id, branch_id, f.fixture_id, seed=rng.randrange(1 << 30),
                                     origin="second_order_generated")
                changed = True
                break
        if not changed:
            break
    fixes = engine.apply_cascade(conn, edition_id, branch_id).fixes
    _log_displacements(conn, edition_id, branch_id)
    return {"ok": True, "diff": diff(conn, before, snapshot(conn, edition_id, branch_id), fixes)}


# ------------------------------------------------------------------ world view

def world_diff(conn, branch_id: int | None) -> dict:
    """Big changes across every tournament in this branch, one section per tournament."""
    if branch_id is None:
        raise EditError("Pick a branch to compare with real history")
    tn, nm = team_names(conn), names(conn)
    sections = []
    for r in conn.execute("SELECT e.edition_id, c.name, e.season, e.start_date FROM editions e "
                          "JOIN competitions c USING(competition_id) JOIN rulesets USING(edition_id) "
                          "ORDER BY e.start_date"):
        ed = r["edition_id"]
        d = history_diff(conn, ed, branch_id)
        real = engine.resolve(conn, ed, None)
        now = engine.resolve(conn, ed, branch_id)
        rocks = []
        if (real.champion, real.runner_up, real.third) != (now.champion, now.runner_up, now.third):
            rocks.append({"kind": "podium", "text": "Podium: " + ", ".join(
                f"{m} {tn.get(t, '?') if t else 'undecided'}" for m, t in
                (("🥇", now.champion), ("🥈", now.runner_up), ("🥉", now.third))),
                "real": ", ".join(f"{m} {tn.get(t, t)}" for m, t in
                                  (("🥇", real.champion), ("🥈", real.runner_up), ("🥉", real.third)))})
        st_now = stats.edition_stats(conn, ed, branch_id)
        st_real = stats.edition_stats(conn, ed, None)
        has_awards = conn.execute("SELECT 1 FROM awards WHERE edition_id = ?", (ed,)).fetchone()
        for award, label in ((("golden_ball", "Golden Ball"), ("golden_boot", "Golden Boot")) if has_awards else ()):
            a, b = st_now.awards.get(award, [None])[:1], st_real.awards.get(award, [None])[:1]
            if a != b:
                rocks.append({"kind": award, "text": f"{label}: {nm.get(a[0], '—') if a else 'undecided until the final'}",
                              "real": nm.get(b[0], "—") if b else "—"})
        if d["displacements"]:
            rocks.append({"kind": "advanced", "text": f"{len(d['displacements'])} team(s) got out of the group instead "
                          "of the real qualifiers", "real": ", ".join(x["was"] for x in d["displacements"])})
        if d.get("player"):
            p = d["player"]
            if p["now"] != p["real"]:
                rocks.append({"kind": "player", "text": f"{p['name']}: {p['now']['goals']} goals, "
                              f"{p['now']['assists']} assists in {p['now']['apps']} matches",
                              "real": f"{p['replaces']}: {p['real']['goals']} goals in {p['real']['apps']} matches"})
        sections.append({"edition_id": ed, "name": f"{r['name']} {r['season']}", "changed": bool(rocks or d["matches"]),
                         "rocks": rocks, "match_count": len(d["matches"]), "detail": d})
    changed = [x for x in sections if x["changed"]]
    return {"sections": sections,
            "summary": {"tournaments": len(sections), "changed": len(changed),
                        "champions": [{"name": x["name"], "now": next((r["text"] for r in x["rocks"] if r["kind"] == "podium"), None)}
                                      for x in changed if any(r["kind"] == "podium" for r in x["rocks"])]}}
