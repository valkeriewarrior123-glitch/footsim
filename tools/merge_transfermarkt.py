"""Put the real substitutions, cards and assists of Champions League 2012/13 into the UCL1213 pack.

Run after tools/build_cl_pack.py and tools/merge_wiki_goals.py (the latter also writes uefa_roster.csv).
Input:  data_packs/UCL1213/tm_cl1213.jsonl, one Transfermarkt match report per line (see claude/reading-wikipedia.md for
        how it was read in the browser): teams, score, starting elevens (players as `slug#id`, shirt number), benches,
        goals with assists, substitutions and cards, all with minutes.
        data_packs/UCL1213/uefa_roster.csv: every player UEFA lists for a match, with the shirt number.
Output: real lineups (starters, who came on and off, when), cards, and assists on the goal events. Goal scorers and
        goal minutes stay as Wikipedia has them; Transfermarkt's goals are only used to find the assists, and the two
        sources must agree on who scored. Matches UEFA has no report for (Braga - Galatasaray) get their lineup here.
"""
import csv
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent / "data_packs" / "UCL1213"
STRICT = "--strict" in sys.argv


LETTERS = str.maketrans({"ı": "i", "İ": "i", "ß": "ss", "ø": "o", "ł": "l", "đ": "d", "æ": "ae", "œ": "oe"})


def plain(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.translate(LETTERS)) if unicodedata.category(c) != "Mn").lower()


def toks(s):
    return [t for t in re.split(r"[^a-z0-9]+", plain(s)) if len(t) > 1]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "", plain(s))


