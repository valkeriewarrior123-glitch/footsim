"""Competition engine: group tables, bracket, suspensions, invalidation cascade.

resolve(conn, edition, branch) is pure: it reads the active timeline and reports
standings, the bracket (with each knockout slot's status), suspensions and the
champion. apply_cascade() persists what resolve() finds: invalidated knockout
fixtures become `pending`, or are filled from a real historical meeting of the
same two teams (second-order reuse).
"""
from collections import defaultdict
from dataclasses import dataclass, field

from footsim import db
from footsim.timeline import Fixture, active_fixtures, set_result

# ------------------------------------------------------------------ standings

FAIR_PLAY_DEFAULT = {"yellow": -1, "second_yellow": -3, "red": -4, "yellow_then_red": -5}


@dataclass
class Row:
    team: str
    played: int = 0
    won: int = 0
    drawn: int = 0
    lost: int = 0
    gf: int = 0
    ga: int = 0
    points: int = 0
    fair_play: int = 0
    decided_by_lots: bool = False
    adjust: int = 0                  # points added/removed by a governing body (e.g. -6 for a scandal)

    @property
    def gd(self) -> int:
        return self.gf - self.ga


def _fair_play(fixtures: list[Fixture], weights: dict) -> dict[str, int]:
    """One deduction per player per match, by the worst card combination."""
    score = defaultdict(int)
    for f in fixtures:
        cards = defaultdict(set)
        for e in f.events:
            if e["event_type"] in ("yellow", "second_yellow", "red"):
                cards[(e["team_id"], e["player_id"])].add(e["event_type"])
        for (team, _), c in cards.items():
            if "second_yellow" in c:
                score[team] += weights["second_yellow"]
            elif "red" in c and "yellow" in c:
                score[team] += weights["yellow_then_red"]
            elif "red" in c:
                score[team] += weights["red"]
            elif "yellow" in c:
                score[team] += weights["yellow"]
    return score


def _table(teams, fixtures: list[Fixture], pts: dict) -> dict[str, Row]:
    rows = {t: Row(t) for t in teams}
    for f in fixtures:
        if not f.played or f.home not in rows or f.away not in rows:
            continue
        h, a = rows[f.home], rows[f.away]
        h.played += 1; a.played += 1
        h.gf += f.home_score; h.ga += f.away_score
        a.gf += f.away_score; a.ga += f.home_score
        if f.home_score > f.away_score:
            h.won += 1; a.lost += 1; h.points += pts["win"]; a.points += pts["loss"]
        elif f.home_score < f.away_score:
            a.won += 1; h.lost += 1; a.points += pts["win"]; h.points += pts["loss"]
        else:
            h.drawn += 1; a.drawn += 1; h.points += pts["draw"]; a.points += pts["draw"]
    return rows


def _rank(teams: list[str], keys: list[str], overall: dict[str, Row],
          fixtures: list[Fixture], pts: dict) -> list[str]:
    if len(teams) <= 1:
        return teams
    if not keys:
        return teams
    key, rest = keys[0], keys[1:]
    if key == "lots":
        # Deterministic stand-in for drawing lots; flagged so the UI can ask the user.
        for t in teams:
            overall[t].decided_by_lots = True
        return sorted(teams)
    if key.startswith("h2h_"):
        mini = _table(teams, fixtures, pts)
        stat = key[4:]
        if stat == "away_goals":            # goals scored away from home in the matches between the tied teams
            value = {t: sum(f.away_score for f in fixtures if f.played and f.away == t and f.home in teams)
                     for t in teams}
        else:
            value = {t: getattr(mini[t], {"points": "points", "goal_diff": "gd", "goals_for": "gf"}[stat])
                     for t in teams}
    else:
        attr = {"points": "points", "goal_diff": "gd", "goals_for": "gf", "fair_play": "fair_play"}[key]
        value = {t: getattr(overall[t], attr) for t in teams}

    buckets = defaultdict(list)
    for t in teams:
        buckets[value[t]].append(t)
    ordered = []
    for v in sorted(buckets, reverse=True):
        ordered += _rank(buckets[v], rest, overall, fixtures, pts)
    return ordered


