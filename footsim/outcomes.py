"""Phase 9, the bulk roller: lock what must be true, roll everything else.

A branch can hold *locks* for one edition: who finishes where (champion, runner-up, third, or league
positions), exact scores, a player's goals and assists, and who wins the Golden Boot / top-scorer race.
`run()` makes them all true at once:

  1. exact scores are saved first;
  2. the podium is forced (two-legged ties too: "make X go through");
  3. every match still needing a result is rolled around the locks;
  4. league positions are fitted by changing the fewest matches that matter;
  5. player goals/assists are fitted by moving goals between team-mates (scores never change);
  6. every lock is checked against the finished branch.

If anything can't be true together the answer is a list of plain-language contradictions and the branch is
left exactly as it was: the work happens inside a transaction that is rolled back (a preview does the same
and always rolls back). Only `apply=True` with every lock met keeps it.
"""
import json
import random
from dataclasses import replace

from footsim import api, db, engine, roller, stats, timeline
from footsim.api import EditError

KINDS = ("champion", "runner_up", "third", "score", "player_total", "golden_boot", "league_position")
PODIUM = ("champion", "runner_up", "third")
GOALS = ("goal", "pen_goal")


class _Tx:
    """The connection with commit() switched off, so everything done through it can still be rolled back."""

    def __init__(self, conn):
        self._c = conn

    def commit(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)


class Lock:
    def __init__(self, lock_id, kind, p):
        self.id, self.kind, self.p = lock_id, kind, p


# ------------------------------------------------------------------ storing locks

def _load(conn, edition_id: str, branch_id: int) -> list:
    return [Lock(r["lock_id"], r["kind"], json.loads(r["params"])) for r in conn.execute(
        "SELECT * FROM outcome_locks WHERE branch_id = ? AND edition_id = ? ORDER BY lock_id",
        (branch_id, edition_id))]


def _format(conn, edition_id: str) -> str:
    return db.get_ruleset(conn, edition_id).get("format", "groups_then_knockout")


def _in_edition(conn, edition_id: str, team: str) -> bool:
    return conn.execute("SELECT 1 FROM fixtures WHERE edition_id = ? AND (home_team_id = ? OR away_team_id = ?) LIMIT 1",
                        (edition_id, team, team)).fetchone() is not None


def _int(v, what, lo=0, hi=999):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise EditError(f"{what} must be a whole number")
    if not lo <= v <= hi:
        raise EditError(f"{what} must be {lo} to {hi}")
    return v


def add_lock(conn, edition_id: str, branch_id: int, kind: str, params: dict) -> dict:
    if branch_id is None:
        raise EditError("Pick a branch first")
    if kind not in KINDS:
        raise EditError("Unknown lock type")
    fmt = _format(conn, edition_id)
    nm = api.names(conn)
    p, replace_key = {}, None
    if kind in PODIUM:
        team = params.get("team")
        if fmt == "league":
            raise EditError("A league has no podium: lock a league position instead")
        if not team or not _in_edition(conn, edition_id, team):
            raise EditError("Pick a team that is in this competition")
        p = {"team": team}
    elif kind == "score":
        fx = timeline.active_fixtures(conn, edition_id, branch_id)
        f = fx.get(params.get("fixture_id"))
        if f is None:
            raise EditError("That match doesn't exist")
        p = {"fixture_id": f.fixture_id, "home": _int(params.get("home"), "The home score"),
             "away": _int(params.get("away"), "The away score"), "teams": [f.home, f.away]}
        ph, pa = params.get("pen_home"), params.get("pen_away")
        if ph not in (None, "") or pa not in (None, ""):
            p["pen_home"], p["pen_away"] = _int(ph, "The shootout score"), _int(pa, "The shootout score")
            if p["pen_home"] == p["pen_away"]:
                raise EditError("A shootout needs a winner")
        replace_key = ("fixture_id", p["fixture_id"])
    elif kind == "player_total":
        pid = params.get("player_id")
        if pid not in nm:
            raise EditError("Pick a player")
        goals = params.get("goals") if params.get("goals") not in (None, "") else None
        assists = params.get("assists") if params.get("assists") not in (None, "") else None
        if goals is None and assists is None:
            raise EditError("Give the number of goals, assists, or both")
        p = {"player_id": pid}
        if goals is not None:
            p["goals"] = _int(goals, "Goals")
        if assists is not None:
            p["assists"] = _int(assists, "Assists")
        replace_key = ("player_id", pid)
    elif kind == "golden_boot":
        pid = params.get("player_id")
        if pid not in nm:
            raise EditError("Pick a player")
        p = {"player_id": pid}
    else:                                             # league_position
        if fmt != "league":
            raise EditError("League positions only apply to a league season")
        team = params.get("team")
        if not team or not _in_edition(conn, edition_id, team):
            raise EditError("Pick a team in this league")
        n = len(timeline.active_fixtures(conn, edition_id, branch_id) and
                {t for f in timeline.active_fixtures(conn, edition_id, branch_id).values() for t in (f.home, f.away)})
        lo = _int(params.get("min", params.get("max")), "The best position", 1, n)
        hi = _int(params.get("max", lo), "The worst position", 1, n)
        if lo > hi:
            lo, hi = hi, lo
        p = {"team": team, "min": lo, "max": hi}
        replace_key = ("team", team)

    # one lock per slot: a new champion replaces the old one, a new score for a match replaces the old score...
    for old in _load(conn, edition_id, branch_id):
        if old.kind == kind and (replace_key is None or old.p.get(replace_key[0]) == replace_key[1]):
            conn.execute("DELETE FROM outcome_locks WHERE lock_id = ?", (old.id,))
    cur = conn.execute("INSERT INTO outcome_locks (branch_id, edition_id, kind, params) VALUES (?,?,?,?)",
                       (branch_id, edition_id, kind, json.dumps(p)))
    conn.commit()
    return {"ok": True, "lock_id": cur.lastrowid}


