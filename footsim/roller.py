"""Single-match generator.

Strength: each team's Elo going into the tournament (no hindsight), plus home
advantage where it applies, plus an optional bonus per team (the created
player's impact, Phase 7).
Scoreline: Poisson goals from the fitted model; knockout draws go to extra
time (a third of the rate) and then a kick-by-kick shootout.
Events: scorers, assists, penalties, own goals and cards drawn from the actual
lineup on the pitch, weighted by position and each player's real scoring rate.
Suspended players are left out of the lineup.
"""
import math
import random
from collections import defaultdict

from footsim import db, engine, ratings, timeline
from footsim.timeline import Fixture

POS_GOAL = {"FW": 4.0, "MF": 1.6, "DF": 0.55, "GK": 0.01}
POS_ASSIST = {"FW": 2.0, "MF": 3.0, "DF": 1.4, "GK": 0.03}
POS_CARD = {"FW": 0.8, "MF": 1.3, "DF": 1.4, "GK": 0.25}
P_ASSIST = 0.72          # share of goals with an assist
P_PENALTY = 0.10         # share of goals from the spot
P_PEN_MISS = 0.25        # penalties awarded that are missed
P_OWN_GOAL = 0.025
YELLOWS_PER_TEAM = 1.7   # WC 2022 average
P_SECOND_YELLOW = 0.03   # per booked player
REDS_PER_TEAM = 0.015
SHOOTOUT_CONVERSION = 0.74
SUBS = 5


def _poisson(rng: random.Random, lam: float) -> int:
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def _pick(rng: random.Random, weighted: list):
    total = sum(w for _, w in weighted)
    if total <= 0:
        return weighted[0][0] if weighted else None
    r = rng.uniform(0, total)
    for item, w in weighted:
        r -= w
        if r <= 0:
            return item
    return weighted[-1][0]


# ------------------------------------------------------------------ inputs

_INFO_CACHE = {}


def _player_info(conn, edition_id: str) -> dict:
    """position, goals/90 and penalty record from the real edition (a talent proxy). Cached."""
    key = (edition_id, conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
           conn.execute("PRAGMA database_list").fetchone()[2])
    if key not in _INFO_CACHE:
        _INFO_CACHE[key] = _player_info_uncached(conn, edition_id)
    return {k: dict(v) for k, v in _INFO_CACHE[key].items()}


def _player_info_uncached(conn, edition_id: str) -> dict:
    info = {}
    for r in conn.execute("SELECT player_id, position FROM players"):
        info[r[0]] = {"pos": r[1] or "MF", "g90": 0.0, "a90": 0.0, "pens": 0}
    mins = defaultdict(int)
    for r in conn.execute("SELECT l.player_id, l.started, l.minute_on, l.minute_off, f.extra_time FROM lineups l "
                          "JOIN fixtures f USING(fixture_id) WHERE f.edition_id = ?", (edition_id,)):
        end = 120 if r["extra_time"] else 90
        on = 0 if r["started"] else (r["minute_on"] or 0)
        mins[r["player_id"]] += max(0, min(r["minute_off"] or end, end) - on)
    goals, assists, pens = defaultdict(int), defaultdict(int), defaultdict(int)
    for r in conn.execute("SELECT e.player_id, e.event_type, e.assist_player_id FROM match_events e "
                          "JOIN fixtures f USING(fixture_id) WHERE f.edition_id = ?", (edition_id,)):
        if r["event_type"] in ("goal", "pen_goal"):
            goals[r["player_id"]] += 1
            if r["assist_player_id"]:
                assists[r["assist_player_id"]] += 1
        if r["event_type"] in ("pen_goal", "pen_miss"):
            pens[r["player_id"]] += 1
    roles = defaultdict(list)
    for r in conn.execute("SELECT player_id, role FROM player_positions ORDER BY minutes DESC"):
        roles[r[0]].append(r[1])
    for pid, i in info.items():
        i["roles"] = tuple(roles.get(pid, ()))
        m = max(mins[pid], 270)              # shrink small samples toward the position average
        i["g90"] = goals[pid] * 90 / m
        i["a90"] = assists[pid] * 90 / m
        i["pens"] = pens[pid]
    return info


