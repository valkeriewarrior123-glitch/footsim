"""Build the UEFA Champions League 2012/13 data pack (data_packs/UCL1213) from the openfootball scores.

    python tools/build_cl_pack.py --cl champions-league --kaggle european_soccer.sqlite --seriea data_packs/SERIEA1213 \
        --out data_packs/UCL1213

Real facts used: every match's date, teams and final score (and half-time score, used only to place goal minutes),
group membership, and the real pairings of the knockout rounds.

Made up, and declared in notes.csv (kind `scorer_unknown`, `lineup_incomplete`, `generated_details`):
  * goal scorers and minutes for every match (placeholder "unknown scorer" goals; minutes follow the half-time score);
  * for Juventus matches only: a lineup (the starters of the nearest Serie A match) and scorers drawn at random by
    each player's Serie A scoring record, with a fixed seed so rebuilding gives the same pack.
Club strength (ratings.csv): domestic Elo going into the season from the Kaggle database, plus one offset per country
fitted on the previous Champions League season; clubs from leagues the Kaggle database lacks get 1500 plus their
country's offset. Italy is the reference, so Juventus and Milan keep their Serie A ratings.
"""
import argparse, csv, difflib, importlib.util, math, random, re, sqlite3, sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bcp", HERE / "build_club_pack.py")
bcp = importlib.util.module_from_spec(spec); spec.loader.exec_module(bcp)

MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
STAGES = {"Round of 16": "R16", "Quarterfinals": "QF", "Semifinals": "SF", "Final": "F"}
OWN = {"Juventus": ("c_juventus", "Juventus"), "AC Milan": ("c_milan", "Milan")}
LEAGUE_OF = {"ENG": 1729, "FRA": 4769, "GER": 7809, "ITA": 10257, "NED": 13274, "POR": 17642, "SCO": 19694,
             "ESP": 21518, "BEL": 1, "SUI": 24558, "POL": 15722}
UNSEEN = -80.0          # a country with no club in the fitting season (e.g. Scotland): assume a smaller league
SHORT = {"Paris Saint-Germain": "Paris Saint-Germain", "FC Barcelona": "Barcelona", "Bayern München": "Bayern Munich"}


def parse(path):
    """-> list of dict(stage, grp, date, home, away, hs, as, hth, hta, cty{team: code})"""
    out, cty = [], {}
    stage = grp = None
    year, last_month, first_year = None, None, None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"^▪ (.+)$", line)
        if m:
            name = m.group(1).strip()
            stage, grp = ("group", name.split()[-1]) if name.startswith("Group") else (STAGES[name], None)
            if stage == "group" and first_year:      # every group starts again in September of the first year
                year, last_month = first_year, None
            continue
        m = re.match(r"^\s+\w{3} (\w{3}) (\d+)(?: (\d{4}))?\s*$", line)
        if m:
            mon = MONTHS[m.group(1)]
            if m.group(3):
                year = int(m.group(3))
                first_year = first_year or year
            elif last_month and mon < last_month:
                year += 1
            last_month = mon
            cur = (year, mon, int(m.group(2)))
            continue
        m = re.match(r"^\s+(?:\d\d:\d\d\s+)?(.+?)\s+v\s+(.+?)\s+(\d+)-(\d+)(?: pen\. (\d+)-(\d+))?( a\.e\.t\.)?(?:\s+\((\d+)-(\d+)(?:, \d+-\d+)?\))?\s*$", line)
        if m and stage:
            def team(s):
                mm = re.match(r"^(.*?)\s*\(([A-Z]{3})\)$", s.strip())
                cty[mm.group(1)] = mm.group(2)
                return mm.group(1)
            h, a = team(m.group(1)), team(m.group(2))
            pens = (int(m.group(3)), int(m.group(4))) if m.group(5) else None      # "4-3 pen. 1-0 a.e.t.": shootout first
            hs_, as__ = (int(m.group(5)), int(m.group(6))) if pens else (int(m.group(3)), int(m.group(4)))
            ht = (int(m.group(8)), int(m.group(9))) if m.group(8) and not m.group(7) and not pens else None
            out.append(dict(stage=stage, grp=grp, date="%04d-%02d-%02d" % cur, home=h, away=a,
                            hs=hs_, as_=as__, ht=ht, pens=pens, et=bool(m.group(7))))
        elif line.strip() and not line.startswith(("=", "#")) and " v " in line:
            raise ValueError("unparsed line: " + line)
    return out, cty


