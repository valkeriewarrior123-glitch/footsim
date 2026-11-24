"""Put the real substitutions, cards and assists of Serie A 2012/13 into the SERIEA1213 pack.

Input:  data_packs/SERIEA1213/kaggle_base/*.csv: the pack as the Kaggle European Soccer Database made it (starters
        only, goals and cards with minutes, no assists, no substitutions). This script never edits those files, so it
        can be run again.
        data_packs/SERIEA1213/tm_seriea1213.jsonl: one Transfermarkt match report per line (read in the browser, see
        claude/reading-wikipedia.md).
Output: events.csv, lineups.csv, notes.csv, players.csv, squad.csv in data_packs/SERIEA1213.
        - lineups: starters, who came on and off and when (real);
        - cards: Transfermarkt's, with their minutes, replace Kaggle's (more complete: 2,009 against 1,974);
        - goals: Kaggle's scorers and minutes stay; assists come from Transfermarkt, which also names the scorer of the
          goals Kaggle left unnamed; the two sources must agree on who scored;
        - a match Transfermarkt has no line-up for (Cagliari - Roma, 23 Sep 2012) is left exactly as Kaggle had it.
Run `python tools/merge_transfermarkt_seriea.py [SERIEA1213|SERIEA1314] [--strict]` (default SERIEA1213; the jsonl is named
tm_<edition lowercase>.jsonl).
"""
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_transfermarkt import plain, slug, day  # noqa: E402


def toks(s):
    """Name tokens; D'Ambrosio = Dambrosio, Soerensen = Sorensen."""
    return [t for t in re.split(r"[^a-z0-9]+", plain(s).replace("'", "").replace("\u2019", "").replace("oe", "o")) if len(t) > 1]


EDITION = next((a for a in sys.argv[1:] if a.startswith("SERIEA")), "SERIEA1213")
PACK = Path(__file__).resolve().parent.parent / "data_packs" / EDITION
STRICT = "--strict" in sys.argv
TEAM = {"AC Milan": "c_milan", "AC Siena": "c_siena", "ACF Fiorentina": "c_fiorentina", "AS Roma": "c_roma", "Atalanta BC": "c_atalanta",
        "Bologna FC 1909": "c_bologna", "Cagliari Calcio": "c_cagliari", "Calcio Catania": "c_catania", "Chievo Verona": "c_chievoverona",
        "Delfino Pescara 1936": "c_pescara", "Genoa CFC": "c_genoa", "Inter Milan": "c_inter", "Juventus FC": "c_juventus",
        "Parma FC": "c_parma", "SS Lazio": "c_lazio", "SSC Napoli": "c_napoli", "Torino FC": "c_torino", "UC Sampdoria": "c_sampdoria",
        "US Palermo": "c_palermo", "Udinese Calcio": "c_udinese",
        "Hellas Verona": "c_hellasverona", "AS Livorno": "c_livorno", "US Sassuolo": "c_sassuolo"}
GOALS = ("goal", "pen_goal", "own_goal")
CARDS = ("yellow", "second_yellow", "red")