def group_tables(fixtures: dict[str, Fixture], rules: dict) -> dict[str, list[Row]]:
    gs = rules["group_stage"]
    pts = gs["points"]
    weights = gs.get("fair_play_points", FAIR_PLAY_DEFAULT)
    tables = {}
    for g in gs["groups"]:
        gf = [f for f in fixtures.values() if f.stage == "group" and f.grp == g]
        teams = sorted({f.home for f in gf} | {f.away for f in gf})
        overall = _table(teams, gf, pts)
        fp = _fair_play([f for f in gf if f.played], weights)
        for t in teams:
            overall[t].fair_play = fp.get(t, 0)
        for t, adj in (gs.get("point_adjustments") or {}).items():
            if t in overall:
                overall[t].points += adj["points"]; overall[t].adjust = adj["points"]
        order = _rank(teams, gs["tiebreakers"], overall, gf, pts)
        tables[g] = [overall[t] for t in order]
    return tables


def group_complete(fixtures: dict[str, Fixture], grp: str) -> bool:
    return all(f.played for f in fixtures.values() if f.stage == "group" and f.grp == grp)


# ------------------------------------------------------------------ bracket

@dataclass
class Slot:
    match_no: int
    stage: str
    home: str | None            # expected teams from the bracket; None = not yet known
    away: str | None
    fixture: Fixture | None     # the active fixture that fills this slot, if valid
    status: str                 # valid | pending | tbd
    reason: str = ""
    reuse: dict | None = None   # a real historical meeting of the same pair, if any
    legs: int = 1               # 2 = a home-and-away tie; `fixture` is then an aggregate stand-in

    def real_fixtures(self) -> list:
        """The fixtures that were actually played in this slot (both legs of a tie)."""
        if self.fixture is None:
            return []
        return list(self.fixture.legs) if self.fixture.legs else [self.fixture]


@dataclass
class Resolution:
    edition_id: str
    tables: dict
    slots: list[Slot]
    suspensions: dict = field(default_factory=dict)   # fixture_id -> {team: [player_ids]}
    violations: list = field(default_factory=list)    # (fixture_id, player_id) suspended but played
    champion: str | None = None
    runner_up: str | None = None
    third: str | None = None
    fixes: list = field(default_factory=list)          # suspension swaps made by apply_cascade

    def slot(self, match_no: int) -> Slot:
        return next(s for s in self.slots if s.match_no == match_no)


def _aggregate(f1: Fixture, f2: Fixture, away_goals: bool) -> Fixture:
    """Stand-in fixture for a finished two-legged tie. f1 is the first leg (its home team is the slot's `home`)."""
    a, b = f1.home, f1.away
    agg = {a: f1.home_score + f2.away_score, b: f1.away_score + f2.home_score}
    winner, how = None, ""
    if agg[a] != agg[b]:
        winner, how = (a if agg[a] > agg[b] else b), "aggregate"
    elif away_goals and f2.away_score != f1.away_score:     # goals each side scored away from home
        winner, how = (a if f2.away_score > f1.away_score else b), "away goals"
    elif f2.pen_home is not None and f2.pen_home != f2.pen_away:
        winner, how = (f2.home if f2.pen_home > f2.pen_away else f2.away), "penalties"
    pen = (None, None)
    if how == "penalties":
        pa, pb = (f2.pen_away, f2.pen_home) if f2.home == b else (f2.pen_home, f2.pen_away)
        pen = (pa, pb)
    played_real = f1.status == "historical" and f2.status == "historical"
    out = Fixture(f2.fixture_id, f2.stage, None, f1.match_no, f2.date, a, b, agg[a], agg[b], f2.extra_time,
                  pen[0], pen[1], status="historical" if played_real else "overridden",
                  origin="baseline" if played_real else (f2.origin if f2.origin != "baseline" else f1.origin))
    out.legs, out.agg_winner, out.decided_by, out.leg = (f1, f2), winner, how, None
    out.events, out.lineup = f2.events, f2.lineup
    return out


def played_fixture_ids(res) -> set:
    """Ids of every knockout fixture that counts (both legs of a decided tie)."""
    return {f.fixture_id for s in res.slots if s.status == "valid" for f in s.real_fixtures()}