def read(name):
    with open(PACK / name, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def day(d):
    y, m, dd = map(int, d.split("-")); return y * 372 + m * 31 + dd


def main():
    fixtures, teams, players = read("fixtures.csv"), read("teams.csv"), read("players.csv")
    events, lineups, squad_rows, notes = read("events.csv"), read("lineups.csv"), read("squad.csv"), read("notes.csv")
    roster = defaultdict(list)                                  # (fixture, team) -> UEFA players
    for r in read("uefa_roster.csv"):
        roster[(r["fixture_id"], r["team_id"])].append(r)
    pl = {p["player_id"]: p for p in players}
    team_name = {t["team_id"]: t["name"] for t in teams}
    squad_of = defaultdict(set)
    for r in squad_rows:
        squad_of[r["team_id"]].add(r["player_id"])
    tm = [json.loads(l) for l in (PACK / "tm_cl1213.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    problems, weak = [], []

    def sluginfo(s):                                            # "lucho-gonzalez#26244" -> name tokens, id
        name, _, tid = s.partition("#")
        return toks(name.replace("-", " ")), tid

    def groups(r):
        """(home starters, away starters, home bench, away bench) as lists of dicts with slug and number."""
        st = r["starters"]; home, away = st[:11], st[11:22]
        bh, ba, cur = [], [], None
        cur = bh
        for b in r["bench"]:
            if b["n"] == "Manager:":
                cur = ba if cur is bh else cur
                continue
            cur.append(b)
        return home, away, bh, ba

    def place(entry, team, fid, used):
        """The pack player a Transfermarkt line-up entry stands for, with how sure we are ('' = sure)."""
        want, _ = sluginfo(entry["s"])
        num = (entry.get("n") or "").strip()
        ros = [x for x in roster.get((fid, team), []) if x["player_id"] not in used]

        def shared(pid):
            """Name tokens in common; a squad name wholly inside the Transfermarkt name ("Nacho") beats a half match."""
            have = set(toks(pl[pid]["full_name"]))
            n = len(set(want) & have)
            return n + (0.5 if n and have <= set(want) else 0)

        best = [x for x in ros if x["number"] == num and shared(x["player_id"])]
        if len(best) == 1:
            return best[0]["player_id"], ""
        cands = sorted(((shared(x["player_id"]), x["player_id"]) for x in ros if shared(x["player_id"])), reverse=True)
        if cands and (len(cands) == 1 or cands[0][0] > cands[1][0]):
            return cands[0][1], ""
        if not ros:                                             # no UEFA report: look through the club's whole squad
            cands = sorted(((shared(p), p) for p in squad_of[team] if p not in used and shared(p)), reverse=True)
            if cands and (len(cands) == 1 or cands[0][0] > cands[1][0]):
                return cands[0][1], "squad"
        byn = [x for x in ros if x["number"] == num]
        if len(byn) == 1:
            return byn[0]["player_id"], "number only"
        return None, "unplaced"

    def overlap(r, hteam, ateam, fid, swap):
        home, away, _, _ = groups(r)
        if swap:
            home, away = away, home
        n = 0
        for side, team in ((home, hteam), (away, ateam)):
            ros = {x["player_id"] for x in roster.get((fid, team), [])}
            for e in side:
                want, _ = sluginfo(e["s"])
                n += any(set(want) & set(toks(pl[p]["full_name"])) for p in ros)
        return n

    by_fx = {}
    for r in tm:
        title, _, date = r["title"].rpartition(", ")
        d, m, y = date.split("/"); date = f"{y}-{m}-{d}"
        sh, sa = map(int, r["score"].split("(")[0].split(":"))
        tmh, tma = [toks(x) for x in r["teams"]]
        best = None
        for f in fixtures:
            if abs(day(f["date"]) - day(date)) > 1:
                continue
            for swap in (False, True):
                a, b = (sa, sh) if swap else (sh, sa)
                if (int(f["home_score"]), int(f["away_score"])) != (a, b):
                    continue
                tname_h, tname_a = (tma, tmh) if swap else (tmh, tma)
                name_score = len(set(tname_h) & set(toks(team_name[f["home_team_id"]]))) + len(set(tname_a) & set(toks(team_name[f["away_team_id"]])))
                score = overlap(r, f["home_team_id"], f["away_team_id"], f["fixture_id"], swap) + 3 * name_score
                if best is None or score > best[0]:
                    best = (score, f, swap)
        if best is None or best[1]["fixture_id"] in by_fx:
            sys.exit(f"cannot place Transfermarkt match {r['id']} {r['title']}")
        by_fx[best[1]["fixture_id"]] = (r, best[2], best[0])

    new_lineups, new_events_by, pid_of = [], {}, {}
    assists, cards, n_subs, n_cards, n_assist = {}, [], 0, 0, 0
    extra_players, extra_squad = {}, []
    for f in fixtures:
        fid = f["fixture_id"]
        if fid not in by_fx:
            problems.append(f"{fid}: no Transfermarkt report")
            continue
        r, swap, score = by_fx[fid]
        home, away, bh, ba = groups(r)
        sides = {"H": (f["away_team_id"] if swap else f["home_team_id"]), "G": (f["home_team_id"] if swap else f["away_team_id"])}
        entries = {"H": (home, bh), "G": (away, ba)}
        slug_pid = {}
        for side in "HG":
            team = sides[side]
            used = set()
            for e in entries[side][0] + entries[side][1]:
                pid, how = place(e, team, fid, used)
                if pid is None:                                 # a player no list has: add him to his club
                    name = e["s"].split("#")[0].replace("-", " ").title()
                    pid = f"wk_{team[2:]}_{slug(name)}"
                    if pid not in pl and pid not in extra_players:
                        extra_players[pid] = {"player_id": pid, "full_name": name, "birth_date": "", "position": ""}
                        pl[pid] = extra_players[pid]
                    if team not in squad_of or pid not in squad_of[team]:
                        extra_squad.append({"team_id": team, "player_id": pid, "shirt_number": (e.get("n") or "").strip()}); squad_of[team].add(pid)
                    how = "new player"
                if how:
                    weak.append(f"{fid} {team_name[team]}: {e['s']} #{e.get('n')} -> {pid} ({how})")
                used.add(pid); slug_pid[e["s"]] = pid
        # starters must be the same eleven as UEFA's, where UEFA has a report
        for side in "HG":
            team = sides[side]
            uefa_xi = {x["player_id"] for x in roster.get((fid, team), []) if x["xi"] == "x"}
            tm_xi = {slug_pid[e["s"]] for e in entries[side][0]}
            if uefa_xi and uefa_xi != tm_xi:
                problems.append(f"{fid} {team_name[team]}: XI differs from UEFA: only UEFA {sorted(uefa_xi - tm_xi)}, only TM {sorted(tm_xi - uefa_xi)}")
        # on / off
        on, off = {}, {}
        for side in "HG":
            for e in entries[side][0]:
                on[slug_pid[e["s"]]] = 0
        for s in sorted(r["subs"], key=lambda x: x["m"]):
            if s["out"]:
                off[slug_pid[s["out"]]] = s["m"]
            if s["in"]:
                on[slug_pid[s["in"]]] = max(1, s["m"]); n_subs += 1
        # cards: find the player among the side's lineup + bench by "A.Surname" / "Surname"
        side_slugs = {side: [e["s"] for e in entries[side][0] + entries[side][1]] for side in "HG"}
        ev = []
        for c in r["cards"]:
            nm = c["name"]
            ini, _, sur = nm.rpartition(".")
            want = toks(sur or nm)
            cand = [s for s in side_slugs[c["side"]] if want and (set(want) <= set(sluginfo(s)[0]) or set(want) <= set(toks(pl[slug_pid[s]]["full_name"])))]
            if len(cand) > 1 and ini:
                cand = [s for s in cand if sluginfo(s)[0] and sluginfo(s)[0][0][0] == plain(ini.strip())[:1]] or cand
            if len(cand) != 1:
                problems.append(f"{fid}: card for {nm!r} ({c['side']}) matches {cand}")
                continue
            pid = slug_pid[cand[0]]
            kind = {"sb-gelb": "yellow", "sb-gelbrot": "second_yellow", "sb-rot": "red"}[c["type"]]
            if kind != "yellow":
                off[pid] = min(off.get(pid, 999), c["m"])
            ev.append(dict(fixture_id=fid, minute=c["m"], team_id=sides[c["side"]], player_id=pid, event_type=kind, assist_player_id=""))
            n_cards += 1
        # goals: Wikipedia's scorers must be Transfermarkt's scorers; assists come from Transfermarkt
        mine = [e for e in events if e["fixture_id"] == fid]
        tm_goals = sorted(r["goals"], key=lambda g: g["m"])
        pool = [(g, slug_pid.get(g["p"]), slug_pid.get(g["a"]) if g["a"] else None) for g in tm_goals]
        for e in sorted(mine, key=lambda x: int(x["minute"])):
            hit = next((x for x in pool if x[1] == e["player_id"]), None)
            if hit is None:                                     # wiki says "Burak", Transfermarkt knows Burak Yilmaz
                wt = set(toks(pl[e["player_id"]]["full_name"]))
                near = [x for x in pool if x[1] and wt and wt <= set(toks(pl[x[1]]["full_name"])) and x[0]["side"] == ("H" if e["team_id"] == sides["H"] else "G")]
                if len(near) == 1:
                    hit = near[0]; e["player_id"] = hit[1]
            if hit is None:
                problems.append(f"{fid}: Wikipedia goal {e['player_id']} {e['minute']}' has no Transfermarkt twin")
                e["assist_player_id"] = ""
                continue
            pool.remove(hit)
            e["assist_player_id"] = "" if e["event_type"] == "own_goal" else (hit[2] or "")
            n_assist += bool(e["assist_player_id"])
        for g, p, a in pool:
            problems.append(f"{fid}: Transfermarkt goal {g['p']} {g['m']}' not in Wikipedia")
        new_events_by[fid] = ev
        # lineup rows: everyone who played
        for side in "HG":
            team = sides[side]
            for e in entries[side][0] + entries[side][1]:
                p = slug_pid[e["s"]]
                if p not in on:
                    continue
                new_lineups.append(dict(fixture_id=fid, team_id=team, player_id=p, started=1 if on[p] == 0 and e in entries[side][0] else 0,
                                        minute_on=on[p], minute_off=off.get(p, "")))
        pid_of[fid] = slug_pid

    for e in events:
        e.setdefault("assist_player_id", "")
    out_events = [e for e in events] + [c for ev in new_events_by.values() for c in ev]
    out_events.sort(key=lambda e: (e["fixture_id"], int(e["minute"])))
    fx_done = set(new_events_by)
    out_lineups = [l for l in lineups if l["fixture_id"] not in fx_done] + new_lineups
    out_notes = [n for n in notes if not (n["fixture_id"] in fx_done and n["kind"] in ("subs_unknown", "lineup_incomplete", "generated_details"))]

    used = {l["player_id"] for l in out_lineups} | {e["player_id"] for e in out_events} | {e["assist_player_id"] for e in out_events} | {r["player_id"] for r in squad_rows + extra_squad}
    all_players = [p for p in players + list(extra_players.values()) if p["player_id"] in used or not p["player_id"].startswith("wk_")]

    def write(name, head, rows):
        with open(PACK / name, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=head); w.writeheader(); w.writerows(rows)

    write("events.csv", ["fixture_id", "minute", "team_id", "player_id", "event_type", "assist_player_id"], out_events)
    write("lineups.csv", ["fixture_id", "team_id", "player_id", "started", "minute_on", "minute_off"], out_lineups)
    write("notes.csv", ["fixture_id", "kind", "note"], out_notes)
    write("players.csv", ["player_id", "full_name", "birth_date", "position"], all_players)
    write("squad.csv", ["team_id", "player_id", "shirt_number"], squad_rows + extra_squad)
    print(f"{len(fx_done)} matches: {len(new_lineups)} lineup rows, {n_subs} substitutes, {n_cards} cards, {n_assist} assists, {len(extra_players)} new players, {len(players) + len(extra_players) - len(all_players)} unused scorer stubs dropped")
    print(f"{len(weak)} placements to eyeball, {len(problems)} problems")
    (PACK.parent.parent / "tools" / "merge_transfermarkt.log").write_text("\n".join(["PROBLEMS"] + problems + ["", "PLACEMENTS"] + weak), encoding="utf-8")
    if problems and STRICT:
        sys.exit(1)


if __name__ == "__main__":
    main()