def tid(name):
    return OWN[name][0] if name in OWN else "c_" + bcp.slug(name)


def tname(name):
    return OWN[name][1] if name in OWN else SHORT.get(name, name)


# ------------------------------------------------------------------ strength
def domestic_elo(kc, cty_code, name, season, cache):
    """Domestic Elo going into `season` for the Kaggle club closest in name to `name`, or None."""
    lg = LEAGUE_OF.get(cty_code)
    if lg is None:
        return None
    key = (lg, season)
    if key not in cache:
        ids = [r[0] for r in kc.execute("SELECT DISTINCT home_team_api_id FROM Match WHERE league_id=? AND season=?", (lg, season))]
        ids += [r[0] for r in kc.execute("SELECT DISTINCT home_team_api_id FROM Match WHERE league_id=? AND season<?", (lg, season))]
        ids = set(ids)
        elo = bcp.club_elo(kc, lg, season, ids)
        names = {r[0]: r[1] for r in kc.execute("SELECT team_api_id, team_long_name FROM Team")}
        cache[key] = {names[i]: e for i, e in elo.items() if i in names}
    pool = cache[key]
    want = " ".join(bcp.plain(name))
    norm = {" ".join(bcp.plain(n)): n for n in pool}
    hit = difflib.get_close_matches(want, list(norm), n=1, cutoff=0.6)
    if not hit:                                   # "Real Madrid" in "Real Madrid CF", "Bayern München" ~ "Bayern Munich"
        hit = [k for k in norm if want in k or k in want][:1]
    return pool[norm[hit[0]]] if hit else None


def fit_offsets(prev, cty_prev, kc, season_prev, cache, fixed="ITA"):
    """Per-country Elo offsets from the previous CL season's results (Italy fixed at 0)."""
    base = {}
    for f in prev:
        for t in (f["home"], f["away"]):
            if t not in base:
                d = domestic_elo(kc, cty_prev[t], t, season_prev, cache)
                base[t] = d if d is not None else 1500.0
    countries = sorted({cty_prev[t] for t in base} - {fixed})
    off = {c: 0.0 for c in countries}; off[fixed] = 0.0
    for it in range(400):
        grad = defaultdict(float)
        for f in prev:
            h, a = f["home"], f["away"]
            neutral = f["stage"] == "F"
            rh = base[h] + off[cty_prev[h]] + (0 if neutral else 100); ra = base[a] + off[cty_prev[a]]
            e = 1 / (1 + 10 ** (-(rh - ra) / 400))
            res = 1.0 if f["hs"] > f["as_"] else 0.0 if f["hs"] < f["as_"] else 0.5
            g = (res - e)
            grad[cty_prev[h]] += g; grad[cty_prev[a]] -= g
        for c in countries:
            off[c] += 3.0 * grad[c] - 0.12 * off[c]          # damped: one season of results is a small sample
    return off