def suspended_for(conn, edition_id: str, branch_id: int | None, fixture: Fixture) -> set:
    """Players banned for this fixture, from cards in matches played before it."""
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    res = engine.resolve(conn, edition_id, branch_id)
    valid = engine.played_fixture_ids(res)
    before = {k: f for k, f in fx.items() if f.played and f.date < fixture.date
              and (f.stage == "group" or k in valid)}
    probe = Fixture("__probe__", fixture.stage, fixture.grp, None, fixture.date, fixture.home, fixture.away, 0, 0)
    before[probe.fixture_id] = probe
    out = engine.Resolution(edition_id, {}, [])
    engine._apply_suspensions(out, before, db.get_ruleset(conn, edition_id), include_all=True)
    return {p for ps in out.suspensions.get("__probe__", {}).values() for p in ps}


def build_lineup(conn, edition_id: str, branch_id: int | None, fixture: Fixture, team: str,
                 banned: set, info: dict, rng: random.Random, length: int) -> list:
    """Starting XI from the team's last match (minus banned players), plus substitutions."""
    from footsim.api import default_lineup, squad
    prev = [l for l in (fixture.lineup or []) if l["team_id"] == team] or \
        default_lineup(conn, edition_id, team, branch_id, fixture.date)
    starters = [l["player_id"] for l in prev if l["started"]]
    pool = [p["player_id"] for p in squad(conn, edition_id, team, branch_id)]
    if not starters:                                   # no history at all: pick by position
        by = defaultdict(list)
        for p in pool:
            by[info.get(p, {}).get("pos", "MF")].append(p)
        starters = by["GK"][:1] + by["DF"][:4] + by["MF"][:3] + by["FW"][:3]
    xi = []
    for p in starters:
        if p in banned:
            pos = info.get(p, {}).get("pos", "MF")
            alt = [q for q in pool if q not in starters and q not in banned and q not in xi
                   and info.get(q, {}).get("pos") == pos] or \
                  [q for q in pool if q not in starters and q not in banned and q not in xi
                   and info.get(q, {}).get("pos") != "GK"]
            if alt:
                xi.append(alt[0])
        else:
            xi.append(p)
    bench = [p for p in pool if p not in xi and p not in banned and info.get(p, {}).get("pos") != "GK"]
    lineup = {p: {"team_id": team, "player_id": p, "started": 1, "minute_on": 0, "minute_off": None} for p in xi}
    outfield = [p for p in xi if info.get(p, {}).get("pos") != "GK"]
    rng.shuffle(bench)
    used_on = set()
    for _ in range(min(SUBS, len(bench))):
        off = _pick(rng, [(p, 1.0 + 1.5 * (info[p]["pos"] == "FW")) for p in outfield if lineup[p]["minute_off"] is None])
        if off is None:
            break
        minute = rng.randint(55, min(88, length - 2)) if length == 90 else rng.randint(55, 105)
        on = next((b for b in bench if b not in used_on and info.get(b, {}).get("pos") == info[off]["pos"]), None) \
            or next((b for b in bench if b not in used_on), None)
        if on is None:
            break
        used_on.add(on)
        lineup[off]["minute_off"] = minute
        lineup[on] = {"team_id": team, "player_id": on, "started": 0, "minute_on": minute, "minute_off": None}
    return list(lineup.values())


def _on_pitch(lineup: list, team: str, minute: int) -> list:
    return [l["player_id"] for l in lineup if l["team_id"] == team
            and (0 if l["started"] else l["minute_on"]) <= minute
            and (l["minute_off"] is None or l["minute_off"] > minute)]


# ------------------------------------------------------------------ the roll