def delete_lock(conn, branch_id: int, lock_id: int) -> dict:
    conn.execute("DELETE FROM outcome_locks WHERE lock_id = ? AND branch_id = ?", (lock_id, branch_id))
    conn.commit()
    return {"ok": True}


def clear_locks(conn, edition_id: str, branch_id: int) -> dict:
    conn.execute("DELETE FROM outcome_locks WHERE edition_id = ? AND branch_id = ?", (edition_id, branch_id))
    conn.commit()
    return {"ok": True}


def describe(conn, lock: Lock, nm: dict | None = None, tn: dict | None = None) -> str:
    nm, tn = nm or api.names(conn), tn or api.team_names(conn)
    p, k = lock.p, lock.kind
    if k in PODIUM:
        return {"champion": "Champion", "runner_up": "Runner-up", "third": "Third place"}[k] + f": {tn.get(p['team'], p['team'])}"
    if k == "score":
        h, a = p["teams"]
        pens = f" ({p['pen_home']}-{p['pen_away']} on penalties)" if "pen_home" in p else ""
        return f"{tn.get(h, h)} {p['home']}-{p['away']} {tn.get(a, a)}{pens}"
    if k == "player_total":
        bits = ([f"{p['goals']} goals"] if "goals" in p else []) + ([f"{p['assists']} assists"] if "assists" in p else [])
        return f"{nm.get(p['player_id'], p['player_id'])}: {' and '.join(bits)}"
    if k == "golden_boot":
        return f"Top scorer (Golden Boot): {nm.get(p['player_id'], p['player_id'])}"
    t = tn.get(p["team"], p["team"])
    if p["min"] == p["max"]:
        return f"{t} finish {_ordinal(p['min'])}" + (" (champions)" if p["min"] == 1 else "")
    return f"{t} finish {_ordinal(p['min'])} to {_ordinal(p['max'])}" + (" (top " + str(p["max"]) + ")" if p["min"] == 1 else "")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def view(conn, edition_id: str, branch_id: int | None) -> dict:
    fmt = _format(conn, edition_id)
    out = {"format": fmt, "locks": []}
    if branch_id is None:
        return out
    nm, tn = api.names(conn), api.team_names(conn)
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    out["matches"] = [{"fixture_id": f.fixture_id, "home": f.home, "away": f.away,
                       "label": f"{tn.get(f.home, f.home)} v {tn.get(f.away, f.away)}", "stage": f.stage, "date": f.date,
                       "score": f"{f.home_score}-{f.away_score}" if f.played else None}
                      for f in sorted(fx.values(), key=lambda f: (f.date or "", f.fixture_id)) if f.home and f.away]
    ids = {t for f in fx.values() for t in (f.home, f.away) if t}
    out["teams"] = sorted(({"team": t, "name": tn.get(t, t)} for t in ids), key=lambda x: x["name"])
    out["locks"] = [{"lock_id": l.id, "kind": l.kind, "text": describe(conn, l, nm, tn), "params": l.p}
                    for l in _load(conn, edition_id, branch_id)]
    return out