def read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def main():
    base = PACK / "kaggle_base"
    fixtures, teams = read(PACK / "fixtures.csv"), read(PACK / "teams.csv")
    events, lineups, notes = read(base / "events.csv"), read(base / "lineups.csv"), read(base / "notes.csv")
    players, squad_rows = read(base / "players.csv"), read(base / "squad.csv")
    team_ids = {t["team_id"] for t in teams}
    assert team_ids <= set(TEAM.values()), team_ids - set(TEAM.values())
    pl = {p["player_id"]: dict(p) for p in players}
    tm = [json.loads(l) for l in (PACK / f"tm_{EDITION.lower()}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    fx_by = {(f["home_team_id"], f["away_team_id"]): f for f in fixtures}
    base_xi = defaultdict(list)                                   # (fixture, team) -> Kaggle starters
    pool = defaultdict(set)                                       # team -> every player the pack knows for it
    for l in lineups:
        base_xi[(l["fixture_id"], l["team_id"])].append(l["player_id"]); pool[l["team_id"]].add(l["player_id"])
    for e in events:
        if e["player_id"]:
            pool[e["team_id"]].add(e["player_id"])
    for r in squad_rows:
        pool[r["team_id"]].add(r["player_id"])
    problems, notes_out = [], []
    new_players, new_squad = {}, []
    team_map = {}                                                 # (team, tm slug) -> pid, kept the same all season

    def shared(pid, want):
        """Name tokens in common, but only when the surname agrees (a first name alone is no match)."""
        tl = toks(pl[pid]["full_name"])
        have = set(tl)
        n = len(set(want) & have)
        if not n or not (tl[-1] in want or (want and want[-1] in have)):
            return 0
        return n + (0.5 if have <= set(want) else 0)

    def best(cands, want):
        sc = sorted(((shared(p, want), p) for p in cands if shared(p, want)), reverse=True)
        if sc and (len(sc) == 1 or sc[0][0] > sc[1][0]):
            return sc[0][1]
        return None

    def sluginfo(s):
        name, _, tid = s.partition("#")
        return toks(name.replace("-", " ")), tid

    def groups(r):
        st = r["starters"]; home, away = st[:11], st[11:22]
        bh, ba, cur = [], [], None
        cur = bh
        for b in r["bench"]:
            if b["n"] == "Manager:":
                cur = ba if cur is bh else cur
                continue
            cur.append(b)
        return home, away, bh, ba

    kept_events, new_events, new_lineups, done = [], [], [], set()
    n_subs = n_cards = n_assist = n_named = 0
    counters = {"created": 0}
    weak = []
    for r in sorted(tm, key=lambda x: x["id"]):
        h, a = (TEAM[t] for t in r["teams"])
        f = fx_by[(h, a)]
        date = r["title"].rpartition(", ")[2]; d, m, y = date.split("/")
        sh, sa = map(int, r["score"].split("(")[0].split(":"))
        if (int(f["home_score"]), int(f["away_score"])) != (sh, sa) or abs(day(f["date"]) - day(f"{y}-{m}-{d}")) > 1:
            problems.append(f"{f['fixture_id']}: score or date differs from Kaggle ({r['title']} {r['score']})")
            continue
        if not r["starters"]:
            weak.append(f"{f['fixture_id']}: Transfermarkt has no line-up ({r['title']}): left as Kaggle had it (declared in the notes)")
            continue
        fid = f["fixture_id"]
        done.add(fid)
        home, away, bh, ba = groups(r)
        sides = {"H": h, "G": a}
        entries = {"H": (home, bh), "G": (away, ba)}
        for side in "HG":                                       # Transfermarkt sometimes has no eleven for one side: use Kaggle's
            if any(not e["s"] for e in entries[side][0]):
                xi = base_xi[(fid, sides[side])]
                if len(xi) != 11:
                    problems.append(f"{fid} {sides[side]}: no eleven at Transfermarkt and Kaggle's is incomplete")
                entries[side] = ([{"s": "-".join(toks(pl[p]["full_name"])) + "#kg", "n": ""} for p in xi], entries[side][1])
                weak.append(f"{fid} {sides[side]}: eleven taken from Kaggle (none at Transfermarkt)")
        slug_pid, team_override = {}, {}
        for side in "HG":
            team = sides[side]
            xi_base = list(base_xi[(fid, team)])
            used = set()
            # starters: first against that match's Kaggle eleven (which has errors: trust Transfermarkt where they differ), then the club's players, else new
            left = []
            for e in entries[side][0]:
                key = (team, e["s"])
                pid = team_map.get(key) if team_map.get(key) in xi_base and team_map.get(key) not in used else best([p for p in xi_base if p not in used], sluginfo(e["s"])[0])
                if pid:
                    used.add(pid); slug_pid[e["s"]] = pid
                else:
                    left.append(e)
            rest = [p for p in xi_base if p not in used]
            if len(left) == 1 and len(rest) == 1 and set(sluginfo(left[0]["s"])[0]) & set(toks(pl[rest[0]]["full_name"])):
                slug_pid[left[0]["s"]] = rest[0]; used.add(rest[0]); weak.append(f"{fid} {team}: {left[0]['s']} -> {rest[0]} (the only one left of the eleven, a shared name part)"); left = []
            for e in left + entries[side][1]:
                key = (team, e["s"])
                pid = team_map.get(key)
                if pid in used:
                    pid = None
                pid = pid or best([p for p in pool[team] if p not in used], sluginfo(e["s"])[0])
                if pid is None and e in entries[side][1]:       # a bench list that lost its Manager row can put a player on the wrong side
                    other = sides["G" if side == "H" else "H"]
                    pid = best([p for p in pool[other] if p not in slug_pid.values()], sluginfo(e["s"])[0])
                    if pid:
                        team_override[pid] = other
                        weak.append(f"{fid}: {e['s']} was on {team}'s bench list but plays for {other}")
                if pid is None:
                    name, _, tid = e["s"].partition("#")
                    pid = f"tm_{tid}"
                    if pid not in pl:
                        full = name.replace("-", " ").title()
                        pl[pid] = new_players[pid] = {"player_id": pid, "full_name": full, "birth_date": "", "position": "GK" if e.get("pos") == "GK" else ""}
                        counters['created'] += 1
                    pool[team].add(pid)
                    if not any(s["team_id"] == team and s["player_id"] == pid for s in new_squad):
                        new_squad.append({"team_id": team, "player_id": pid, "shirt_number": "".join(c for c in (e.get("n") or "") if c.isdigit())})
                    weak.append(f"{fid} {team}: {e['s']} -> new player {pid}")
                used.add(pid); slug_pid[e["s"]] = pid
            for s_, p_ in slug_pid.items():
                team_map[(team, s_)] = p_
            if len(xi_base) == 11 and set(xi_base) != {slug_pid[e["s"]] for e in entries[side][0]}:
                weak.append(f"{fid} {team}: Transfermarkt's eleven differs from Kaggle's (Kaggle has a player under the wrong club); Transfermarkt's used")
        def resolve(sl, prefer):
            """A Transfermarkt player the line-ups did not name (substitutes, goal scorers, assisters): find or add him."""
            if not sl or sl in slug_pid:
                return slug_pid.get(sl)
            for team in prefer:
                if team_map.get((team, sl)):
                    pid = slug_pid[sl] = team_map[(team, sl)]; return pid
            for team in prefer:
                pid = best(pool[team], sluginfo(sl)[0])
                if pid:
                    slug_pid[sl] = pid; team_map[(team, sl)] = pid; return pid
            nonlocal_new(sl, prefer[0])
            return slug_pid[sl]

        def nonlocal_new(sl, team):
            name, _, tid = sl.partition("#")
            pid = f"tm_{tid}"
            if pid not in pl:
                pl[pid] = new_players[pid] = {"player_id": pid, "full_name": name.replace("-", " ").title(), "birth_date": "", "position": ""}
                counters["created"] += 1
            pool[team].add(pid)
            new_squad.append({"team_id": team, "player_id": pid, "shirt_number": ""})
            weak.append(f"{fid} {team}: {sl} (named only in the events) -> new player {pid}")
            slug_pid[sl] = pid; team_map[(team, sl)] = pid

        for sb in r["subs"]:
            order = [sides[sb["side"]], sides["G" if sb["side"] == "H" else "H"]]
            for key in ("in", "out"):
                if sb[key] and sb[key] not in slug_pid:
                    resolve(sb[key], order[:1])
                    entries[sb["side"]][1].append({"s": sb[key], "n": "", "pos": ""})
        for g in r["goals"]:
            order = [sides[g["side"]], sides["G" if g["side"] == "H" else "H"]]
            if "Own-goal" in g["t"]:
                order = order[::-1]
            resolve(g["p"], order)
            if g["a"]:
                resolve(g["a"], [sides[g["side"]]])
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
        # cards
        side_slugs = {side: [e["s"] for e in entries[side][0] + entries[side][1]] for side in "HG"}
        ev = []
        for c in r["cards"]:
            nm = c["name"]
            ini, _, sur = nm.rpartition(".")
            want = toks(sur or nm)
            cand = [s for s in side_slugs[c["side"]] if want and (set(want) <= set(sluginfo(s)[0]) or set(want) <= set(toks(pl[slug_pid[s]]["full_name"])))]
            if len(cand) > 1 and ini:
                cand = [s for s in cand if sluginfo(s)[0] and sluginfo(s)[0][0][0] == plain(ini.strip())[:1]] or cand
            if len({slug_pid[x] for x in cand}) == 1:
                cand = cand[:1]
            if len(cand) > 1:                                   # "Danilo" is the player called just that, not Danilo Soddimo
                exact = [x for x in cand if sluginfo(x)[0] == want]
                short = [x for x in cand if len(sluginfo(x)[0]) == min(len(sluginfo(y)[0]) for y in cand)]
                cand = exact if len(exact) == 1 else (short if len(short) == 1 else cand)
            if not cand:                                        # a bench list without its Manager row can sit under the wrong side
                other = "G" if c["side"] == "H" else "H"
                cand = [s for s in side_slugs[other] if want and (set(want) <= set(sluginfo(s)[0]) or set(want) <= set(toks(pl[slug_pid[s]]["full_name"])))]
                if len({slug_pid[x] for x in cand}) == 1:
                    cand = cand[:1]
                    weak.append(f"{fid}: card for {nm!r} found on the other side's list; the {c['side']} side's team is used")
            if len(cand) != 1:
                problems.append(f"{fid}: card for {nm!r} ({c['side']}) matches {cand}")
                continue
            pid = slug_pid[cand[0]]
            kind = {"sb-gelb": "yellow", "sb-gelbrot": "second_yellow", "sb-rot": "red"}[c["type"]]
            if kind != "yellow":
                off[pid] = min(off.get(pid, 999), c["m"])
            ev.append(dict(fixture_id=fid, minute=c["m"], team_id=sides[c["side"]], player_id=pid, event_type=kind, assist_player_id=""))
            n_cards += 1
        new_events += ev
        # goals: Kaggle's scorers and minutes; Transfermarkt's assists and the scorers Kaggle lacks
        mine = sorted([e for e in events if e["fixture_id"] == fid and e["event_type"] in GOALS], key=lambda x: int(x["minute"]))
        pending = []
        tmg = sorted(r["goals"], key=lambda g: g["m"] or 0)
        gpool = [(g, slug_pid.get(g["p"]), slug_pid.get(g["a"]) if g["a"] else None) for g in tmg]
        for e in mine:
            if e["player_id"]:
                hit = next((x for x in gpool if x[1] == e["player_id"] and abs((x[0]["m"] or 0) - int(e["minute"])) <= 3), None)
                if hit is None:                                 # Kaggle credited a goal that was an own goal, or a namesake
                    sur = toks(pl[e["player_id"]]["full_name"])[-1]
                    own = [x for x in gpool if "Own-goal" in x[0]["t"] and abs((x[0]["m"] or 0) - int(e["minute"])) <= 1]
                    same = [x for x in gpool if x[1] and toks(pl[x[1]]["full_name"])[-1] == sur and abs((x[0]["m"] or 0) - int(e["minute"])) <= 2]
                    if not own and not same:                    # the same surname for the same club, whatever the minute, if only one
                        same = [x for x in gpool if x[1] and toks(pl[x[1]]["full_name"])[-1] == sur and x[0]["side"] == ("H" if e["team_id"] == h else "G")]
                        same = same if len(same) == 1 else []
                    if not own and not same and pl[e["player_id"]]["full_name"].startswith("Player "):   # Kaggle never named him
                        same = [x for x in gpool if x[0]["side"] == ("H" if e["team_id"] == h else "G") and abs((x[0]["m"] or 0) - int(e["minute"])) <= 2]
                        if len(same) == 1:
                            weak.append(f"{fid}: Kaggle's unnamed {e['player_id']} is {same[0][1]} ({pl[same[0][1]]['full_name']})")
                            pl[e["player_id"]]["full_name"] = pl[same[0][1]]["full_name"]
                            old = slug_pid.get(same[0][0]["p"])
                            slug_pid[same[0][0]["p"]] = e["player_id"]
                            if old in on:                       # his line-up row moves with him
                                on[e["player_id"]] = on.pop(old)
                            if old in off:
                                off[e["player_id"]] = off.pop(old)
                    hit = own[0] if own else (same[0] if same else None)
                    if hit is None:                             # the same scorer, but the minute is far off: as before
                        hit = next((x for x in gpool if x[1] == e["player_id"]), None)
                    if hit and own:
                        e["event_type"], e["player_id"] = "own_goal", hit[1]
                        e["team_id"] = sides["G" if hit[0]["side"] == "H" else "H"] if False else next(t for t in (h, a) if t != e["team_id"])
                        weak.append(f"{fid}: Kaggle goal at {e['minute']}' was an own goal by {hit[1]}")
                    elif hit:
                        weak.append(f"{fid}: Kaggle's {e['player_id']} and Transfermarkt's {hit[1]} treated as the same scorer (minutes {e['minute']} / {hit[0]['m']})")
            else:                                               # Kaggle left the scorer out: the closest unmatched goal for that club
                opts = [x for x in gpool if x[0]["side"] == ("H" if e["team_id"] == h else "G") and e["event_type"] != "own_goal"]
                hit = min(opts, key=lambda x: abs((x[0]["m"] or 0) - int(e["minute"])), default=None)
                if hit:
                    e["player_id"] = hit[1]; n_named += 1
            if hit is None:
                pending.append(e)
                e["assist_player_id"] = ""
                continue
            gpool.remove(hit)
            if hit[0]["m"] and abs(hit[0]["m"] - int(e["minute"])) > 3 and "Own-goal" not in hit[0]["t"]:
                weak.append(f"{fid}: goal minute differs, Kaggle {e['minute']} / Transfermarkt {hit[0]['m']} ({pl[e['player_id']]['full_name']}); Kaggle's kept")
            e["assist_player_id"] = "" if e["event_type"] == "own_goal" else (hit[2] or "")
            n_assist += bool(e["assist_player_id"])
        # Kaggle credited another player (a namesake, or an own goal that was not one): the goals left over on both sides are
        # the same goals when the counts agree and the minutes are within 2; Transfermarkt's facts about the person win
        for e in list(pending):
            hit = min(gpool, key=lambda x: abs((x[0]["m"] or 0) - int(e["minute"])), default=None)
            if len(pending) != len(gpool) or hit is None or abs((hit[0]["m"] or 0) - int(e["minute"])) > 2:
                problems.append(f"{fid}: Kaggle goal {e['player_id']} {e['minute']}' has no Transfermarkt twin")
                continue
            was = (pl[e["player_id"]]["full_name"] if e["player_id"] else "no scorer", e["event_type"])
            own_goal = "Own-goal" in hit[0]["t"]
            e["player_id"] = hit[1]
            e["event_type"] = "own_goal" if own_goal else ("goal" if e["event_type"] == "own_goal" else e["event_type"])
            e["team_id"] = (a if hit[0]["side"] == "H" else h) if own_goal else (h if hit[0]["side"] == "H" else a)
            e["assist_player_id"] = "" if own_goal else (hit[2] or "")
            n_assist += bool(e["assist_player_id"])
            gpool.remove(hit); pending.remove(e)
            weak.append(f"{fid}: Kaggle's goal at {e['minute']}' ({was[0]}, {was[1]}) is Transfermarkt's {hit[1]}" + (" (own goal)" if own_goal else ""))
        for g, p, a_ in gpool:
            problems.append(f"{fid}: Transfermarkt goal {g['p']} {g['m']}' not in Kaggle")
        for p in on:
            pass
        for side in "HG":
            for e in entries[side][0] + entries[side][1]:
                p = slug_pid[e["s"]]
                if p in on:
                    new_lineups.append(dict(fixture_id=fid, team_id=team_override.get(p, sides[side]), player_id=p, started=1 if e in entries[side][0] else 0,
                                            minute_on=on[p], minute_off=off.get(p, "")))
    for e in events:
        e.setdefault("assist_player_id", "")
    out_events = [e for e in events if not (e["fixture_id"] in done and e["event_type"] in CARDS)] + new_events
    out_events.sort(key=lambda e: (e["fixture_id"], int(e["minute"])))
    out_lineups = [l for l in lineups if l["fixture_id"] not in done] + new_lineups
    # the same real person under two ids (a name Kaggle spells differently): fold a Transfermarkt-only id into Kaggle's
    key = lambda pid: slug(pl[pid]["full_name"]).replace("oe", "o")
    kg = defaultdict(list)
    for pid in pl:
        if not pid.startswith("tm_"):
            kg[key(pid)].append(pid)
    fold = {}
    for pid in new_players:
        if len(kg.get(key(pid), [])) == 1:
            fold[pid] = kg[key(pid)][0]
    for e in out_events:
        e["player_id"] = fold.get(e["player_id"], e["player_id"]); e["assist_player_id"] = fold.get(e["assist_player_id"], e["assist_player_id"])
    for l in out_lineups:
        l["player_id"] = fold.get(l["player_id"], l["player_id"])
    new_squad = [r for r in new_squad if r["player_id"] not in fold]
    for pid, to in fold.items():
        weak.append(f"{pid} ({pl[pid]['full_name']}) folded into {to}")
        del new_players[pid]
    seen = set()
    out_lineups = [l for l in out_lineups if not ((l["fixture_id"], l["player_id"]) in seen or seen.add((l["fixture_id"], l["player_id"])))]
    unnamed = {e["fixture_id"] for e in out_events if e["event_type"] in GOALS and not e["player_id"]}
    for n in notes:
        if n["fixture_id"] in done and n["kind"] in ("subs_estimated", "lineup_incomplete"):
            continue
        if n["kind"] == "scorer_unknown" and n["fixture_id"] not in unnamed:
            continue
        notes_out.append(n)

    def write(name, head, rows):
        with open(PACK / name, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=head); w.writeheader(); w.writerows(rows)

    write("events.csv", ["fixture_id", "minute", "team_id", "player_id", "event_type", "assist_player_id"], out_events)
    write("lineups.csv", ["fixture_id", "team_id", "player_id", "started", "minute_on", "minute_off"], out_lineups)
    write("notes.csv", ["fixture_id", "kind", "note"], notes_out)
    write("players.csv", ["player_id", "full_name", "birth_date", "position"], [pl[p["player_id"]] for p in players] + list(new_players.values()))
    write("squad.csv", ["team_id", "player_id", "shirt_number"], squad_rows + new_squad)
    print(f"{len(done)} of {len(fixtures)} matches: {len(new_lineups)} lineup rows, {n_subs} substitutes, {n_cards} cards, {n_assist} assists, "
          f"{n_named} goal scorers named, {len(new_players)} new players")
    print(f"{len(weak)} placements to eyeball, {len(problems)} problems")
    (PACK / "merge_transfermarkt.log").write_text("\n".join(["PROBLEMS"] + problems + ["", "PLACEMENTS"] + weak), encoding="utf-8")
    if problems and STRICT:
        sys.exit(1)


if __name__ == "__main__":
    main()
