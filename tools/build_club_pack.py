"""Turn the Kaggle "European Soccer Database" (a 313 MB SQLite file, not kept in the repo) into a small CSV data pack
for one league season, plus the squad list for one club from the footballcsv squad cache.

    python tools/build_club_pack.py --kaggle european_soccer.sqlite --league 10257 --season 2012/2013 \
        --edition SERIEA1213 --squad-club juventus --squad-file italy/2012-2013/seriea/juventus.txt \
        --out data_packs/SERIEA1213

What the source has: final scores, starting elevens, goals (scorer, minute, type), cards. What it lacks: assists,
substitutions, minutes played. Where a match's goal list doesn't add up to its score the missing goal is written with
no scorer, and every estimate is listed in notes.csv so the app can say so.
"""
import argparse, csv, difflib, re, sqlite3, unicodedata, xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path


def plain(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s).split()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "", "".join(plain(s)))


def pos_from_y(y):
    return "GK" if y <= 1 else "DF" if y <= 4 else "MF" if y <= 8 else "FW"


def parse_squad(path):
    out, past = [], False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("== Past"):
            break
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        d, m, y = parts[6].split("-")
        y = int(y); y += 1900 if y >= 40 else 2000
        out.append(dict(number=int(parts[0]), name=parts[1], nat=parts[2],
                        pos={"G": "GK", "D": "DF", "M": "MF", "F": "FW"}.get(parts[3], None), born=f"{y:04d}-{int(m):02d}-{int(d):02d}"))
    return out


def players_name(pinfo, p):
    return pinfo.get(p, (f"Player {p}",))[0]