def _slot_team(code: str, tables, fixtures, slots_by_no) -> str | None:
    kind, ref = code[0], code[1:]
    if kind in "WL" and ref.isdigit():
        s = slots_by_no.get(int(ref))
        if s is None or s.status != "valid":
            return None
        return s.fixture.winner() if kind == "W" else s.fixture.loser()
    pos, grp = int(code[0]), code[1:]
    if not group_complete(fixtures, grp):
        return None
    return tables[grp][pos - 1].team


def _historical_meeting(conn, competition_id: str, pair: frozenset, exclude_edition: str | None = None):
    """A real knockout meeting of these two teams in this competition (any edition)."""
    a, b = sorted(pair)
    row = conn.execute(
        """SELECT f.* FROM fixtures f JOIN editions e USING(edition_id)
           WHERE e.competition_id = ? AND f.stage <> 'group'
             AND ((f.home_team_id = ? AND f.away_team_id = ?) OR (f.home_team_id = ? AND f.away_team_id = ?))
           ORDER BY f.date DESC LIMIT 1""",
        (competition_id, a, b, b, a),
    ).fetchone()
    return dict(row) if row else None


def resolve(conn, edition_id: str, branch_id: int | None = None) -> Resolution:
    rules = db.get_ruleset(conn, edition_id)
    competition_id = conn.execute("SELECT competition_id FROM editions WHERE edition_id = ?",
                                  (edition_id,)).fetchone()[0]
    fixtures = active_fixtures(conn, edition_id, branch_id)
    tables = group_tables(fixtures, rules)

    # Which active fixture claims each bracket number: overrides first, then baseline.
    by_no, by_leg = {}, {}
    for f in sorted(fixtures.values(), key=lambda f: f.override_id is None):
        if f.match_no is not None and f.match_no not in by_no:
            by_no[f.match_no] = f
        if f.match_no is not None and f.leg and (f.match_no, f.leg) not in by_leg:
            by_leg[(f.match_no, f.leg)] = f
    away_goals = bool(rules["knockout"].get("away_goals"))

    slots_by_no = {}
    for b in sorted(rules["knockout"]["bracket"], key=lambda b: b["match_no"]):
        n = b["match_no"]
        home = _slot_team(b["home"], tables, fixtures, slots_by_no)
        away = _slot_team(b["away"], tables, fixtures, slots_by_no)
        f = by_no.get(n)
        if b.get("legs", 1) == 2:
            f1, f2 = by_leg.get((n, 1)), by_leg.get((n, 2))
            if home is None or away is None:
                slot = Slot(n, b["stage"], home, away, None, "tbd", "waiting on an earlier result", legs=2)
            elif (f1 and f2 and f1.home == home and f1.away == away and f2.home == away and f2.away == home
                  and f1.played and f2.played):
                agg = _aggregate(f1, f2, away_goals)
                slot = (Slot(n, b["stage"], home, away, agg, "valid", legs=2) if agg.agg_winner else
                        Slot(n, b["stage"], home, away, None, "pending", "level after both legs", legs=2))
            else:
                slot = Slot(n, b["stage"], home, away, None, "pending",
                            "participants changed" if (f1 or f2) else "no result yet", legs=2)
            slots_by_no[n] = slot
            continue
        if home is None or away is None:
            slot = Slot(n, b["stage"], home, away, None, "tbd", "waiting on an earlier result")
        elif f is not None and f.teams() == frozenset((home, away)) and f.played and f.winner():
            slot = Slot(n, b["stage"], home, away, f, "valid")
        else:
            why = ("participants changed" if f is not None and f.teams() != frozenset((home, away))
                   else "no result yet")
            slot = Slot(n, b["stage"], home, away, None, "pending", why)
            slot.reuse = _historical_meeting(conn, competition_id, frozenset((home, away)))
        slots_by_no[n] = slot

    res = Resolution(edition_id, tables, list(slots_by_no.values()))
    _apply_suspensions(res, fixtures, rules)

    final = next((s for s in res.slots if s.stage == "F"), None)
    if final and final.status == "valid":
        res.champion, res.runner_up = final.fixture.winner(), final.fixture.loser()
    third = next((s for s in res.slots if s.stage == "3P"), None)
    if third and third.status == "valid":
        res.third = third.fixture.winner()
    return res