# ------------------------------------------------------------------ saving one match

def _save(conn, edition_id, branch_id, f, lock, rng, origin=None):
    """Roll one match (or build it around an exact-score lock) and save it."""
    seed = rng.randrange(1 << 30)
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    if lock is not None:
        c = {"home_score": lock.p["home"], "away_score": lock.p["away"], "keep_real": True}
        if "pen_home" in lock.p:
            c["pen_home"], c["pen_away"] = lock.p["pen_home"], lock.p["pen_away"]
        payload = roller.roll_with(conn, edition_id, branch_id, f, c, seed=seed)
    else:
        payload = roller.roll(conn, edition_id, branch_id, f, seed=seed)
    api._validate(f, payload, timeline.tie_rule(conn, edition_id, branch_id, f, fx))
    timeline.set_result(conn, branch_id, f, payload["home_score"], payload["away_score"], events=payload["events"],
                        lineup=payload["lineup"], extra_time=int(payload["extra_time"]),
                        pen_home=payload["pen_home"], pen_away=payload["pen_away"],
                        status="overridden" if lock is not None else "generated",
                        origin=origin or ("outcome" if lock is not None else "second_order_generated"),
                        reason="outcome lock" if lock is not None else "rolled", edition_id=edition_id)
    return payload


def _matches_score(f, lock) -> bool:
    if f.home_score is None or (f.home_score, f.away_score) != (lock.p["home"], lock.p["away"]):
        return False
    return "pen_home" not in lock.p or (f.pen_home, f.pen_away) == (lock.p["pen_home"], lock.p["pen_away"])


def _apply_scores(conn, edition_id, branch_id, score_locks, rng) -> None:
    """Save every locked score whose match exists right now with the teams it was locked for."""
    for fid, lock in score_locks.items():
        fx = timeline.active_fixtures(conn, edition_id, branch_id)
        f = fx.get(fid)
        if f is None or [f.home, f.away] != lock.p["teams"]:
            continue
        if f.played and _matches_score(f, lock):
            continue
        _save(conn, edition_id, branch_id, f, lock, rng)


# ------------------------------------------------------------------ two-legged ties and slots

def _slot_fixtures(fx: dict, s) -> list:
    todo = sorted((f for f in fx.values() if f.match_no == s.match_no and (s.legs == 1 or f.leg)
                   and f.home_score is None and {f.home, f.away} == {s.home, s.away}), key=lambda f: f.leg or 0)
    if not todo:
        todo = [next(f for f in fx.values() if f.match_no == s.match_no)]
    return todo


def _roll_slot(conn, edition_id, branch_id, s, fx, rng, score_locks) -> None:
    for f in _slot_fixtures(fx, s):                     # leg 1 is saved before leg 2 is rolled
        fresh = timeline.active_fixtures(conn, edition_id, branch_id)[f.fixture_id]
        _save(conn, edition_id, branch_id, fresh, score_locks.get(f.fixture_id), rng)


def _roll_all_pending(conn, edition_id, branch_id, rng, score_locks) -> None:
    for _ in range(40):
        res = engine.apply_cascade(conn, edition_id, branch_id)
        pending = [s for s in res.slots if s.status == "pending"]
        if not pending:
            return
        fx = timeline.active_fixtures(conn, edition_id, branch_id)
        for s in pending:
            _roll_slot(conn, edition_id, branch_id, s, fx, rng, score_locks)
    raise EditError("Some knockout matches still have no result after rolling; try again")


def _winner_of(conn, edition_id, branch_id, f, lock, rng) -> str | None:
    f2 = timeline.active_fixtures(conn, edition_id, branch_id)[f.fixture_id]
    return f2.winner()


