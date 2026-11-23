"""Season records for a league edition (works for any club league pack): scorers, fastest/latest goals,
biggest wins, clean sheets, cards, runs. Everything is computed from the active fixtures, so a branch's
edited or rolled matches count."""
from collections import defaultdict

from footsim import timeline

GOALS = ("goal", "pen_goal")


def _goals(f):
    """(minute, scoring_team, player_id, known) for every goal; own goals count for the opponent."""
    for e in f.events:
        t = e["event_type"]
        if t in GOALS:
            yield e["minute"], e["team_id"], e["player_id"], e["player_id"] is not None
        elif t == "own_goal":
            yield e["minute"], (f.away if e["team_id"] == f.home else f.home), e["player_id"], e["player_id"] is not None


def season_records(conn, edition_id, branch_id, names, team_names):
    fx = [f for f in timeline.active_fixtures(conn, edition_id, branch_id).values() if f.played]
    fx.sort(key=lambda f: f.date)
    if not fx:
        return []
    T = lambda t: team_names.get(t, t)
    P = lambda p: names.get(p, "Unknown scorer") if p else "Unknown scorer"
    nm = lambda f: f"{T(f.home)} {f.home_score}–{f.away_score} {T(f.away)}"
    out = []

    def add(key, label, lines):
        out.append({"key": key, "label": label, "lines": lines or ["—"]})

    def cap(items, n=3):
        items = list(items)
        return items[:n] + ([f"+{len(items) - n} more tied"] if len(items) > n else [])

    # --- goals ---------------------------------------------------------------
    goals = []     # (date, minute, team, player, fixture) with a real scorer (placeholder minutes are skipped)
    scored = defaultdict(int)
    for f in fx:
        for minute, team, pid, known in _goals(f):
            if known:
                goals.append((f.date, minute, team, pid, f))
    for f in fx:
        for _, team, pid, known in _goals(f):
            if known:
                scored[(pid, team)] += 1
    top = sorted(scored.items(), key=lambda kv: -kv[1])[:5]
    add("top_scorers", "Top 5 goal scorers", [f"{i + 1}. {P(p)} ({T(t)}) – {n}" for i, ((p, t), n) in enumerate(top)])

    def gline(g):
        d, m, t, p, f = g
        return f"{P(p)} ({T(t)}) {m}′ · {nm(f)} · {d}"
    if goals:
        first_day = min(g[0] for g in goals)
        add("first_goal", "First goal of the season", [gline(min((g for g in goals if g[0] == first_day), key=lambda g: g[1]))])
        lo, hi = min(g[1] for g in goals), max(g[1] for g in goals)
        add("fastest_goal", "Fastest goal of the season", cap(gline(g) for g in goals if g[1] == lo))
        add("latest_goal", "Latest goal of the season", cap(gline(g) for g in goals if g[1] == hi))

    # --- matches -------------------------------------------------------------
    def best(key, fn, label, fmt, reverse=True):
        vals = [(fn(f), f) for f in fx]
        vals = [(v, f) for v, f in vals if v is not None]
        if not vals:
            return
        target = max(v for v, _ in vals)
        add(key, label, cap(fmt(f) for v, f in vals if v == target))
    best("highest_scoring", lambda f: f.home_score + f.away_score, "Highest scoring game",
         lambda f: f"{nm(f)} · {f.date}")
    # most goals by one team in a match
    m = max(max(f.home_score, f.away_score) for f in fx)
    add("most_by_team", "Most goals by a single team in a match",
        cap(f"{T(f.home if f.home_score == m else f.away)} – {m} · {nm(f)}" for f in fx if max(f.home_score, f.away_score) == m))
    losers = [(min(f.home_score, f.away_score), f) for f in fx if f.home_score != f.away_score]
    if losers:
        mx = max(v for v, _ in losers)
        add("most_by_loser", "Most goals by a losing team",
            cap(f"{T(f.home if f.home_score < f.away_score else f.away)} – {v} · {nm(f)}" for v, f in losers if v == mx))
    hw = [(f.home_score - f.away_score, f) for f in fx if f.home_score > f.away_score]
    aw = [(f.away_score - f.home_score, f) for f in fx if f.away_score > f.home_score]
    if hw:
        mx = max(v for v, _ in hw); add("big_home", "Biggest home win", cap(f"{nm(f)} · {f.date}" for v, f in hw if v == mx))
    if aw:
        mx = max(v for v, _ in aw); add("big_away", "Biggest away win", cap(f"{nm(f)} · {f.date}" for v, f in aw if v == mx))
    total = sum(f.home_score + f.away_score for f in fx)
    add("total_goals", "Goals in the league", [f"{total} in {len(fx)} matches ({total / len(fx):.2f} per match)"])

    # --- clean sheets, cards -------------------------------------------------
    teams = {t for f in fx for t in (f.home, f.away)}
    cs = {t: 0 for t in teams}
    for f in fx:
        if f.away_score == 0: cs[f.home] += 1
        if f.home_score == 0: cs[f.away] += 1

    def extreme(d, pick):
        v = pick(d.values()); return v, sorted(T(t) for t, x in d.items() if x == v)
    v, ts = extreme(cs, max); add("most_cs", "Most clean sheets", [f"{', '.join(ts[:4])} – {v}"])
    v, ts = extreme(cs, min); add("fewest_cs", "Fewest clean sheets", [f"{', '.join(ts[:4])} – {v}"])

    ycp, ycl, rcp, rcl = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    for f in fx:
        for e in f.events:
            if e["event_type"] == "yellow":
                ycl[e["team_id"]] += 1
                if e["player_id"]: ycp[(e["player_id"], e["team_id"])] += 1
            elif e["event_type"] in ("red", "second_yellow"):
                rcl[e["team_id"]] += 1
                if e["player_id"]: rcp[(e["player_id"], e["team_id"])] += 1

    def card(key, label, club, player):
        lines = []
        if club:
            v = max(club.values()); lines.append(f"Club: {', '.join(sorted(T(t) for t, x in club.items() if x == v))} – {v}")
        if player:
            v = max(player.values()); who = [f"{P(p)} ({T(t)})" for (p, t), x in sorted(player.items()) if x == v]
            lines.append(f"Player: {', '.join(who[:4])}{f' +{len(who) - 4} more tied' if len(who) > 4 else ''} – {v}")
        add(key, label, lines)
    card("yellows", "Most yellow cards", ycl, ycp)
    card("reds", "Most red cards (a second yellow counts as a red)", rcl, rcp)

    # --- runs ---------------------------------------------------------------
    seq = defaultdict(list)
    for f in fx:
        seq[f.home].append("W" if f.home_score > f.away_score else "D" if f.home_score == f.away_score else "L")
        seq[f.away].append("W" if f.away_score > f.home_score else "D" if f.home_score == f.away_score else "L")

    def run(ok):
        best_n, who = 0, []
        for t, s in seq.items():
            n = cur = 0
            for r in s:
                cur = cur + 1 if ok(r) else 0
                n = max(n, cur)
            if n > best_n: best_n, who = n, [t]
            elif n == best_n and n: who.append(t)
        return best_n, sorted(T(t) for t in who)
    for key, label, ok in (("win_run", "Longest winning run", lambda r: r == "W"),
                           ("unbeaten_run", "Longest unbeaten run (draws count)", lambda r: r != "L"),
                           ("winless_run", "Longest winless run", lambda r: r != "W"),
                           ("losing_run", "Longest losing run", lambda r: r == "L")):
        n, who = run(ok)
        add(key, label, [f"{', '.join(who[:4])} – {n} matches"] if n else None)
    return out