# ------------------------------------------------------------------ suspensions

STAGE_ORDER = ["group", "R32", "R16", "QF", "SF", "3P", "F"]


def _apply_suspensions(res: Resolution, fixtures: dict[str, Fixture], rules: dict,
                       include_all: bool = False) -> None:
    """Card accumulation -> bans for each team's next match. Flags suspended players who played.

    By default only group matches and valid knockout slots count; include_all=True
    uses every played fixture given (the caller has already filtered them).
    """
    sus = rules.get("suspensions", {})
    if sus.get("enabled") is False:
        return
    threshold = sus.get("yellows_for_ban", 2)
    reset_after = sus.get("yellows_reset_after_stage")
    reset_idx = STAGE_ORDER.index(reset_after) if reset_after in STAGE_ORDER else None

    valid_ko = played_fixture_ids(res)
    played = [f for f in fixtures.values()
              if f.played and (include_all or f.stage == "group" or f.fixture_id in valid_ko)]
    played.sort(key=lambda f: (f.date, STAGE_ORDER.index(f.stage)))

    yellows = defaultdict(int)          # player -> running count
    banned = defaultdict(int)           # player -> matches still to serve
    team_of = {}
    last_stage_idx = 0

    for f in played:
        idx = STAGE_ORDER.index(f.stage)
        if reset_idx is not None and last_stage_idx <= reset_idx < idx:
            yellows.clear()
        last_stage_idx = max(last_stage_idx, idx)

        # serve bans for this match
        out = defaultdict(list)
        for p, n in list(banned.items()):
            if n > 0 and team_of.get(p) in (f.home, f.away):
                out[team_of[p]].append(p)
                if any(l["player_id"] == p for l in f.lineup):
                    res.violations.append((f.fixture_id, p))
                banned[p] -= 1
        if out:
            res.suspensions[f.fixture_id] = dict(out)

        # collect this match's cards
        cards = defaultdict(set)
        for e in f.events:
            if e["event_type"] in ("yellow", "second_yellow", "red") and e["player_id"]:
                cards[e["player_id"]].add(e["event_type"])
                team_of[e["player_id"]] = e["team_id"]
        for p, c in cards.items():
            if "second_yellow" in c:
                banned[p] += 1                       # sent off; the match's yellow is wiped
            elif "red" in c:
                banned[p] += sus.get("red_card_ban_matches", 1)
                if "yellow" in c:
                    yellows[p] += 1
            elif "yellow" in c:
                yellows[p] += 1
                if yellows[p] >= threshold:
                    banned[p] += sus.get("ban_matches_per_accumulation", 1)
                    yellows[p] = 0


# ------------------------------------------------------------------ persistence

def assign_match_numbers(conn, edition_id: str) -> int:
    """Number the baseline knockout fixtures by walking the bracket on real results."""
    rules = db.get_ruleset(conn, edition_id)
    conn.execute("UPDATE fixtures SET match_no = NULL WHERE edition_id = ? AND stage <> 'group'", (edition_id,))
    fixtures = active_fixtures(conn, edition_id)
    tables = group_tables(fixtures, rules)
    slots, numbered = {}, 0
    for b in sorted(rules["knockout"]["bracket"], key=lambda b: b["match_no"]):
        home = _slot_team(b["home"], tables, fixtures, slots)
        away = _slot_team(b["away"], tables, fixtures, slots)
        pair = frozenset((home, away))
        f = next((f for f in fixtures.values() if f.stage == b["stage"] and f.teams() == pair), None)
        if f is None:
            raise ValueError(f"Bracket match {b['match_no']} ({home} v {away}) not found in baseline")
        f.match_no = b["match_no"]
        conn.execute("UPDATE fixtures SET match_no = ? WHERE fixture_id = ?", (b["match_no"], f.fixture_id))
        slots[b["match_no"]] = Slot(b["match_no"], b["stage"], home, away, f, "valid")
        numbered += 1
    conn.commit()
    return numbered