def _force_single(conn, edition_id, branch_id, s, fx, team, rng, score_locks) -> bool:
    f = next(x for x in fx.values() if x.match_no == s.match_no)
    lock = score_locks.get(f.fixture_id)
    if lock is not None and [f.home, f.away] == lock.p["teams"]:
        if not (f.played and _matches_score(f, lock)):
            _save(conn, edition_id, branch_id, f, lock, rng)
            f = timeline.active_fixtures(conn, edition_id, branch_id)[f.fixture_id]
            changed = True
        else:
            changed = False
        if f.winner() != team:
            names = api.team_names(conn)
            raise EditError(f"{names.get(f.home, f.home)} {f.home_score}-{f.away_score} {names.get(f.away, f.away)} "
                            f"is locked, so {names.get(team, team)} can't win that match (it is on their route).")
        return changed
    return api._force_winner(conn, edition_id, branch_id, f, team, rng.randrange(1 << 30))


def _through(f1, f2, away_goals: bool, team) -> bool:
    return f1.home_score is not None and f2.home_score is not None and engine._aggregate(f1, f2, away_goals).agg_winner == team


def _force_through(conn, edition_id, branch_id, s, team, rng, score_locks) -> bool:
    """Make `team` win a two-legged tie: keep what's there if it already goes through, else re-roll the legs."""
    away_goals = bool(db.get_ruleset(conn, edition_id)["knockout"].get("away_goals"))
    tn = api.team_names(conn)
    res = engine.resolve(conn, edition_id, branch_id)
    cur = res.slot(s.match_no)
    if cur.status == "valid" and cur.fixture is not None and cur.fixture.agg_winner == team:
        return False

    def legs():
        fx = timeline.active_fixtures(conn, edition_id, branch_id)
        ls = sorted((f for f in fx.values() if f.match_no == s.match_no and f.leg), key=lambda f: f.leg)
        if len(ls) != 2:
            raise EditError("Couldn't find both legs of that tie")
        return ls

    for attempt in range(14):
        l1, l2 = legs()
        k1, k2 = score_locks.get(l1.fixture_id), score_locks.get(l2.fixture_id)
        if k1 is not None:
            if not (l1.played and _matches_score(l1, k1)):
                _save(conn, edition_id, branch_id, l1, k1, rng)
        elif attempt > 0 or not l1.played:
            _save(conn, edition_id, branch_id, l1, None, rng, origin="picked")
        l1, l2 = legs()
        if k2 is not None:
            if not (l2.played and _matches_score(l2, k2)):
                _save(conn, edition_id, branch_id, l2, k2, rng)
            l1, l2 = legs()
            if _through(l1, l2, away_goals, team):
                return True
            if k1 is not None:
                raise EditError(f"Both legs of the {tn.get(s.home, s.home)}-{tn.get(s.away, s.away)} tie are locked and "
                                f"{tn.get(team, team)} doesn't go through on those scores.")
            continue
        for _ in range(30):
            p = roller.roll(conn, edition_id, branch_id, l2, seed=rng.randrange(1 << 30))
            trial = replace(l2, home_score=p["home_score"], away_score=p["away_score"], pen_home=p["pen_home"],
                            pen_away=p["pen_away"], extra_time=int(p["extra_time"]))
            if _through(l1, trial, away_goals, team):
                api._validate(l2, p, timeline.tie_rule(conn, edition_id, branch_id, l2))
                timeline.set_result(conn, branch_id, l2, p["home_score"], p["away_score"], events=p["events"],
                                    lineup=p["lineup"], extra_time=int(p["extra_time"]), pen_home=p["pen_home"],
                                    pen_away=p["pen_away"], status="generated", origin="picked",
                                    reason=f"tie decided: {team} through", edition_id=edition_id)
                return True
    # a big underdog: a 1-0 first-leg win and a goalless second leg
    l1, l2 = legs()
    if score_locks.get(l1.fixture_id) or score_locks.get(l2.fixture_id):
        raise EditError(f"With the locked score(s), {tn.get(team, team)} can't be made to go through that tie.")
    for f, (h, a) in ((l1, (1, 0) if l1.home == team else (0, 1)), (l2, (0, 0))):
        fresh = next(x for x in timeline.active_fixtures(conn, edition_id, branch_id).values() if x.fixture_id == f.fixture_id)
        fake = Lock(0, "score", {"home": h, "away": a, "teams": [fresh.home, fresh.away]})
        _save(conn, edition_id, branch_id, fresh, fake, rng, origin="picked")
    return True