def roll(conn, edition_id: str, branch_id: int | None, fixture: Fixture, *, seed: int | None = None,
         bonus: dict | None = None, info: dict | None = None) -> dict:
    """A complete, valid result for one fixture, ready for api.edit_fixture."""
    from footsim import player as cp
    rng = random.Random(seed)
    info = dict(info or _player_info(conn, edition_id))
    bonus = dict(bonus or {})
    start = conn.execute("SELECT start_date FROM editions WHERE edition_id = ?", (edition_id,)).fetchone()[0]
    tie = timeline.tie_rule(conn, edition_id, branch_id, fixture)

    banned = suspended_for(conn, edition_id, branch_id, fixture)
    lineup = (build_lineup(conn, edition_id, branch_id, fixture, fixture.home, banned, info, rng, 90) +
              build_lineup(conn, edition_id, branch_id, fixture, fixture.away, banned, info, rng, 90))

    me = cp.profile(conn, branch_id, edition_id)
    if me is not None and me.team_id in (fixture.home, fixture.away):
        from footsim.api import squad
        info[me.player_id] = cp.scoring_info(me)
        squad_ids = [x["player_id"] for x in squad(conn, edition_id, me.team_id, branch_id)]
        role = cp.get_role(conn, branch_id, fixture.fixture_id)
        if role["role"] == "auto":       # nothing chosen: play to his tier
            role = ({"role": "start", "target_player_id": None, "minute": None} if me.overall >= 75 else
                    {"role": "sub", "target_player_id": None, "minute": 65} if me.overall >= 65 else
                    {"role": "unused", "target_player_id": None, "minute": None})
        if role["role"] == "inherit":
            role = {"role": "start", "target_player_id": None, "minute": None}
        if me.player_id in banned or cp.injured_on(conn, branch_id, me.player_id, fixture.date):
            role = {"role": "unused", "target_player_id": None, "minute": None}
        lineup, _ = cp.strip(lineup, [], me, squad_ids, info)
        lineup, _ = cp.apply_role(lineup, [], me, role, squad_ids, info)
        mine = next((l for l in lineup if l["player_id"] == me.player_id), None)
        if mine:
            on = 0 if mine["started"] else (mine["minute_on"] or 0)
            share = max(0, (mine["minute_off"] or 90) - on) / 90
            bonus[me.team_id] = bonus.get(me.team_id, 0) + cp.impact_elo(me) * min(1.0, share)

    eh = ratings.rating(conn, fixture.home, start) + bonus.get(fixture.home, 0)
    ea = ratings.rating(conn, fixture.away, start) + bonus.get(fixture.away, 0)
    home_adv = 0 if _neutral(conn, fixture) else ratings.HOME_ADV
    lh, la = ratings.lambdas(eh, ea, home_adv)

    hs, as_ = _poisson(rng, lh), _poisson(rng, la)
    goal_minutes = {fixture.home: sorted(rng.randint(1, 90) for _ in range(hs)),
                    fixture.away: sorted(rng.randint(1, 90) for _ in range(as_))}
    extra_time = False
    if tie.et(hs, as_):
        extra_time = True
        eh_, ea_ = _poisson(rng, lh / 3), _poisson(rng, la / 3)
        goal_minutes[fixture.home] += sorted(rng.randint(91, 120) for _ in range(eh_))
        goal_minutes[fixture.away] += sorted(rng.randint(91, 120) for _ in range(ea_))
        hs, as_ = hs + eh_, as_ + ea_
    length = 120 if extra_time else 90

    events = []
    # cards
    for team in (fixture.home, fixture.away):
        booked = set()
        for _ in range(_poisson(rng, YELLOWS_PER_TEAM * length / 90)):
            minute = rng.randint(5, length)
            on = [p for p in _on_pitch(lineup, team, minute) if p not in booked]
            if not on:
                continue
            p = _pick(rng, [(q, POS_CARD[info[q]["pos"]]) for q in on])
            booked.add(p)
            events.append({"minute": minute, "team_id": team, "player_id": p, "event_type": "yellow",
                           "assist_player_id": None})
            if rng.random() < P_SECOND_YELLOW and minute < length - 3:
                m2 = rng.randint(minute + 1, length)
                events.append({"minute": m2, "team_id": team, "player_id": p, "event_type": "second_yellow",
                               "assist_player_id": None})
                _send_off(lineup, p, m2)
        if rng.random() < REDS_PER_TEAM * length / 90:
            minute = rng.randint(10, length)
            on = [p for p in _on_pitch(lineup, team, minute) if p not in booked]
            if on:
                p = _pick(rng, [(q, POS_CARD[info[q]["pos"]]) for q in on])
                events.append({"minute": minute, "team_id": team, "player_id": p, "event_type": "red",
                               "assist_player_id": None})
                _send_off(lineup, p, minute)

    for team, minutes in goal_minutes.items():
        opp = fixture.away if team == fixture.home else fixture.home
        for minute in minutes:
            if rng.random() < P_OWN_GOAL:
                defenders = [(p, 3.0 if info[p]["pos"] == "DF" else 0.5) for p in _on_pitch(lineup, opp, minute)]
                events.append({"minute": minute, "team_id": opp, "player_id": _pick(rng, defenders),
                               "event_type": "own_goal", "assist_player_id": None})
                continue
            on = _on_pitch(lineup, team, minute)
            if rng.random() < P_PENALTY:
                taker = max(on, key=lambda p: (info[p]["pens"], info[p]["g90"], POS_GOAL[info[p]["pos"]]))
                events.append({"minute": minute, "team_id": team, "player_id": taker,
                               "event_type": "pen_goal", "assist_player_id": None})
                continue
            scorer = _pick(rng, [(p, POS_GOAL[info[p]["pos"]] + 6 * info[p]["g90"]) for p in on])
            assist = None
            if rng.random() < P_ASSIST:
                assist = _pick(rng, [(p, POS_ASSIST[info[p]["pos"]] + 8 * info[p]["a90"]) for p in on if p != scorer])
            events.append({"minute": minute, "team_id": team, "player_id": scorer,
                           "event_type": "goal", "assist_player_id": assist})

    # missed penalties (don't change the score)
    for team, lam in ((fixture.home, lh), (fixture.away, la)):
        if rng.random() < lam * P_PENALTY * P_PEN_MISS / (1 - P_PEN_MISS):
            minute = rng.randint(1, length)
            on = _on_pitch(lineup, team, minute)
            taker = max(on, key=lambda p: (info[p]["pens"], info[p]["g90"]))
            events.append({"minute": minute, "team_id": team, "player_id": taker,
                           "event_type": "pen_miss", "assist_player_id": None})

    pen_home = pen_away = None
    if tie.pens(hs, as_):
        pen_home, pen_away, kicks = _shootout(rng, fixture, lineup, info, length)
        events += kicks

    events.sort(key=lambda e: e["minute"])
    return {"home_score": hs, "away_score": as_, "extra_time": extra_time,
            "pen_home": pen_home, "pen_away": pen_away, "events": events, "lineup": lineup,
            "expected_goals": [round(lh, 2), round(la, 2)], "elo": [round(eh), round(ea)]}