def apply_cascade(conn, edition_id: str, branch_id: int, reuse_history: bool = True) -> Resolution:
    """Bring a branch up to date after any change.

    1. Walk the bracket: pending slots, reused real results, stale stand-ins removed.
    2. Enforce suspensions: a banned player who appears in a later match is swapped
       out (the best player available at his position takes his spot and its events).
       Earlier automatic swaps are undone first, so undoing an edit undoes its knock-ons.
    """
    conn.execute("DELETE FROM fixture_overrides WHERE branch_id = ? AND edition_id = ? AND origin = 'suspension_fix'",
                 (branch_id, edition_id))
    conn.commit()
    _cascade_slots(conn, edition_id, branch_id, reuse_history)
    fixes = enforce_suspensions(conn, edition_id, branch_id)
    res = resolve(conn, edition_id, branch_id)
    seen = {(r[0], r[1]) for r in conn.execute(
        "SELECT fixture_id, player_id FROM suspension_log WHERE branch_id = ? AND edition_id = ?",
        (branch_id, edition_id))}
    res.fixes = [f for f in fixes if (f["fixture_id"], f["player_id"]) not in seen]
    conn.execute("DELETE FROM suspension_log WHERE branch_id = ? AND edition_id = ?", (branch_id, edition_id))
    for f in fixes:
        conn.execute("INSERT OR IGNORE INTO suspension_log VALUES (?,?,?,?)",
                     (branch_id, edition_id, f["fixture_id"], f["player_id"]))
    conn.commit()
    return res


def enforce_suspensions(conn, edition_id: str, branch_id: int) -> list:
    """Swap suspended players out of the matches they're banned from. Returns what was changed."""
    from footsim.timeline import active_fixtures
    from footsim import player as cp
    fixes = []
    for _ in range(40):
        res = resolve(conn, edition_id, branch_id)
        if not res.violations:
            break
        fid, banned_player = res.violations[0]
        fx = active_fixtures(conn, edition_id, branch_id)
        f = fx[fid]
        team = next((l["team_id"] for l in f.lineup if l["player_id"] == banned_player), None)
        banned_here = set(res.suspensions.get(fid, {}).get(team, []))
        me = cp.profile(conn, branch_id, edition_id)
        if me is not None and banned_player == me.player_id and f.override_id is None:
            # a real match: just keep the created player out of it
            conn.execute("INSERT OR REPLACE INTO player_roles VALUES (?,?, 'unused', NULL, NULL)", (branch_id, fid))
            conn.commit()
            fixes.append({"fixture_id": fid, "player_id": banned_player, "replacement": None,
                          "home": f.home, "away": f.away, "date": f.date})
            continue
        lineup, events = _swap_out(conn, edition_id, branch_id, f, banned_player, team, banned_here)
        replacement = next((l["player_id"] for l in lineup if l["player_id"] not in
                            {x["player_id"] for x in f.lineup}), None)
        if f.override_id is None:
            from footsim.timeline import set_result
            set_result(conn, branch_id, f, f.home_score, f.away_score, events=events, lineup=lineup,
                       extra_time=f.extra_time, pen_home=f.pen_home, pen_away=f.pen_away,
                       status="overridden", origin="suspension_fix",
                       reason=f"suspended player swapped out", edition_id=edition_id)
        else:
            conn.execute("DELETE FROM lineup_overrides WHERE override_id = ?", (f.override_id,))
            conn.execute("DELETE FROM event_overrides WHERE override_id = ?", (f.override_id,))
            for l in lineup:
                conn.execute("INSERT INTO lineup_overrides VALUES (?,?,?,?,?,?)",
                             (f.override_id, l["team_id"], l["player_id"], l["started"], l.get("minute_on"),
                              l.get("minute_off")))
            for e in events:
                conn.execute("INSERT INTO event_overrides (override_id, minute, added_time, team_id, player_id, "
                             "event_type, assist_player_id) VALUES (?,?,?,?,?,?,?)",
                             (f.override_id, e["minute"], e.get("added_time"), e["team_id"], e.get("player_id"),
                              e["event_type"], e.get("assist_player_id")))
            conn.commit()
        fixes.append({"fixture_id": fid, "player_id": banned_player, "replacement": replacement,
                      "home": f.home, "away": f.away, "date": f.date})
    return fixes