def _make_force(score_locks):
    def force(conn, edition_id, branch_id, s, fx, team, rng):
        if s.legs == 2:
            return _force_through(conn, edition_id, branch_id, s, team, rng, score_locks)
        return _force_single(conn, edition_id, branch_id, s, fx, team, rng, score_locks)
    return force


def _make_roll(score_locks):
    def roll(conn, edition_id, branch_id, s, fx, rng):
        _roll_slot(conn, edition_id, branch_id, s, fx, rng, score_locks)
    return roll


# ------------------------------------------------------------------ league positions

def _table(conn, edition_id, branch_id) -> list:
    return list(engine.resolve(conn, edition_id, branch_id).tables["A"])


def _cheapest_score(h, a, want: str):
    """The nearest score to h-a in which the home side wins / draws / loses (fewest goals added or removed)."""
    if want == "win":
        opts = [(a + 1, a), (h, h - 1)] if h >= 1 else [(a + 1, a)]
    elif want == "draw":
        opts = [(h, h), (a, a)]
    else:
        opts = [(h, h + 1), (a - 1, a)] if a >= 1 else [(h, h + 1)]
    return min((o for o in opts if o[0] >= 0 and o[1] >= 0), key=lambda o: abs(o[0] - h) + abs(o[1] - a))


def _fit_league(conn, edition_id, branch_id, locks, rng, protected: dict) -> list:
    problems = []
    wants = [l for l in locks if l.kind == "league_position"]
    if not wants:
        return problems
    tn = api.team_names(conn)
    for _ in range(160):
        rows = _table(conn, edition_id, branch_id)
        pos = {r.team: i + 1 for i, r in enumerate(rows)}
        unmet = [l for l in wants if not l.p["min"] <= pos[l.p["team"]] <= l.p["max"]]
        if not unmet:
            return problems
        l = unmet[0]
        team, lo, hi = l.p["team"], l.p["min"], l.p["max"]
        too_low = pos[team] > hi
        fx = timeline.active_fixtures(conn, edition_id, branch_id)
        cands = []
        for f in fx.values():
            if f.stage != "group" or team not in (f.home, f.away) or not f.played or f.fixture_id in protected:
                continue
            mine, theirs = (f.home_score, f.away_score) if f.home == team else (f.away_score, f.home_score)
            opp = f.away if f.home == team else f.home
            if too_low and mine <= theirs:
                swing = (3 if mine < theirs else 2) + (1.5 if pos[opp] < pos[team] else 0) + rng.random() * 0.5
                cands.append((swing, f, "win"))
            elif not too_low and mine >= theirs:
                swing = (3 if mine > theirs else 1) + (1.5 if pos[opp] > pos[team] and pos[opp] <= hi + 2 else 0) + rng.random() * 0.5
                cands.append((swing, f, "draw" if mine > theirs else "lose"))
        if not cands:
            break
        _, f, mode = max(cands, key=lambda c: c[0])
        want = "win" if mode == "win" else mode
        # the wanted result is for `team`; convert to a home/away score line
        h, a = f.home_score, f.away_score
        if f.home == team:
            nh, na = _cheapest_score(h, a, want)
        else:
            na, nh = _cheapest_score(a, h, want)
        fake = Lock(0, "score", {"home": nh, "away": na, "teams": [f.home, f.away]})
        _save(conn, edition_id, branch_id, f, fake, rng, origin="outcome")
    rows = _table(conn, edition_id, branch_id)
    pos = {r.team: i + 1 for i, r in enumerate(rows)}
    for l in wants:
        if not l.p["min"] <= pos[l.p["team"]] <= l.p["max"]:
            problems.append({"locks": [l.id], "message": (
                f"{tn.get(l.p['team'])} can't be moved to {_ordinal(l.p['min'])}"
                f"{'' if l.p['min'] == l.p['max'] else ' to ' + _ordinal(l.p['max'])} without changing matches that are "
                f"locked, or that you've asked for elsewhere. Best I could reach: {_ordinal(pos[l.p['team']])}.")})
    return problems


def _static_league_checks(conn, locks) -> list:
    wants = [l for l in locks if l.kind == "league_position"]
    problems, tn = [], api.team_names(conn)
    for k in range(1, 40):
        top = [l for l in wants if l.p["max"] <= k]
        if len(top) > k:
            problems.append({"locks": [l.id for l in top], "message": (
                f"{len(top)} teams are locked into the top {k} place{'s' if k > 1 else ''}, but there are only {k}.")})
            break
    return problems


