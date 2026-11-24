"""Put the real scorers and goal minutes of Champions League 2012/13 into the UCL1213 pack.

Run after tools/build_cl_pack.py (that script makes the scores, pairings and the generated parts).
Input:  data_packs/UCL1213/wiki_goals.txt, one match per line, copied from the match boxes of the Wikipedia pages
        "2012-13 UEFA Champions League group stage" and "... knockout phase":
        stage|date|home|score|away|home goals|away goals|attendance|referee|uefa match id
        goals look like `Name 12,45+2(pen.);Other 90(o.g.)`; an own goal is listed under the team that gained it.
        data_packs/UCL1213/uefa_lineups.txt (optional): starting elevens and benches from the UEFA full-time reports,
        `uefa id|heading|teams|score|coaches|attendance|referee|home players|away players|stats`, players as
        `number~name~flags(g goalkeeper, c captain)~x starter / b bench`; the left team is the one Wikipedia lists first.
Output: replaces the goal events (and the 'scorer unknown' notes) of every match found in the file, adds the scorers
        as players of their club, and re-words the notes. The final (not in the file) is left as it was.
"""
import csv
import re
import sys
import unicodedata
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent / "data_packs" / "UCL1213"

NAMES = {  # Wikipedia short name -> team name in teams.csv
    "Porto": "FC Porto", "Dynamo Kyiv": "Dinamo Kiev", "Montpellier": "Montpellier HSC", "Arsenal": "Arsenal FC",
    "Olympiacos": "Olympiakos Piraeus", "Schalke 04": "FC Schalke 04", "Málaga": "Málaga CF",
    "Zenit Saint Petersburg": "Zenit St. Petersburg", "Anderlecht": "RSC Anderlecht", "Ajax": "AFC Ajax",
    "Nordsjælland": "FC Nordsjælland", "Chelsea": "Chelsea FC", "Lille": "Lille OSC", "Valencia": "Valencia CF",
    "Spartak Moscow": "Spartak Moskva", "Celtic": "Celtic FC", "Benfica": "SL Benfica", "Braga": "Sporting Braga",
}


def plain(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "", plain(s))


def load_uefa():
    out = {}
    path = PACK / "uefa_lineups.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            p = line.split("|")
            side = lambda t: [dict(zip(("n", "name", "flags", "xi"), x.split("~"))) for x in t.split(";")]
            out[p[0]] = dict(left=side(p[7]), right=side(p[8]), coach=p[4], att=p[5])
    return out


# a Wikipedia scorer name that differs from UEFA's spelling
ALIAS = {"Husyev": "Gusev", "Torosidis": "Torossidis", "Seba Fernández": "Seba"}