def _send_off(lineup: list, player: str, minute: int) -> None:
    for l in lineup:
        if l["player_id"] == player:
            l["minute_off"] = minute


def _shootout(rng, fixture, lineup, info, length):
    takers = {}
    for team in (fixture.home, fixture.away):
        on = _on_pitch(lineup, team, length)
        on.sort(key=lambda p: (-info[p]["pens"], -info[p]["g90"], info[p]["pos"] == "GK"))
        takers[team] = on or ["?"]
    score = {fixture.home: 0, fixture.away: 0}
    kicks, i = [], 0
    while True:
        for team in (fixture.home, fixture.away):
            p = takers[team][i % len(takers[team])]
            scored = rng.random() < SHOOTOUT_CONVERSION
            score[team] += scored
            kicks.append({"minute": 120, "team_id": team, "player_id": None if p == "?" else p,
                          "event_type": "shootout_score" if scored else "shootout_miss", "assist_player_id": None})
        i += 1
        h, a = score[fixture.home], score[fixture.away]
        if i < 5:
            left = 5 - i
            if h > a + left or a > h + left:
                break
        elif h != a:
            break
    return score[fixture.home], score[fixture.away], kicks


def _neutral(conn, fixture: Fixture) -> bool:
    row = conn.execute("SELECT neutral_venue FROM fixtures WHERE fixture_id = ?", (fixture.fixture_id,)).fetchone()
    if row is not None:
        return bool(row[0])
    host = conn.execute("SELECT 1 FROM fixtures WHERE neutral_venue = 0 AND (home_team_id = ? OR away_team_id = ?) "
                        "LIMIT 1", (fixture.home, fixture.home)).fetchone()
    return host is None


# ------------------------------------------------------------------ save helpers

def roll_and_save(conn, edition_id: str, branch_id: int, fixture_id: str, seed: int | None = None,
                  origin: str = "rolled") -> dict:
    from footsim import api
    fx = timeline.active_fixtures(conn, edition_id, branch_id)
    f = fx[fixture_id]
    payload = roll(conn, edition_id, branch_id, f, seed=seed)
    api._validate(f, payload, timeline.tie_rule(conn, edition_id, branch_id, f, fx))
    timeline.set_result(conn, branch_id, f, payload["home_score"], payload["away_score"],
                        events=payload["events"], lineup=payload["lineup"],
                        extra_time=int(payload["extra_time"]), pen_home=payload["pen_home"],
                        pen_away=payload["pen_away"], status="generated", origin=origin,
                        reason=f"rolled: Elo {payload['elo'][0]} v {payload['elo'][1]}", edition_id=edition_id)
    return payload