# ------------------------------------------------------------------ player goals and assists

def _window(lineup, pid, length=120):
    l = next((x for x in lineup if x["player_id"] == pid), None)
    if l is None:
        return None
    on = 0 if l["started"] else (l["minute_on"] or 0)
    off = l["minute_off"] if l["minute_off"] is not None else length
    return on, off


def _on_pitch(f, pid, minute) -> bool:
    w = _window(f.lineup, pid)
    return w is not None and w[0] <= minute <= max(w[1], w[0])


def _team_of(f, pid):
    l = next((x for x in f.lineup if x["player_id"] == pid), None)
    return l["team_id"] if l else None


def _counts(played, what: str) -> dict:
    out = {}
    for f in played:
        for e in f.events:
            if e["event_type"] in GOALS:
                key = e.get("player_id") if what == "goals" else e.get("assist_player_id")
                if key:
                    out[key] = out.get(key, 0) + 1
    return out


def _fit_players(conn, edition_id, branch_id, locks, rng) -> list:
    wants = {}
    for l in locks:
        if l.kind == "player_total":
            w = wants.setdefault(l.p["player_id"], {"goals": None, "assists": None, "lead": False, "ids": []})
            w["goals"], w["assists"] = l.p.get("goals", w["goals"]), l.p.get("assists", w["assists"])
            w["ids"].append(l.id)
        elif l.kind == "golden_boot":
            w = wants.setdefault(l.p["player_id"], {"goals": None, "assists": None, "lead": False, "ids": []})
            w["lead"] = True
            w["ids"].append(l.id)
    if not wants:
        return []
    nm = api.names(conn)
    problems = []
    played, _ = stats.counted_fixtures(conn, edition_id, branch_id)
    before = {f.fixture_id: json.dumps(f.events, sort_keys=True) for f in played}
    info = roller._player_info(conn, edition_id)
    frozen = set()

    def weight(pid):
        i = info.get(pid, {"pos": "MF", "g90": 0.1})
        return roller.POS_GOAL.get(i.get("pos", "MF"), 1.0) + 6 * i.get("g90", 0.0)

    def take_candidates(pid, what, avoid=()):
        """Goals (what='goals') or assists by team-mates that `pid` could take over, from matches he's in."""
        out = []
        for f in played:
            team = _team_of(f, pid)
            if team is None:
                continue
            for e in f.events:
                if e["event_type"] not in GOALS or e["team_id"] != team or e.get("player_id") == pid:
                    continue
                if not _on_pitch(f, pid, e["minute"]):
                    continue
                if what == "goals":
                    if e.get("player_id") in frozen:
                        continue
                    out.append((f, e))
                elif e.get("assist_player_id") != pid and e.get("assist_player_id") not in frozen:
                    out.append((f, e))
        rng.shuffle(out)
        return out

    def give_away(pid, what, protect=()):
        """Move one of pid's goals/assists to a team-mate who is on the pitch at that minute."""
        mine = []
        for f in played:
            for e in f.events:
                if e["event_type"] in GOALS and (e.get("player_id") if what == "goals" else e.get("assist_player_id")) == pid:
                    mine.append((f, e))
        rng.shuffle(mine)
        for f, e in mine:
            mates = [l["player_id"] for l in f.lineup if l["team_id"] == e["team_id"] and l["player_id"] != pid
                     and l["player_id"] not in frozen and l["player_id"] not in protect
                     and _on_pitch(f, l["player_id"], e["minute"])
                     and (what == "assists" or l["player_id"] != e.get("assist_player_id"))
                     and (what == "goals" or l["player_id"] != e.get("player_id"))]
            if what == "assists" and not mates:
                e["assist_player_id"] = None
                return True
            if mates:
                pick = rng.choices(mates, weights=[weight(m) for m in mates])[0]
                if what == "goals":
                    e["player_id"] = pick
                    if e.get("assist_player_id") == pick:
                        e["assist_player_id"] = None
                else:
                    e["assist_player_id"] = pick
                return True
        return False

    def make_exact(pid, what, n):
        cur = _counts(played, what).get(pid, 0)
        word = "goals" if what == "goals" else "assists"
        if cur > n:
            for _ in range(cur - n):
                if not give_away(pid, what):
                    problems.append({"locks": wants[pid]["ids"], "message": (
                        f"Couldn't take {cur - n} {word} away from {nm.get(pid)}: no team-mate was on the pitch to take them.")})
                    return
        elif cur < n:
            cands = take_candidates(pid, what)
            if len(cands) < n - cur:
                problems.append({"locks": wants[pid]["ids"], "message": (
                    f"{nm.get(pid)} can reach at most {cur + len(cands)} {word}: his team only scores "
                    f"{cur + len(cands)} {'goals' if what == 'goals' else 'goals with assists'} while he's on the pitch. "
                    f"You asked for {n}. Lock more of his team's scores, or lower the number.")})
                return
            for f, e in cands[: n - cur]:
                if what == "goals":
                    e["player_id"] = pid
                    if e.get("assist_player_id") == pid:
                        e["assist_player_id"] = None
                else:
                    e["assist_player_id"] = pid

    for pid, w in wants.items():
        if w["goals"] is not None:
            make_exact(pid, "goals", w["goals"])
            frozen.add(pid)
    for pid, w in wants.items():
        if w["assists"] is not None:
            make_exact(pid, "assists", w["assists"])
    for pid, w in wants.items():
        if not w["lead"]:
            continue
        for _ in range(400):
            goals = _counts(played, "goals")
            mine = goals.get(pid, 0)
            rivals = {q: g for q, g in goals.items() if q != pid}
            top = max(rivals.values(), default=0)
            if mine > top and mine > 0:
                break
            if w["goals"] is None:
                cands = take_candidates(pid, "goals")
                if cands:
                    # prefer taking from the leading rival
                    cands.sort(key=lambda c: -goals.get(c[1].get("player_id"), 0))
                    f, e = cands[0]
                    e["player_id"] = pid
                    if e.get("assist_player_id") == pid:
                        e["assist_player_id"] = None
                    continue
            leader = next((q for q, g in sorted(rivals.items(), key=lambda kv: -kv[1]) if q not in frozen), None)
            if leader is None or not give_away(leader, "goals", protect={pid}):
                problems.append({"locks": w["ids"], "message": (
                    f"{nm.get(pid)} can't be made top scorer: he has {mine} and {nm.get(max(rivals, key=rivals.get)) if rivals else 'nobody'} "
                    f"has {top}, and there aren't enough of his team's goals (in the matches he plays) to close the gap.")})
                break
        frozen.add(pid)

    changed = [f for f in played if json.dumps(f.events, sort_keys=True) != before[f.fixture_id]]
    for f in changed:
        timeline.set_result(conn, branch_id, f, f.home_score, f.away_score, events=f.events, lineup=f.lineup,
                            extra_time=f.extra_time, pen_home=f.pen_home, pen_away=f.pen_away,
                            status=f.status if f.status in ("generated", "overridden") else "overridden",
                            origin=f.origin if f.origin not in ("baseline", "reused_historical", "suspension_fix", "cascade")
                            else "outcome", reason="outcome lock: goals moved between team-mates", edition_id=edition_id)
    return problems


