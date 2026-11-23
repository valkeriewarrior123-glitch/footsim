"""Put the real scorers and goal minutes of Champions League 2012/13 into the UCL1213 pack.

Run after tools/build_cl_pack.py (that script makes the scores, pairings and the generated parts).
Input:  data_packs/UCL1213/wiki_goals.txt, one match per line, copied from the match boxes of the Wikipedia pages
        "2012-13 UEFA Champions League group stage" and "... knockout phase":
        stage|date|home|score|away|home goals|away goals|attendance|referee|uefa match id
        goals look like `Name 12,45+2(pen.);Other 90(o.g.)`; an own goal is listed under the team that gained it.
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

    def player_for(team, name):
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

    matched, new_events = set(), []
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
        if n["fixture_id"] in matched and n["kind"] == "generated_details":
            n = dict(n, note="Real score, scorers and goal minutes (Wikipedia). Juventus' lineup is generated: the starters of the nearest Serie A match")
        elif n["fixture_id"] in matched and n["kind"] == "lineup_incomplete":
            n = dict(n, note="Real score, scorers and goal minutes (Wikipedia). Lineups, substitutions and cards are not known for this match")
        out_notes.append(n)

    def write(name, head, rows):
        with open(PACK / name, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=head); w.writeheader(); w.writerows(rows)

    write("events.csv", ["fixture_id", "minute", "team_id", "player_id", "event_type"], kept)
    write("notes.csv", ["fixture_id", "kind", "note"], out_notes)
    write("players.csv", ["player_id", "full_name", "birth_date", "position"], players + list(new_players.values()))
    print(f"{len(matched)} matches ({len(matched & juve_fx)} Juventus), {len(new_events)} goals, {len(new_players)} new players")


if __name__ == "__main__":
    main()