def roll_pending(conn, edition_id: str, branch_id: int, seed: int | None = None) -> list:
    """Roll every match that needs a result, round by round, until nothing is pending."""
    rng = random.Random(seed)
    rolled = []
    for _ in range(20):
        res = engine.apply_cascade(conn, edition_id, branch_id)
        pending = [s for s in res.slots if s.status == "pending"]
        if not pending:
            break
        fx = timeline.active_fixtures(conn, edition_id, branch_id)
        for s in pending:
            todo = sorted((f for f in fx.values() if f.match_no == s.match_no and (s.legs == 1 or f.leg)
                           and f.home_score is None and {f.home, f.away} == {s.home, s.away}),
                          key=lambda f: f.leg or 0)
            if not todo:                                  # one-off slot with a stale stand-in
                todo = [next(f for f in fx.values() if f.match_no == s.match_no)]
            for f in todo:                                # leg 1 is saved before leg 2 is rolled
                roll_and_save(conn, edition_id, branch_id, f.fixture_id, seed=rng.randrange(1 << 30),
                              origin="second_order_generated")
            rolled.append(s.match_no)
    engine.apply_cascade(conn, edition_id, branch_id)
    return rolled


# ------------------------------------------------------------------ constrained roll

GOAL_TYPES = ("goal", "pen_goal")


def _goals_for(events, team, opp):
    """A team's goal events: its own goals plus own goals by the opponent."""
    return [e for e in events if (e["team_id"] == team and e["event_type"] in GOAL_TYPES)
            or (e["team_id"] == opp and e["event_type"] == "own_goal")]


def _window(lineup, pid, length):
    l = next((x for x in lineup if x["player_id"] == pid), None)
    if l is None:
        return None
    on = 0 if l["started"] else (l["minute_on"] or 0)
    off = l["minute_off"] if l["minute_off"] is not None else length
    return (on, off) if off > on else None