# ------------------------------------------------------------------ checking and running

def _verify(conn, edition_id, branch_id, locks) -> list:
    res = engine.resolve(conn, edition_id, branch_id)
    st = stats.edition_stats(conn, edition_id, branch_id)
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    nm, tn = api.names(conn), api.team_names(conn)
    out = []

    def bad(l, why):
        out.append({"locks": [l.id], "message": f"{describe(conn, l, nm, tn)}: {why}"})
    rows = res.tables.get("A", []) if _format(conn, edition_id) == "league" else []
    pos = {r.team: i + 1 for i, r in enumerate(rows)}
    for l in locks:
        p = l.p
        if l.kind == "champion" and res.champion != p["team"]:
            bad(l, f"not met (the champion is {tn.get(res.champion, res.champion)}).")
        elif l.kind == "runner_up" and res.runner_up != p["team"]:
            bad(l, "not met.")
        elif l.kind == "third" and res.third != p["team"]:
            bad(l, "not met.")
        elif l.kind == "score":
            f = fx.get(p["fixture_id"])
            if f is None or [f.home, f.away] != p["teams"]:
                bad(l, "that match no longer exists as locked: the results around it changed who plays in it. "
                       "Lock the teams' route (e.g. a champion) so the same pairing happens, or remove this lock.")
            elif not (f.played and _matches_score(f, l)):
                bad(l, "not met.")
        elif l.kind == "player_total":
            line = st.players.get(p["player_id"])
            g, a = (line.goals, line.assists) if line else (0, 0)
            if ("goals" in p and g != p["goals"]) or ("assists" in p and a != p["assists"]):
                bad(l, f"not met (he has {g} goals and {a} assists).")
        elif l.kind == "golden_boot":
            if st.awards.get("golden_boot", [None])[:1] != [p["player_id"]]:
                bad(l, "not met.")
        elif l.kind == "league_position":
            if not p["min"] <= pos.get(p["team"], 0) <= p["max"]:
                bad(l, f"not met (they finish {_ordinal(pos.get(p['team'], 0))}).")
    return out