def read(name):
    with open(PACK / name, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def main():
    fixtures, teams = read("fixtures.csv"), read("teams.csv")
    events, notes, players = read("events.csv"), read("notes.csv"), read("players.csv")
    squad = read("squad.csv")
    tid = {t["name"]: t["team_id"] for t in teams}
    pl = {p["player_id"]: p for p in players}
    juve = [p for p in players if p["player_id"] in {r["player_id"] for r in squad if r["team_id"] == "c_juventus"}]

    def team_id(wiki):
        return tid[NAMES.get(wiki, wiki)]

    def day(d):
        y, m, dd = map(int, d.split("-")); return y * 372 + m * 31 + dd

    by_key = {}
    for f in fixtures:
        by_key.setdefault((f["home_team_id"], f["away_team_id"]), []).append(f)

    new_players, scorer_of = {}, {}
    uefa = load_uefa()
    lineups = read("lineups.csv")
    squad_rows = read("squad.csv")
    squad_have = {(r["team_id"], r["player_id"]) for r in squad_rows}
    full_pid = {}                               # (team, normalised full name) -> player id, one id per real player

    def pid_for_full(team, p):
        key = (team, plain(p["name"]))
        if key in full_pid:
            return full_pid[key]
        pid = None
        if team == "c_juventus":                # the Serie A squad already has Juventus' players
            norm = plain(p["name"])
            hits = [q for q in juve if plain(q["full_name"]) == norm]
            if not hits:
                last = slug(p["name"].split()[-1])
                hits = [q for q in juve if slug(q["full_name"].split()[-1]) == last]
            if len(hits) == 1:
                pid = hits[0]["player_id"]
        if pid is None:
            pid = f"wk_{team[2:]}_{slug(p['name'])}"
            if pid not in pl and pid not in new_players:
                new_players[pid] = {"player_id": pid, "full_name": p["name"], "birth_date": "",
                                    "position": "GK" if "g" in p["flags"] else ""}
        full_pid[key] = pid
        return pid

    def from_squad(squad, name):
        """The squad member a Wikipedia scorer name stands for, or None."""
        name = ALIAS.get(name, name)
        toks = [t for t in plain(name).replace(".", " ").replace("-", " ").split() if len(t) > 1]
        hits = [q for q in squad if all(t in plain(q["name"]).replace("-", " ").split() or t in plain(q["name"]) for t in toks)]
        if len(hits) > 1:
            exact = [q for q in hits if plain(q["name"]).split()[-1] == toks[-1]]
            hits = exact or hits
        if len(hits) > 1:                                   # "Kombarov D." among twin brothers: use the initial
            initials = [t for t in plain(name).replace(".", " ").split() if len(t) == 1]
            hits = [q for q in hits if initials and plain(q["name"])[0] == initials[0]] or hits
        return hits[0] if len(hits) == 1 else None

    def player_plain(team, name):
        """The player id for a scorer: a Juventus squad member when the surname matches, else a new player of that club."""
        key = (team, slug(name))
        if key in scorer_of:
            return scorer_of[key]
        pid = None
        if team == "c_juventus":
            last = slug(name.split()[-1])
            hits = [p for p in juve if slug(p["full_name"].split()[-1]) == last]
            if len(hits) == 1:
                pid = hits[0]["player_id"]
        if pid is None:
            pid = f"wk_{team[2:]}_{slug(name)}"
            if pid not in pl and pid not in new_players:
                new_players[pid] = {"player_id": pid, "full_name": name, "birth_date": "", "position": ""}
        scorer_of[key] = pid
        return pid

    def minute(m):
        base, _, extra = m.partition("+")
        return int(base) + (int(extra) if extra else 0)

    matched, new_events, lineup_fx = set(), [], {}
    roster = []                                 # every UEFA-listed player of every match: used to place Transfermarkt data
    for line in (PACK / "wiki_goals.txt").read_text(encoding="utf-8").splitlines():
        p = line.split("|")
        stage, date, home, score, away, g_home, g_away = p[:7]
        hid, aid = team_id(home), team_id(away)
        y, m, d = map(int, date.split("-"))
        date = f"{y}-{m:02d}-{d:02d}"
        hs, as_ = map(int, score.split("–"))
        cands = [f for f in by_key.get((hid, aid), []) if abs(day(f["date"]) - day(date)) <= 1
                 and (int(f["home_score"]), int(f["away_score"])) == (hs, as_)]
        if not cands and stage == "F":      # the final is on neutral ground: the pack may list the clubs the other way round
            hid, aid, hs, as_, g_home, g_away = aid, hid, as_, hs, g_away, g_home
            cands = [f for f in by_key.get((hid, aid), []) if abs(day(f["date"]) - day(date)) <= 1]
        if len(cands) != 1:
            sys.exit(f"cannot match {line[:60]} ({len(cands)} candidates)")
        fid = cands[0]["fixture_id"]
        matched.add(fid)
        left_id, right_id = team_id(home), team_id(away)          # Wikipedia's order, which is also UEFA's left/right
        lu = uefa.get(p[9])
        squads = {}
        if lu:
            for tm, lst in ((left_id, lu["left"]), (right_id, lu["right"])):
                squads[tm] = lst
                for q in lst:
                    q["pid"] = pid_for_full(tm, q)
                    roster.append(dict(fixture_id=fid, team_id=tm, player_id=q["pid"], number=q["n"], xi=q["xi"]))
                    if (tm, q["pid"]) not in squad_have:
                        squad_have.add((tm, q["pid"])); squad_rows.append({"team_id": tm, "player_id": q["pid"], "shirt_number": q["n"]})
                lineup_fx[fid] = lineup_fx.get(fid, []) + [dict(fixture_id=fid, team_id=tm, player_id=q["pid"], started="1", minute_on="0", minute_off="")
                                                         for q in lst if q["xi"] == "x"]

        def player_for(team, name, _fid=fid, _squads=squads):                          # noqa: F811 (the lineup-aware version)
            if team in _squads:
                q = from_squad(_squads[team], name)
                if q is None:
                    sys.exit(f"{_fid}: scorer {name!r} not in the {team} squad")
                return q["pid"]
            return player_plain(team, name)

        for team, opp, goals in ((hid, aid, g_home), (aid, hid, g_away)):
            for entry in filter(None, goals.split(";")):
                name, _, mins = entry.rpartition(" ")
                for part in mins.split(","):
                    mm = re.match(r"(\d+(?:\+\d+)?)(?:\((.*)\))?$", part)
                    kind = mm.group(2)
                    if kind == "o.g.":      # the scorer plays for the other club; the goal counts for `team`
                        new_events.append((fid, minute(mm.group(1)), opp, player_for(opp, name), "own_goal"))
                    else:
                        new_events.append((fid, minute(mm.group(1)), team, player_for(team, name), "pen_goal" if kind == "pen." else "goal"))

    kept = [e for e in events if e["fixture_id"] not in matched]
    kept += [dict(fixture_id=a, minute=b, team_id=c, player_id=d, event_type=e) for a, b, c, d, e in new_events]
    kept.sort(key=lambda e: (e["fixture_id"], int(e["minute"])))

    juve_fx = {f["fixture_id"] for f in fixtures if "c_juventus" in (f["home_team_id"], f["away_team_id"])}
    out_notes = []
    for n in notes:
        if n["fixture_id"] in matched and n["kind"] == "scorer_unknown":
            continue
        if n["fixture_id"] in lineup_fx and n["kind"] in ("generated_details", "lineup_incomplete"):
            continue
        if n["fixture_id"] in matched and n["kind"] == "generated_details":
            n = dict(n, note="Real score, scorers and goal minutes (Wikipedia). Juventus' lineup is generated: the starters of the nearest Serie A match")
        elif n["fixture_id"] in matched and n["kind"] == "lineup_incomplete":
            n = dict(n, note="Real score, scorers and goal minutes (Wikipedia). Lineups, substitutions and cards are not known for this match")
        out_notes.append(n)

    for fid in lineup_fx:
        out_notes.append({"fixture_id": fid, "kind": "subs_unknown", "note": "Real score, scorers and goal minutes (Wikipedia); starting elevens (UEFA full-time report). Substitution times, cards and assists are not known: players who scored off the bench are added a minute before their first goal"})
    lineup_rows = [r for r in lineups if r["fixture_id"] not in lineup_fx] + [r for rows in lineup_fx.values() for r in rows]

    def write(name, head, rows):
        with open(PACK / name, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=head); w.writeheader(); w.writerows(rows)

    write("events.csv", ["fixture_id", "minute", "team_id", "player_id", "event_type"], kept)
    write("notes.csv", ["fixture_id", "kind", "note"], out_notes)
    write("players.csv", ["player_id", "full_name", "birth_date", "position"], players + list(new_players.values()))
    write("lineups.csv", ["fixture_id", "team_id", "player_id", "started", "minute_on", "minute_off"], lineup_rows)
    write("squad.csv", ["team_id", "player_id", "shirt_number"], squad_rows)
    write("uefa_roster.csv", ["fixture_id", "team_id", "player_id", "number", "xi"], roster)
    print(f"{len(matched)} matches ({len(matched & juve_fx)} Juventus), {len(new_events)} goals, {len(new_players)} new players, {len(lineup_fx)} with real lineups")


if __name__ == "__main__":
    main()