def roll_with(conn, edition_id: str, branch_id: int | None, fixture: Fixture, constraints: dict | None = None,
              *, seed: int | None = None, info: dict | None = None) -> dict:
    """Roll a match around what you've fixed.

    constraints:
      home_score / away_score   exact score (after extra time); missing = rolled
      pen_home / pen_away       shootout score for a drawn knockout; missing = rolled
      my_goals                  exact number of goals for the created player
      locked_events             events kept exactly as given (goals count toward the score)
      keep_real                 keep this match's current goals, cards and lineup where possible

    Returns the payload plus `notes`: what happened to each goal already in the match.
    """
    from footsim import player as cp
    from footsim.api import EditError
    c = dict(constraints or {})
    rng = random.Random(seed)
    info = dict(info or _player_info(conn, edition_id))
    me = cp.profile(conn, branch_id, edition_id)
    if me is not None:
        info[me.player_id] = cp.scoring_info(me)
    tie = timeline.tie_rule(conn, edition_id, branch_id, fixture)
    locked = [dict(e) for e in c.get("locked_events") or []]
    keep_real = bool(c.get("keep_real")) and bool(fixture.lineup) and fixture.home_score is not None

    base = roll(conn, edition_id, branch_id, fixture, seed=rng.randrange(1 << 30), info=info)
    lineup = [dict(l) for l in (fixture.lineup if keep_real else base["lineup"])]
    current = [dict(e) for e in fixture.events] if fixture.home_score is not None else []

    # every locked event's player has to be on the pitch: bring him on before his event
    keep = {e.get("player_id") for e in locked} | ({me.player_id} if me else set())
    for e in sorted(locked, key=lambda e: e["minute"]):
        pid = e.get("player_id")
        if not pid or any(l["player_id"] == pid for l in lineup):
            continue
        from footsim.api import squad
        if pid not in {x["player_id"] for x in squad(conn, edition_id, e["team_id"], branch_id)}:
            raise EditError("A locked event names a player who isn't in that squad")
        on_at = max(0, min(e["minute"] - 1, 60))
        cands = [p for p in _on_pitch(lineup, e["team_id"], on_at) if p not in keep]
        same = [p for p in cands if info.get(p, {}).get("pos") == info.get(pid, {}).get("pos")]
        out = (same or [p for p in cands if info.get(p, {}).get("pos") != "GK"] or cands or [None])[0]
        if out is None:
            raise EditError("Couldn't fit a locked player into the lineup")
        if on_at == 0:
            cp._take_slot(lineup, [], out, pid)
        else:
            cp._take_slot(lineup, [], out, pid, from_minute=on_at)

    # the score
    target = {}
    for team, opp, key, idx in ((fixture.home, fixture.away, "home_score", 0), (fixture.away, fixture.home, "away_score", 1)):
        need = len(_goals_for(locked, team, opp))
        if c.get(key) is not None:
            if int(c[key]) < need:
                raise EditError(f"{team} has {need} locked goal(s), more than the {c[key]} you asked for")
            target[team] = int(c[key])
        else:
            got = base["home_score" if idx == 0 else "away_score"]
            target[team] = max(got, need)
    hs, as_ = target[fixture.home], target[fixture.away]
    extra_time = bool(c.get("extra_time")) if c.get("extra_time") is not None else tie.et(hs, as_)
    if any(e["minute"] > 90 for e in locked if e["event_type"] in GOAL_TYPES + ("own_goal",)):
        extra_time = True
    if tie.et(hs, as_):
        extra_time = True
    length = 120 if extra_time else 90

    my_window = _window(lineup, me.player_id, length) if me else None
    notes = []
    events = [e for e in locked]

    for team, opp in ((fixture.home, fixture.away), (fixture.away, fixture.home)):
        n = target[team] - len(_goals_for(locked, team, opp))
        chosen = []
        if keep_real:
            pool = [e for e in _goals_for(current, team, opp)
                    if not any(_same(e, l) for l in locked)]
            for e in sorted(pool, key=lambda e: e["minute"]):
                if len(chosen) < n and e["minute"] <= length:
                    chosen.append(dict(e, _real=True))
                else:
                    notes.append({"kind": "removed", "event": dict(e)})
        new_pool = [dict(e) for e in _goals_for(base["events"], team, opp) if e["minute"] <= length]
        while len(chosen) < n:
            if new_pool:
                e = new_pool.pop(0)
                _fix_on_pitch(rng, e, lineup, info)
            else:
                e = _new_goal(rng, lineup, team, length, info)
            chosen.append(dict(e, _new=True))
        events += chosen

    # the created player's goals
    k = c.get("my_goals")
    if me is not None and k is not None:
        k = int(k)
        team = me.team_id
        if team not in (fixture.home, fixture.away):
            raise EditError("Your player's team isn't in this match")
        if k > 0 and my_window is None:
            raise EditError(f"{me.name} isn't playing in this match. Set his role (start or sub) first.")
        mine = [e for e in events if e.get("player_id") == me.player_id and e["event_type"] in GOAL_TYPES]
        free = [e for e in events if e["team_id"] == team and e["event_type"] in GOAL_TYPES
                and e.get("player_id") != me.player_id and not any(_same(e, l) for l in locked)]
        # prefer new goals, then real ones; only goals he could have been on the pitch for
        free.sort(key=lambda e: (not e.get("_new"), e["minute"]))
        while len(mine) < k:
            cand = next((e for e in free if e.get("_new")), None)
            if cand is not None:
                cand["minute"] = rng.randint(max(1, my_window[0] + 1), max(my_window[0] + 1, min(my_window[1], length)))
            else:
                cand = next((e for e in free if my_window[0] <= e["minute"] < my_window[1]), None)
                if cand is None:
                    raise EditError(f"Not enough {team} goals to give {me.name} {k}. Raise the score or unlock goals.")
                notes.append({"kind": "taken", "event": {k2: v for k2, v in cand.items() if not k2.startswith("_")}})
            free.remove(cand)
            cand["player_id"] = me.player_id
            cand["event_type"] = "goal" if cand["event_type"] == "goal" else cand["event_type"]
            if cand.get("assist_player_id") == me.player_id or (
                    cand.get("assist_player_id") and cand["assist_player_id"] not in _on_pitch(lineup, team, cand["minute"])):
                cand["assist_player_id"] = None
                if cand.get("_new"):
                    cand["_reassist"] = True
            mine.append(cand)
        locked_mine = [e for e in mine if any(_same(e, l) for l in locked)]
        if len(locked_mine) > k:
            raise EditError(f"{len(locked_mine)} of {me.name}'s goals are locked, more than {k}")
        for e in sorted(mine, key=lambda e: (not e.get("_new"), e.get("_real", False)))[: max(0, len(mine) - k)]:
            if any(_same(e, l) for l in locked):
                continue
            others = [p for p in _on_pitch(lineup, team, e["minute"]) if p != me.player_id]
            if e.get("_real"):
                notes.append({"kind": "given_away", "event": {k2: v for k2, v in e.items() if not k2.startswith("_")}})
            e["player_id"] = _pick(rng, [(p, POS_GOAL[info.get(p, {}).get("pos", "MF")] + 6 * info.get(p, {}).get("g90", 0))
                                         for p in others]) if others else None
            if e.get("assist_player_id") == e["player_id"]:
                e["assist_player_id"] = None

    # assists: real goals keep what really happened; any other goal without one gets a rolled assist
    real_source = fixture.origin in ("baseline", "suspension_fix", "reused_historical")
    for e in events:
        if e["event_type"] not in GOAL_TYPES or e["event_type"] == "pen_goal" or any(_same(e, l) for l in locked):
            continue
        if e.get("_real") and real_source:
            continue
        on = [p for p in _on_pitch(lineup, e["team_id"], e["minute"]) if p != e.get("player_id")]
        if e.get("assist_player_id") in on:
            continue
        e["assist_player_id"] = None
        if on and rng.random() < P_ASSIST:
            e["assist_player_id"] = _pick(rng, [(p, POS_ASSIST[info.get(p, {}).get("pos", "MF")] + 8 * info.get(p, {}).get("a90", 0))
                                                for p in on])

    # the created player's assists
    ka = c.get("my_assists")
    if me is not None and ka is not None:
        ka = int(ka)
        team = me.team_id
        mine_a = [e for e in events if e.get("assist_player_id") == me.player_id]
        if ka > 0 and my_window is None:
            raise EditError(f"{me.name} isn't playing in this match. Set his role (start or sub) first.")
        open_goals = [e for e in events if e["team_id"] == team and e["event_type"] == "goal"
                      and e.get("player_id") != me.player_id and e.get("assist_player_id") != me.player_id
                      and my_window and my_window[0] <= e["minute"] < my_window[1]
                      and not any(_same(e, l) for l in locked)]
        open_goals.sort(key=lambda e: (bool(e.get("_real") and real_source), e["minute"]))   # prefer non-real goals
        while len(mine_a) < ka:
            if not open_goals:
                # move a rolled (not real) teammate goal into the minutes he's on the pitch
                opp = fixture.away if team == fixture.home else fixture.home
                movable = [e for e in _goals_for(events, team, opp) if e.get("_new")
                           and e.get("player_id") != me.player_id and e.get("assist_player_id") != me.player_id
                           and not any(_same(e, l) for l in locked)]
                movable.sort(key=lambda e: e["event_type"] != "goal")      # plain goals first
                if movable and movable[0]["event_type"] != "goal":
                    movable[0].update({"event_type": "goal", "team_id": team, "player_id": None})
                if not movable or not my_window:
                    raise EditError(f"Not enough {team} goals for {me.name} to assist {ka}. "
                                    "Raise the score or lower his goals.")
                e = movable[0]
                e["minute"] = rng.randint(max(1, my_window[0] + 1), max(my_window[0] + 1, min(my_window[1] - 1, length)))
                on = [p for p in _on_pitch(lineup, team, e["minute"]) if p != me.player_id]
                if e.get("player_id") not in on and on:
                    e["player_id"] = _pick(rng, [(p, POS_GOAL[info.get(p, {}).get("pos", "MF")] + 6 * info.get(p, {}).get("g90", 0))
                                                 for p in on])
                open_goals.append(e)
            e = open_goals.pop(0)
            if e.get("_real") and real_source and e.get("assist_player_id"):
                notes.append({"kind": "assist_taken", "event": {k2: v for k2, v in e.items() if not k2.startswith("_")}})
            e["assist_player_id"] = me.player_id
            mine_a.append(e)
        for e in mine_a[ka:]:
            on = [p for p in _on_pitch(lineup, team, e["minute"]) if p not in (me.player_id, e.get("player_id"))]
            e["assist_player_id"] = _pick(rng, [(p, POS_ASSIST[info.get(p, {}).get("pos", "MF")]) for p in on]) if on else None

    # cards and other non-goal events: keep the match's own; roll them if it has none
    has_cards = any(e["event_type"] in ("yellow", "second_yellow", "red") for e in current)
    keepers = current if (keep_real and has_cards) else base["events"]
    if keep_real and not has_cards:
        keepers = [e for e in current if e["event_type"] == "pen_miss"] + \
                  [e for e in base["events"] if e["event_type"] in ("yellow", "second_yellow", "red")]
    for e in keepers:
        if e["event_type"] in GOAL_TYPES + ("own_goal", "shootout_score", "shootout_miss"):
            continue
        if e["minute"] > length or any(_same(e, l) for l in locked):
            continue
        if e.get("player_id") and e["player_id"] not in _on_pitch(lineup, e["team_id"], e["minute"]):
            continue
        events.append(dict(e))

    # shootout
    pen_home = pen_away = None
    if tie.pens(hs, as_):
        if c.get("pen_home") is not None and c.get("pen_away") is not None:
            pen_home, pen_away = int(c["pen_home"]), int(c["pen_away"])
            if pen_home == pen_away:
                raise EditError("The shootout needs a winner")
        elif keep_real and fixture.pen_home is not None and fixture.home_score == hs and fixture.away_score == as_:
            pen_home, pen_away = fixture.pen_home, fixture.pen_away
            events += [dict(e) for e in current if e["event_type"] in ("shootout_score", "shootout_miss")]
        else:
            pen_home, pen_away, kicks = _shootout(rng, fixture, lineup, info, length)
            events += kicks

    # report on the goals that were in the match before
    if not keep_real:
        for e in current:
            if e["event_type"] in GOAL_TYPES + ("own_goal",) and not any(_same(e, l) for l in locked):
                notes.append({"kind": "removed", "event": dict(e)})
    kept = [e for e in events if e.get("_real")]
    for e in kept:
        orig = next((x for x in current if x["minute"] == e["minute"] and x["team_id"] == e["team_id"]
                     and x["event_type"] in GOAL_TYPES + ("own_goal",)), None)
        if orig and orig.get("player_id") == e.get("player_id"):
            notes.append({"kind": "kept", "event": {k2: v for k2, v in e.items() if not k2.startswith("_")}})
    for e in events:
        if e.get("_new"):
            notes.append({"kind": "new", "event": {k2: v for k2, v in e.items() if not k2.startswith("_")}})
    for e in locked:
        if e["event_type"] in GOAL_TYPES + ("own_goal",):
            notes.append({"kind": "locked", "event": dict(e)})

    clean = [{k2: v for k2, v in e.items() if not k2.startswith("_") and k2 != "locked"} for e in events]
    clean.sort(key=lambda e: e["minute"])
    notes.sort(key=lambda n: n["event"]["minute"])
    return {"home_score": hs, "away_score": as_, "extra_time": extra_time, "pen_home": pen_home,
            "pen_away": pen_away, "events": clean, "lineup": lineup, "notes": notes,
            "elo": base["elo"], "expected_goals": base["expected_goals"]}