# ------------------------------------------------------------------ details
def goal_minutes(rng, n_first, n_second):
    return sorted([rng.randint(6, 44) for _ in range(n_first)] + [rng.randint(47, 89) for _ in range(n_second)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cl", type=Path, required=True)
    ap.add_argument("--kaggle", type=Path, required=True)
    ap.add_argument("--seriea", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20130525)
    games, cty = parse(a.cl / "2012-13" / "cl.txt")
    prev, cty_prev = parse(a.cl / "2011-12" / "cl.txt")
    kc = sqlite3.connect(a.kaggle); kc.row_factory = sqlite3.Row
    cache = {}
    EDN = "UCL1213"

    # ---- groups and fixtures
    groups = {}
    for g in games:
        if g["stage"] == "group":
            groups.setdefault(g["grp"], set()).update((g["home"], g["away"]))
    rows, per_group = [], defaultdict(int)
    ties = defaultdict(list)
    for g in games:
        if g["stage"] == "group":
            per_group[g["grp"]] += 1
            g["fixture_id"] = f"{EDN}_G{g['grp']}{per_group[g['grp']]:02d}"
            g["leg"], g["match_no"] = "", ""
        elif g["stage"] == "F":
            g["fixture_id"], g["leg"], g["match_no"] = f"{EDN}_F", "", 64
        else:
            ties[(g["stage"], frozenset((g["home"], g["away"])))].append(g)

    # ---- group tables (points, then head-to-head, then goal difference...) to name the group winners
    def table(grp):
        gm = [g for g in games if g["stage"] == "group" and g["grp"] == grp]
        st = {t: dict(p=0, gf=0, ga=0) for t in groups[grp]}
        for g in gm:
            for t, f, aa in ((g["home"], g["hs"], g["as_"]), (g["away"], g["as_"], g["hs"])):
                st[t]["gf"] += f; st[t]["ga"] += aa; st[t]["p"] += 3 if f > aa else 1 if f == aa else 0
        def h2h(t, others):
            p = gd = gf = 0
            for g in gm:
                if {g["home"], g["away"]} <= others and t in (g["home"], g["away"]):
                    f, aa = (g["hs"], g["as_"]) if g["home"] == t else (g["as_"], g["hs"])
                    p += 3 if f > aa else 1 if f == aa else 0; gd += f - aa; gf += f
            ag = sum(g["as_"] for g in gm if g["away"] == t and g["home"] in others)
            return (p, gd, gf, ag)
        order = sorted(st, key=lambda t: (-st[t]["p"],))
        out, i = [], 0
        while i < len(order):
            j = i
            while j < len(order) and st[order[j]]["p"] == st[order[i]]["p"]:
                j += 1
            tied = set(order[i:j])
            out += sorted(tied, key=lambda t: (tuple(-x for x in h2h(t, tied)), -(st[t]["gf"] - st[t]["ga"]), -st[t]["gf"]))
            i = j
        return out
    pos = {}
    for grp in groups:
        for i, t in enumerate(table(grp), 1):
            pos[t] = (i, grp)

    # ---- knockout bracket from the real pairings
    bracket, tie_no, winner_of = [], {}, {}
    n = 49
    def tie_winner(legs):
        l1, l2 = sorted(legs, key=lambda g: g["date"])
        a_, b_ = l1["home"], l1["away"]
        agg = {a_: l1["hs"] + l2["as_"], b_: l1["as_"] + l2["hs"]}
        if agg[a_] != agg[b_]:
            return a_ if agg[a_] > agg[b_] else b_
        return a_ if l2["as_"] > l1["as_"] else b_
    for stage in ("R16", "QF", "SF"):
        for key in sorted((k for k in ties if k[0] == stage), key=lambda k: min(g["date"] for g in ties[k])):
            legs = sorted(ties[key], key=lambda g: g["date"])
            tie_no[key] = n
            l1 = legs[0]
            def code(t):
                if stage == "R16":
                    return f"{pos[t][0]}{pos[t][1]}"
                return "W%d" % winner_of[t]
            bracket.append({"match_no": n, "stage": stage, "home": code(l1["home"]), "away": code(l1["away"]), "legs": 2})
            for k, g in enumerate(legs, 1):
                g["fixture_id"], g["leg"], g["match_no"] = f"{EDN}_{stage}_{n}_{k}", k, n
            w = tie_winner(legs)
            winner_of[w] = n
            n += 1
    final = next(g for g in games if g["stage"] == "F")
    bracket.append({"match_no": 64, "stage": "F", "home": "W%d" % winner_of[final["home"]], "away": "W%d" % winner_of[final["away"]], "legs": 1})
    # winners that feed a later round must be known by team: rebuild codes now that winner_of is complete
    # (R16 codes were fine; QF/SF codes are built from winner_of of earlier rounds, which was filled in order)

    # ---- teams, ratings
    names = sorted({t for g in games for t in (g["home"], g["away"])})
    offs = fit_offsets(prev, cty_prev, kc, "2011/2012", cache)
    ratings = []
    for t in names:
        d = domestic_elo(kc, cty[t], t, "2012/2013", cache)
        ratings.append((tid(t), "2012-08-01", round((d if d is not None else 1500.0) + offs.get(cty[t], UNSEEN), 1), cty[t]))
    with open(a.out / "teams.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["team_id", "name"])
        for t in names:
            w.writerow([tid(t), tname(t)])
    with open(a.out / "ratings.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["team_id", "date", "elo"])
        for r in ratings:
            if r[0] not in ("c_juventus", "c_milan"):                   # keep the Serie A pack's ratings for these two
                w.writerow(r[:3])

    # ---- Juventus: lineups from the nearest Serie A match, scorers from the Serie A record
    sa = a.seriea
    sa_fix = {r["fixture_id"]: r for r in csv.DictReader(open(sa / "fixtures.csv", encoding="utf-8"))}
    sa_lu = defaultdict(list)
    for r in csv.DictReader(open(sa / "lineups.csv", encoding="utf-8")):
        if r["team_id"] == "c_juventus":
            sa_lu[r["fixture_id"]].append(r)
    juve_dates = sorted((f["date"], fid) for fid, f in sa_fix.items() if "c_juventus" in (f["home_team_id"], f["away_team_id"]))
    sa_ev = list(csv.DictReader(open(sa / "events.csv", encoding="utf-8")))
    goals_by = defaultdict(int)
    for e in sa_ev:
        if e["team_id"] == "c_juventus" and e["event_type"] in ("goal", "pen_goal") and e["player_id"]:
            goals_by[e["player_id"]] += 1
    sa_players = {r["player_id"]: r for r in csv.DictReader(open(sa / "players.csv", encoding="utf-8"))}
    squad = [r for r in csv.DictReader(open(sa / "squad.csv", encoding="utf-8")) if r["team_id"] == "c_juventus"]

    def ordinal(d):
        y, m, dd = map(int, d.split("-")); return y * 400 + m * 31 + dd

    lineups, events, notes, used_players = [], [], [], set()
    for g in games:
        fids = g["fixture_id"]
        juve = "Juventus" in (g["home"], g["away"])
        hs, as_ = g["hs"], g["as_"]
        ht = g["ht"] or (hs // 2 + hs % 2 and (hs // 2), as_ // 2)
        h1, a1 = (g["ht"] or (hs // 2, as_ // 2))
        sides = ((g["home"], hs, h1), (g["away"], as_, a1))
        starters = []
        if juve:
            near = min(juve_dates, key=lambda x: abs(ordinal(x[0]) - ordinal(g["date"])))[1]
            starters = [r["player_id"] for r in sa_lu[near] if r["started"] == "1"]
            for p in starters:
                lineups.append((fids, "c_juventus", p, 1, 0, "")); used_players.add(p)
            pool = [p for p in starters if sa_players[p]["position"] != "GK"]
            pool += [r["player_id"] for r in squad if r["player_id"] not in starters
                     and sa_players.get(r["player_id"], {}).get("position") == "FW"][:4]
            wts = [(p, 0.4 + goals_by.get(p, 0) * (1.0 if sa_players[p]["position"] != "DF" else 0.6)) for p in pool if p in sa_players]
        for team, sc, first in sides:
            mins = goal_minutes(rng, first, sc - first)
            for m in mins:
                pid = ""
                if juve and team == "Juventus":
                    pid = rng.choices([p for p, _ in wts], [w for _, w in wts])[0]; used_players.add(pid)
                events.append((fids, m, tid(team), pid, "goal"))
        if juve:
            notes.append((fids, "generated_details", "Real score only: Juventus lineup (the nearest Serie A match's starters), scorers and goal minutes are generated, not real"))
        else:
            notes.append((fids, "lineup_incomplete", "Real score only: no lineups, scorers or goal minutes are known for this match"))
        if sc_any := (hs + as_):
            notes.append((fids, "scorer_unknown", "Goal minutes are placed from the half-time score; scorers are unknown" if not juve else
                          "Opponent goals have no scorer; minutes are placed from the half-time score"))

    # ---- write
    def w(name, head, data):
        with open(a.out / name, "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh); wr.writerow(head); wr.writerows(data)
    fx = []
    for g in games:
        fx.append((g["fixture_id"], g["stage"], g["grp"] or "", g["leg"], g["match_no"], g["date"], tid(g["home"]), tid(g["away"]),
                   g["hs"], g["as_"], 1 if g["stage"] == "F" else 0))
    fx.sort(key=lambda r: (r[5], r[0]))
    w("fixtures.csv", ["fixture_id", "stage", "grp", "leg", "match_no", "date", "home_team_id", "away_team_id", "home_score", "away_score", "neutral"], fx)
    w("lineups.csv", ["fixture_id", "team_id", "player_id", "started", "minute_on", "minute_off"], lineups)
    w("events.csv", ["fixture_id", "minute", "team_id", "player_id", "event_type"], events)
    w("notes.csv", ["fixture_id", "kind", "note"], notes)
    w("players.csv", ["player_id", "full_name", "birth_date", "position"],
      [(p, sa_players[p]["full_name"], sa_players[p]["birth_date"], sa_players[p]["position"]) for p in sorted(used_players | {r["player_id"] for r in squad}) if p in sa_players])
    w("squad.csv", ["team_id", "player_id", "shirt_number"], [(r["team_id"], r["player_id"], r["shirt_number"]) for r in squad])

    import json
    ruleset = {
        "edition_id": EDN, "data_version": 3, "display_name": "Champions League 2012/13", "focus_team": "c_juventus",
        "source": "openfootball/champions-league (CC0) scores; details generated, see notes",
        "format": "groups_then_knockout",
        "group_stage": {"groups": sorted(groups), "teams_per_group": 4, "round_robin_legs": 2, "qualify_per_group": 2, "best_thirds": 0,
                        "points": {"win": 3, "draw": 1, "loss": 0},
                        "tiebreakers": ["points", "h2h_points", "h2h_goal_diff", "h2h_goals_for", "h2h_away_goals", "goal_diff", "goals_for", "lots"]},
        "knockout": {"two_legs": True, "away_goals": True, "extra_time": True, "penalties": True, "third_place_match": False,
                     "bracket": bracket},
        "suspensions": {"enabled": False, "note": "UEFA's disciplinary rules aren't modeled"},
        "zones": [{"positions": [1, 2], "kind": "adv", "label": "Advance to the round of 16"},
                  {"positions": [3], "kind": "el", "label": "Europa League round of 32"},
                  {"positions": [4], "kind": "out", "label": "Eliminated"}],
        "classification_note": "Ranked by points, then head-to-head points, head-to-head goal difference, head-to-head goals scored, head-to-head away goals, goal difference, goals scored.",
    }
    (a.out / "ruleset.json").write_text(json.dumps(ruleset, indent=2), encoding="utf-8")
    print(len(games), "matches;", len(names), "clubs;", len(bracket), "bracket slots;", len(events), "events;", len(lineups), "lineup rows")
    print("offsets:", {c: round(v) for c, v in sorted(offs.items())})
    print("ratings:", sorted(((r[3], r[0], r[2]) for r in ratings), key=lambda x: -x[2])[:12])


if __name__ == "__main__":
    main()
