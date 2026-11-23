"""Import a CSV club data pack (made by tools/build_club_pack.py) as one edition.

    python -m footsim.import_club_pack data_packs/SERIEA1213 [--db footsim.db]

The pack's meta lives in its folder name and ruleset.json. Source facts (scores, starters, goals, cards, squad) are
imported as they are. The source has no substitutions, so a substitute who scored or was carded gets an *estimated*
entry time (a minute before his first event) and a starter of the same position group goes off then. Every estimate
and gap is also stored in data_notes so the app can say so.
"""
import argparse, csv, json
from collections import defaultdict
from pathlib import Path

from footsim import db

COMPETITIONS = {"SERIEA": ("SA", "Serie A", "club", "UEFA", "Italy"),
                "UCL": ("UCL", "UEFA Champions League", "club", "UEFA", "Europe")}


def _rows(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def import_pack(conn, pack: Path) -> dict:
    pack = Path(pack)
    rules = json.loads((pack / "ruleset.json").read_text(encoding="utf-8"))
    edition = rules["edition_id"]
    comp_key = "".join(ch for ch in edition if ch.isalpha())
    cid, cname, kind, confed, country = COMPETITIONS[comp_key]
    fixtures, teams = _rows(pack / "fixtures.csv"), _rows(pack / "teams.csv")
    players = {r["player_id"]: r for r in _rows(pack / "players.csv")}
    lineups, events = _rows(pack / "lineups.csv"), _rows(pack / "events.csv")
    squad, notes = _rows(pack / "squad.csv"), _rows(pack / "notes.csv")
    dates = [f["date"] for f in fixtures]
    season = f"{min(dates)[:4]}-{max(dates)[2:4]}"
    conn.execute("INSERT OR IGNORE INTO competitions VALUES (?,?,?,?)", (cid, cname, kind, confed))
    conn.execute("INSERT OR REPLACE INTO editions VALUES (?,?,?,?,?,?,?)",
                 (edition, cid, season, min(dates), max(dates), int(db.is_locked(min(dates))),
                  "Kaggle European Soccer Database (ODbL) + footballcsv squad cache; see data_notes"))
    for t in teams:
        conn.execute("INSERT OR IGNORE INTO teams VALUES (?,?,?,?, 'club')", (t["team_id"], t["name"], country, confed))
    for f in fixtures:
        # a league pack has only the basic columns; a cup pack adds stage, group, leg, match number and venue
        conn.execute("""INSERT OR REPLACE INTO fixtures (fixture_id, edition_id, stage, grp, leg, match_no, date, home_team_id,
                        away_team_id, home_score, away_score, extra_time, pen_home, pen_away, neutral_venue)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (f["fixture_id"], edition, f.get("stage") or "group", (f["grp"] if "grp" in f else "A") or None,
                      int(f["leg"]) if f.get("leg") else None, int(f["match_no"]) if f.get("match_no") else None,
                      f["date"], f["home_team_id"], f["away_team_id"], int(f["home_score"]), int(f["away_score"]),
                      int(f.get("extra_time") or 0), int(f["pen_home"]) if f.get("pen_home") else None,
                      int(f["pen_away"]) if f.get("pen_away") else None, int(f.get("neutral") or 0)))
    ev_by, lu_by = defaultdict(list), defaultdict(list)
    for e in events:
        ev_by[e["fixture_id"]].append(e)
    for l in lineups:
        lu_by[(l["fixture_id"], l["team_id"])].append(l)

    n_sub = 0
    for f in fixtures:
        fid = f["fixture_id"]
        conn.execute("DELETE FROM lineups WHERE fixture_id = ?", (fid,)); conn.execute("DELETE FROM match_events WHERE fixture_id = ?", (fid,))
        for tid in (f["home_team_id"], f["away_team_id"]):
            starters = {l["player_id"]: dict(on=0, off=None) for l in lu_by[(fid, tid)]}
            mine = [e for e in ev_by[fid] if e["team_id"] == tid and e["player_id"] and e["event_type"] != "own_goal"]
            mine += [e for e in ev_by[fid] if e["team_id"] != tid and e["player_id"] and e["event_type"] == "own_goal" and False]
            busy = {e["player_id"] for e in mine}
            for e in mine:                                   # a red card ends a starter's match
                if e["event_type"] in ("red", "second_yellow") and e["player_id"] in starters:
                    starters[e["player_id"]]["off"] = int(e["minute"])
            subs = {}
            for e in sorted(mine, key=lambda x: int(x["minute"])):
                p = e["player_id"]
                if p in starters or p in subs:
                    continue
                m = max(1, int(e["minute"]) - 1)
                group = players[p]["position"] or "MF"
                cand = [s for s in starters if starters[s]["off"] is None and s not in busy
                        and (players[s]["position"] or "MF") == group and group != "GK"]
                if p.startswith("wk_") and not players[p]["position"]:
                    cand = []                       # position unknown (UEFA reports have none): nobody is guessed off
                if cand:
                    starters[cand[-1]]["off"] = m
                subs[p] = dict(on=m, off=None); n_sub += 1
            # own goals are scored by a player of tid, listed under tid in events.csv; make sure he is on the pitch
            for e in ev_by[fid]:
                if e["team_id"] == tid and e["event_type"] == "own_goal" and e["player_id"] and e["player_id"] not in starters and e["player_id"] not in subs:
                    subs[e["player_id"]] = dict(on=max(1, int(e["minute"]) - 1), off=None); n_sub += 1
            for p in list(starters) + list(subs):
                _ensure_player(conn, players, p)
            for p, s in starters.items():
                conn.execute("INSERT INTO lineups VALUES (?,?,?,1,0,?)", (fid, tid, p, s["off"]))
            for p, s in subs.items():
                conn.execute("INSERT INTO lineups VALUES (?,?,?,0,?,NULL)", (fid, tid, p, s["on"]))
        for e in ev_by[fid]:
            if e["player_id"]:
                _ensure_player(conn, players, e["player_id"])
            conn.execute("INSERT INTO match_events (fixture_id, minute, added_time, team_id, player_id, event_type, assist_player_id) "
                         "VALUES (?,?,NULL,?,?,?,NULL)", (fid, int(e["minute"]), e["team_id"], e["player_id"] or None, e["event_type"]))
    # squads: the listed club squad, and for every other club the players seen in its matches
    conn.execute("DELETE FROM squads WHERE edition_id = ?", (edition,))
    listed = {r["team_id"] for r in squad}
    for r in squad:
        _ensure_player(conn, players, r["player_id"])
        conn.execute("INSERT OR IGNORE INTO squads VALUES (?,?,?,?)", (edition, r["team_id"], r["player_id"], int(r["shirt_number"]) if r["shirt_number"] else None))
    for tid, p in conn.execute("SELECT DISTINCT team_id, player_id FROM lineups WHERE fixture_id LIKE ?", (edition + "_%",)).fetchall():
        if tid not in listed:
            conn.execute("INSERT OR IGNORE INTO squads VALUES (?,?,?,NULL)", (edition, tid, p))
    if (pack / "ratings.csv").exists():
        for r in _rows(pack / "ratings.csv"):
            conn.execute("INSERT OR REPLACE INTO team_ratings VALUES (?,?,?,?)", (r["team_id"], r["date"], float(r["elo"]),
                         f"club strength going into {edition}: estimated from earlier seasons (see the pack's builder)"))
    conn.execute("DELETE FROM data_notes WHERE edition_id = ?", (edition,))
    for n in notes:
        conn.execute("INSERT INTO data_notes VALUES (?,?,?,?)", (edition, n["fixture_id"] or None, n["kind"], n["note"]))
    conn.commit()
    db.load_ruleset(conn, pack / "ruleset.json")
    return {"fixtures": len(fixtures), "teams": len(teams), "lineups": conn.execute("SELECT COUNT(*) FROM lineups WHERE fixture_id LIKE ?", (edition + "_%",)).fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM match_events WHERE fixture_id LIKE ?", (edition + "_%",)).fetchone()[0],
            "estimated_substitutes": n_sub, "notes": len(notes)}


def _ensure_player(conn, players, pid):
    p = players[pid]
    conn.execute("""INSERT INTO players (player_id, full_name, known_as, birth_date, nationality, position, is_created)
                    VALUES (?,?,NULL,?,NULL,?,0)
                    ON CONFLICT(player_id) DO UPDATE SET birth_date = COALESCE(players.birth_date, excluded.birth_date),
                      position = COALESCE(players.position, excluded.position)""",
                 (pid, p["full_name"], p["birth_date"] or None, p["position"] or None))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack", type=Path); ap.add_argument("--db", type=Path, default=db.DEFAULT_DB)
    a = ap.parse_args()
    conn = db.init_db(a.db)
    print("Imported", Path(a.pack).name + ":", ", ".join(f"{v} {k}" for k, v in import_pack(conn, a.pack).items()))


if __name__ == "__main__":
    main()