def _fix_on_pitch(rng, e, lineup, info):
    """Re-pick a rolled goal's scorer/assister if lineup changes took them off the pitch."""
    if e["event_type"] == "own_goal":
        return
    on = _on_pitch(lineup, e["team_id"], e["minute"])
    if e.get("player_id") not in on and on:
        e["player_id"] = _pick(rng, [(p, POS_GOAL[info.get(p, {}).get("pos", "MF")] + 6 * info.get(p, {}).get("g90", 0))
                                     for p in on])
    if e.get("assist_player_id") and (e["assist_player_id"] not in on or e["assist_player_id"] == e.get("player_id")):
        e["assist_player_id"] = None


def _same(a, b):
    return (a.get("minute") == b.get("minute") and a.get("team_id") == b.get("team_id")
            and a.get("player_id") == b.get("player_id") and a.get("event_type") == b.get("event_type"))


def _new_goal(rng, lineup, team, length, info):
    minute = rng.randint(1, length)
    on = _on_pitch(lineup, team, minute)
    scorer = _pick(rng, [(p, POS_GOAL[info.get(p, {}).get("pos", "MF")] + 6 * info.get(p, {}).get("g90", 0))
                         for p in on]) if on else None
    assist = None
    if on and rng.random() < P_ASSIST:
        assist = _pick(rng, [(p, POS_ASSIST[info.get(p, {}).get("pos", "MF")] + 8 * info.get(p, {}).get("a90", 0))
                             for p in on if p != scorer])
    return {"minute": minute, "team_id": team, "player_id": scorer, "event_type": "goal", "assist_player_id": assist}