def _swap_out(conn, edition_id, branch_id, f, out_player, team, banned_here):
    from footsim import player as cp
    from footsim.api import squad
    from footsim.roller import _player_info
    info = _player_info(conn, edition_id)
    me = cp.profile(conn, branch_id, edition_id)
    if me is not None:
        info[me.player_id] = cp.scoring_info(me)
    squad_ids = [p["player_id"] for p in squad(conn, edition_id, team, branch_id)]
    lineup = [dict(l) for l in f.lineup]
    events = [dict(e) for e in f.events]
    exclude = set(banned_here) | {out_player}
    if me is not None and (me.player_id in banned_here or cp.injured_on(conn, branch_id, me.player_id, f.date)):
        exclude.add(me.player_id)
    filler = cp._bench_pick(lineup, squad_ids, info, info.get(out_player, {}).get("pos", "MF"), exclude)
    if filler:
        cp._take_slot(lineup, events, out_player, filler)
    else:
        lineup = [l for l in lineup if l["player_id"] != out_player]
        events = [e for e in events if e.get("player_id") != out_player or e["event_type"] not in
                  ("yellow", "second_yellow", "red")]
    return lineup, events


def _cascade_slots(conn, edition_id: str, branch_id: int, reuse_history: bool = True) -> Resolution:
    """Walk the bracket and persist its state. Repeats until stable."""
    baseline_ids = {r["match_no"]: r["fixture_id"] for r in conn.execute(
        "SELECT match_no, fixture_id FROM fixtures WHERE edition_id = ? AND match_no IS NOT NULL", (edition_id,))}
    leg_rows = {(r["match_no"], r["leg"]): dict(r) for r in conn.execute(
        "SELECT * FROM fixtures WHERE edition_id = ? AND match_no IS NOT NULL AND leg IS NOT NULL", (edition_id,))}
    for _ in range(10):
        res = resolve(conn, edition_id, branch_id)
        changed = False
        for s in res.slots:
            if s.legs == 2:
                changed |= _cascade_tie(conn, edition_id, branch_id, s, leg_rows)
                continue
            fid = baseline_ids.get(s.match_no, f"{edition_id}_M{s.match_no}")
            existing = conn.execute(
                "SELECT status, origin, home_team_id, away_team_id FROM fixture_overrides "
                "WHERE branch_id = ? AND fixture_id = ?", (branch_id, fid)).fetchone()
            if s.status == "valid":
                continue
            if s.status == "tbd":
                # Upstream unknown: any stale override for this slot is no longer meaningful.
                if existing and existing["status"] != "pending":
                    conn.execute("DELETE FROM fixture_overrides WHERE branch_id = ? AND fixture_id = ?",
                                 (branch_id, fid))
                    changed = True
                continue
            # Teams are back to the real pairing for this slot: drop the cascade's
            # stand-in so the historical result shows again. User edits are kept.
            if s.reuse and s.reuse["fixture_id"] == fid and existing and \
                    existing["origin"] in ("cascade", "reused_historical"):
                conn.execute("DELETE FROM fixture_overrides WHERE branch_id = ? AND fixture_id = ?",
                             (branch_id, fid))
                changed = True
                continue
            # pending with known teams
            if existing and existing["status"] == "pending" and \
                    {existing["home_team_id"], existing["away_team_id"]} == {s.home, s.away} and \
                    not (reuse_history and s.reuse):
                continue
            stub = Fixture(fid, s.stage, None, s.match_no, _slot_date(conn, edition_id, s.match_no),
                           s.home, s.away, None, None)
            if reuse_history and s.reuse:
                r = s.reuse
                flip = r["home_team_id"] != s.home
                hs, as_ = (r["away_score"], r["home_score"]) if flip else (r["home_score"], r["away_score"])
                ph, pa = (r["pen_away"], r["pen_home"]) if flip else (r["pen_home"], r["pen_away"])
                events = [dict(e) for e in conn.execute(
                    "SELECT minute, team_id, player_id, event_type, assist_player_id FROM match_events "
                    "WHERE fixture_id = ?", (r["fixture_id"],))]
                lineup = [dict(l) for l in conn.execute(
                    "SELECT team_id, player_id, started, minute_on, minute_off FROM lineups WHERE fixture_id = ?",
                    (r["fixture_id"],))]
                set_result(conn, branch_id, stub, hs, as_, events=events, lineup=lineup,
                           extra_time=r["extra_time"], pen_home=ph, pen_away=pa, status="generated",
                           origin="reused_historical", reason=f"real result from {r['fixture_id']}",
                           edition_id=edition_id)
            else:
                set_result(conn, branch_id, stub, None, None, status="pending", origin="cascade",
                           reason=s.reason, edition_id=edition_id)
            changed = True
        if not changed:
            return res
    return resolve(conn, edition_id, branch_id)