def run(conn, edition_id: str, branch_id: int, apply: bool = False, seed: int | None = None) -> dict:
    """Make every lock true and roll the rest. Preview (apply=False) never keeps anything."""
    if branch_id is None:
        raise EditError("Pick a branch first")
    locks = _load(conn, edition_id, branch_id)
    if not locks:
        raise EditError("Add at least one lock first")
    seed = random.randrange(1 << 30) if seed in (None, "") else int(seed)
    rng = random.Random(seed)
    tx = _Tx(conn)
    nm, tn = api.names(conn), api.team_names(conn)
    descr = [describe(conn, l, nm, tn) for l in locks]
    try:
        before = api.snapshot(tx, edition_id, branch_id)
        problems = _execute(tx, edition_id, branch_id, locks, rng)
        fixes = []
        if not problems:
            fixes = engine.apply_cascade(tx, edition_id, branch_id).fixes
            api._log_displacements(tx, edition_id, branch_id)
            problems = _verify(tx, edition_id, branch_id, locks)
        if problems:
            conn.rollback()
            return {"ok": False, "applied": False, "seed": seed, "contradictions": problems, "locks": descr}
        d = api.diff(tx, before, api.snapshot(tx, edition_id, branch_id), fixes)
        changed = conn.execute("SELECT COUNT(*) FROM fixture_overrides WHERE branch_id = ? AND edition_id = ?",
                               (branch_id, edition_id)).fetchone()[0]
        if apply:
            conn.commit()
        else:
            conn.rollback()
        return {"ok": True, "applied": bool(apply), "seed": seed, "diff": d, "locks": descr, "overrides": changed}
    except EditError as e:
        conn.rollback()
        return {"ok": False, "applied": False, "seed": seed, "locks": descr,
                "contradictions": [{"locks": [], "message": str(e)}]}
    except Exception:
        conn.rollback()
        raise


def _execute(conn, edition_id, branch_id, locks, rng) -> list:
    fmt = _format(conn, edition_id)
    score_locks = {l.p["fixture_id"]: l for l in locks if l.kind == "score"}
    podium = {l.kind: l.p["team"] for l in locks if l.kind in PODIUM}
    problems = _static_league_checks(conn, locks)
    if problems:
        return problems
    if podium and "champion" not in podium:
        return [{"locks": [], "message": "A runner-up or third-place lock needs a champion lock too."}]
    taken = [t for t in podium.values()]
    if len(set(taken)) != len(taken):
        return [{"locks": [], "message": "The same team can't fill two podium places."}]

    _apply_scores(conn, edition_id, branch_id, score_locks, rng)
    engine.apply_cascade(conn, edition_id, branch_id)
    if podium:
        api.pick_podium(conn, edition_id, branch_id, podium["champion"], podium.get("runner_up"), podium.get("third"),
                        seed=rng.randrange(1 << 30), force=_make_force(score_locks), roll=_make_roll(score_locks),
                        require_second=False)
    if fmt != "league":
        _roll_all_pending(conn, edition_id, branch_id, rng, score_locks)
        _apply_scores(conn, edition_id, branch_id, score_locks, rng)
    problems = _fit_league(conn, edition_id, branch_id, locks, rng, score_locks) if fmt == "league" else []
    if problems:
        return problems
    return _fit_players(conn, edition_id, branch_id, locks, rng)