def teams_by_id(teams, tid):
    return next((v[1] for v in teams.values() if v[0] == tid), tid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kaggle", type=Path, required=True); ap.add_argument("--league", type=int, required=True)
    ap.add_argument("--season", required=True); ap.add_argument("--edition", required=True)
    ap.add_argument("--squad-club", default=""); ap.add_argument("--squad-file", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    c = sqlite3.connect(f"file:{a.kaggle}?mode=ro", uri=True); c.row_factory = sqlite3.Row
    teams = {r["team_api_id"]: (f"c_{slug(r['team_long_name'])}", r["team_long_name"]) for r in c.execute("SELECT * FROM Team")}
    pinfo = {r["player_api_id"]: (r["player_name"], (r["birthday"] or "")[:10]) for r in c.execute("SELECT * FROM Player")}
    rows = c.execute("SELECT * FROM Match WHERE league_id=? AND season=? ORDER BY date, id", (a.league, a.season)).fetchall()
    ys = defaultdict(list)                                   # player -> y coordinate of each start
    fixtures, lineups, events, notes = [], [], [], []
    rnd = Counter()
    for r in rows:
        h, aw = r["home_team_api_id"], r["away_team_api_id"]
        fid = f"{a.edition}_{r['match_api_id']}"
        d = r["date"][:10]
        starters = {}
        for side, tid in (("home", h), ("away", aw)):
            ids = [(r[f"{side}_player_{i}"], r[f"{side}_player_Y{i}"]) for i in range(1, 12)]
            starters[tid] = [p for p, _ in ids if p]
            for p, y in ids:
                if p and y is not None:
                    ys[p].append(y)
            if len(starters[tid]) < 11:
                notes.append((fid, "lineup_incomplete", f"{teams[tid][1]}: the source lists only {len(starters[tid])} starters"))
        ev = []
        goals = [0, 0]
        if r["goal"]:
            for v in ET.fromstring(r["goal"]).findall("value"):
                t, el = v.findtext("goal_type"), v.findtext("elapsed")
                if t is None or v.find("del") is not None or not v.findtext("player1"):
                    continue
                if not v.findtext("player1").isdigit():
                    continue
                p, team, minute = int(v.findtext("player1")), int(v.findtext("team")), int(el)
                kind = {"n": "goal", "p": "pen_goal", "npm": "pen_miss", "o": "own_goal"}[t]
                ev.append([minute, team, p, kind])
                if kind != "pen_miss":
                    side = 0 if team == h else 1
                    goals[(1 - side) if kind == "own_goal" else side] += 1
        for side, tid, want in ((0, h, r["home_team_goal"]), (1, aw, r["away_team_goal"])):
            for _ in range(want - goals[side]):
                ev.append([45, tid, None, "goal"])
                notes.append((fid, "scorer_unknown", f"{teams[tid][1]}: one goal has no scorer in the source (placed at 45')"))
            if goals[side] > want:
                raise SystemExit(f"{fid}: more goal events than the score")
        if r["card"]:
            for v in ET.fromstring(r["card"]).findall("value"):
                t = v.findtext("card_type"); p = v.findtext("player1")
                if t in ("y", "y2", "r") and p and p.isdigit():
                    ev.append([int(v.findtext("elapsed")), int(v.findtext("team")), int(p),
                               {"y": "yellow", "y2": "second_yellow", "r": "red"}[t]])
        ev.sort(key=lambda e: e[0])
        rnd[h] += 1; rnd[aw] += 1
        fixtures.append([fid, r["stage"], d, teams[h][0], teams[aw][0], r["home_team_goal"], r["away_team_goal"]])
        for tid in (h, aw):
            for p in starters[tid]:
                lineups.append([fid, teams[tid][0], p, 1, 0, ""])
        for e in ev:
            events.append([fid, e[0], teams[e[1]][0], e[2], e[3]])
        notes.append((fid, "subs_estimated", "The source has no substitutions: substitutes who scored or were carded get an estimated entry time"))

    # the source occasionally lists one player for both teams in a match: keep him with the team he plays for the rest of the season
    home_of = defaultdict(Counter)
    for l in lineups:
        home_of[l[2]][l[1]] += 1
    seen_fp, keep = {}, []
    for l in lineups:
        key = (l[0], l[2])
        dup = sum(1 for x in lineups if (x[0], x[2]) == key) > 1 if key not in seen_fp else seen_fp[key]
        seen_fp[key] = dup
        if dup and home_of[l[2]].most_common(1)[0][0] != l[1]:
            notes.append((l[0], "lineup_incomplete", f"{players_name(pinfo, l[2])} was listed for both teams; kept with {teams_by_id(teams, home_of[l[2]].most_common(1)[0][0])} only"))
            continue
        keep.append(l)
    lineups = keep
    # squad of one club (bench players who never started come from the squad list)
    squad = []
    if a.squad_club:
        squad = parse_squad(a.squad_file)
    pos_modal = {p: pos_from_y(Counter(round(y) for y in v).most_common(1)[0][0]) for p, v in ys.items()}
    # every player seen
    seen = {int(l[2]) for l in lineups} | {int(e[3]) for e in events if e[3]}
    players = {}
    for p in seen:
        nm, born = pinfo.get(p, (f"Player {p}", ""))
        players[f"kg_{p}"] = dict(name=nm, born=born, pos=pos_modal.get(p, ""), num="")
    club_id = next((v[0] for k, v in teams.items() if slug(v[1]) == a.squad_club), None)
    # match the squad list to source players by name; unmatched ones become new players
    club_team = [pid for pid, p in players.items() if any(l[2] == int(pid[3:]) and l[1] == club_id for l in lineups)] if False else []
    club_pids = {f"kg_{l[2]}" for l in lineups if l[1] == club_id}
    club_pids |= {f"kg_{e[3]}" for e in events if e[2] == club_id and e[3]}
    squad_rows = []
    unmatched = []
    for s in squad:
        toks = set(plain(s["name"]))
        best, score = None, 0
        for pid in club_pids:
            nt = set(plain(players[pid]["name"]))
            sc = len(toks & nt) / max(1, min(len(toks), len(nt))) if (toks <= nt or nt <= toks) else \
                difflib.SequenceMatcher(None, " ".join(sorted(toks)), " ".join(sorted(nt))).ratio()
            if sc > score:
                best, score = pid, sc
        if best and score >= 0.8:
            pl = players[best]; pl["pos"] = s["pos"] or pl["pos"]; pl["born"] = s["born"] or pl["born"]; pl["num"] = s["number"]
            squad_rows.append((best, s["number"]))
        else:
            pid = f"fs_{slug(s['name'])}"
            players[pid] = dict(name=s["name"], born=s["born"], pos=s["pos"] or "", num=s["number"])
            squad_rows.append((pid, s["number"])); unmatched.append(s["name"])
    in_squad = {p for p, _ in squad_rows}
    for pid in sorted(club_pids - in_squad):
        squad_rows.append((pid, ""))
        notes.append(("", "added_to_squad", f"{players[pid]['name']} played for {a.squad_club} but is not on the season squad list; added without a shirt number"))
    out = a.out; out.mkdir(parents=True, exist_ok=True)
    def w(name, head, data):
        with open(out / name, "w", encoding="utf-8", newline="") as fh:
            cw = csv.writer(fh); cw.writerow(head); cw.writerows(data)
    used_teams = sorted({(teams[t][0], teams[t][1]) for t in teams if teams[t][0] in {f[3] for f in fixtures} | {f[4] for f in fixtures}})
    w("teams.csv", ["team_id", "name"], used_teams)
    w("players.csv", ["player_id", "full_name", "birth_date", "position"], [(k, v["name"], v["born"], v["pos"]) for k, v in sorted(players.items())])
    w("fixtures.csv", ["fixture_id", "round", "date", "home_team_id", "away_team_id", "home_score", "away_score"], fixtures)
    w("lineups.csv", ["fixture_id", "team_id", "player_id", "started", "minute_on", "minute_off"], [[l[0], l[1], f"kg_{l[2]}", l[3], l[4], l[5]] for l in lineups])
    w("events.csv", ["fixture_id", "minute", "team_id", "player_id", "event_type"], [[e[0], e[1], e[2], (f"kg_{e[3]}" if e[3] else ""), e[4]] for e in events])
    w("squad.csv", ["team_id", "player_id", "shirt_number"], [(club_id, p, n) for p, n in squad_rows])
    w("notes.csv", ["fixture_id", "kind", "note"], notes)
    print(f"{len(fixtures)} fixtures, {len(lineups)} lineup rows, {len(events)} events, {len(players)} players; squad {len(squad_rows)} "
          f"({len(unmatched)} not found in the source: {', '.join(unmatched[:12])})")
    print("notes:", dict(Counter(n[1] for n in notes)))


if __name__ == "__main__":
    main()