def _cascade_tie(conn, edition_id: str, branch_id: int, s: Slot, leg_rows: dict) -> bool:
    """Keep the two leg fixtures of a home-and-away slot in step with who is in it. True if anything changed."""
    changed = False
    for leg in (1, 2):
        base = leg_rows.get((s.match_no, leg))
        if base is None:
            continue
        fid = base["fixture_id"]
        existing = conn.execute("SELECT status, origin, home_team_id, away_team_id FROM fixture_overrides "
                                "WHERE branch_id = ? AND fixture_id = ?", (branch_id, fid)).fetchone()
        if s.status == "valid":
            continue
        if s.status == "tbd":                            # upstream unknown: a stale override means nothing
            if existing and existing["status"] != "pending":
                conn.execute("DELETE FROM fixture_overrides WHERE branch_id = ? AND fixture_id = ?", (branch_id, fid))
                changed = True
            continue
        home, away = (s.home, s.away) if leg == 1 else (s.away, s.home)
        if existing and existing["status"] == "pending" and {existing["home_team_id"], existing["away_team_id"]} == {home, away}:
            continue
        if existing and existing["origin"] in ("cascade", "reused_historical") and \
                (base["home_team_id"], base["away_team_id"]) == (home, away):
            conn.execute("DELETE FROM fixture_overrides WHERE branch_id = ? AND fixture_id = ?", (branch_id, fid))
            changed = True                               # back to the real pairing: the real leg shows again
            continue
        if existing is None and (base["home_team_id"], base["away_team_id"]) == (home, away):
            continue                                     # the real leg is already right (the tie is level, see resolve)
        stub = Fixture(fid, s.stage, None, s.match_no, base["date"], home, away, None, None)
        stub.leg = leg
        set_result(conn, branch_id, stub, None, None, status="pending", origin="cascade",
                   reason=s.reason, edition_id=edition_id)
        changed = True
    return changed


def _slot_date(conn, edition_id: str, match_no: int) -> str:
    row = conn.execute("SELECT date FROM fixtures WHERE edition_id = ? AND match_no = ?",
                       (edition_id, match_no)).fetchone()
    return row[0] if row else "TBD"


# ------------------------------------------------------------------ report

def report(res: Resolution) -> str:
    lines = [f"== {res.edition_id} =="]
    for g, rows in res.tables.items():
        lines.append(f"Group {g}")
        for i, r in enumerate(rows, 1):
            lot = " (lots)" if r.decided_by_lots else ""
            lines.append(f"  {i}. {r.team:4} P{r.played} W{r.won} D{r.drawn} L{r.lost} "
                         f"{r.gf}-{r.ga} ({r.gd:+d}) {r.points}pts FP{r.fair_play}{lot}")
    lines.append("Knockouts")
    for s in res.slots:
        if s.status == "valid":
            f = s.fixture
            pens = f" ({f.pen_home}-{f.pen_away}p)" if f.pen_home is not None else ""
            tag = "" if f.status == "historical" else f" [{f.origin}]"
            lines.append(f"  {s.match_no} {s.stage:3} {f.home} {f.home_score}-{f.away_score} {f.away}{pens}{tag}")
        else:
            lines.append(f"  {s.match_no} {s.stage:3} {s.home or '?'} v {s.away or '?'}  "
                         f"[{s.status.upper()}: {s.reason}]")
    lines.append(f"Champion: {res.champion or 'undecided'}")
    if res.violations:
        lines.append(f"Suspended players who played: {res.violations}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    conn = db.connect()
    branch = int(sys.argv[2]) if len(sys.argv) > 2 else None
    print(report(resolve(conn, sys.argv[1] if len(sys.argv) > 1 else "WC2022", branch)))
