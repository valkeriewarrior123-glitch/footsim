"""Import one StatsBomb open-data competition season into the baseline tables.

Usage:
    python -m footsim.import_statsbomb <open-data/data folder> [--db footsim.db]

Currently configured for FIFA World Cup 2022 (StatsBomb competition 43, season 106).
"""
import argparse
import json
from pathlib import Path

from footsim import db

SB_COMPETITION, SB_SEASON = 43, 106
EDITION = "WC2022"
SOURCE = "StatsBomb open data (github.com/statsbomb/open-data)"

STAGES = {
    "Group Stage": "group", "Round of 16": "R16", "Quarter-finals": "QF",
    "Semi-finals": "SF", "3rd Place Final": "3P", "Final": "F",
}

# StatsBomb team name -> FIFA code
TEAM_CODES = {
    "Argentina": "ARG", "Australia": "AUS", "Belgium": "BEL", "Brazil": "BRA",
    "Cameroon": "CMR", "Canada": "CAN", "Costa Rica": "CRC", "Croatia": "CRO",
    "Denmark": "DEN", "Ecuador": "ECU", "England": "ENG", "France": "FRA",
    "Germany": "GER", "Ghana": "GHA", "Iran": "IRN", "Japan": "JPN",
    "Mexico": "MEX", "Morocco": "MAR", "Netherlands": "NED", "Poland": "POL",
    "Portugal": "POR", "Qatar": "QAT", "Saudi Arabia": "KSA", "Senegal": "SEN",
    "Serbia": "SRB", "South Korea": "KOR", "Spain": "ESP", "Switzerland": "SUI",
    "Tunisia": "TUN", "United States": "USA", "Uruguay": "URU", "Wales": "WAL",
}

CONFED = {
    "UEFA": ["BEL", "CRO", "DEN", "ENG", "FRA", "GER", "NED", "POL", "POR", "SRB", "ESP", "SUI", "WAL"],
    "CONMEBOL": ["ARG", "BRA", "ECU", "URU"],
    "CAF": ["CMR", "GHA", "MAR", "SEN", "TUN"],
    "AFC": ["AUS", "IRN", "JPN", "QAT", "KSA", "KOR"],
    "CONCACAF": ["CAN", "CRC", "MEX", "USA"],
}
CONFED_OF = {code: c for c, codes in CONFED.items() for code in codes}

CARD_TYPES = {"Yellow Card": "yellow", "Second Yellow": "second_yellow", "Red Card": "red"}


def team_code(name: str) -> str:
    if name not in TEAM_CODES:
        raise KeyError(f"No team code for '{name}'. Add it to TEAM_CODES.")
    return TEAM_CODES[name]


def pid(sb_id) -> str:
    return f"sb_{sb_id}"


def position_group(name: str) -> str:
    if name == "Goalkeeper":
        return "GK"
    if "Back" in name:
        return "DF"
    if "Midfield" in name:
        return "MF"
    return "FW"


def minute_of(clock: str) -> int:
    return int(clock.split(":")[0])


def import_season(data_dir: Path, conn) -> dict:
    data_dir = Path(data_dir)
    matches = json.loads((data_dir / "matches" / str(SB_COMPETITION) / f"{SB_SEASON}.json").read_text(encoding="utf-8"))
    matches.sort(key=lambda m: (m["match_date"], m["kick_off"] or ""))

    conn.execute("INSERT OR IGNORE INTO competitions VALUES ('WC','FIFA World Cup','international','FIFA')")
    dates = [m["match_date"] for m in matches]
    conn.execute(
        "INSERT OR REPLACE INTO editions VALUES (?,?,?,?,?,?,?)",
        (EDITION, "WC", "2022", min(dates), max(dates), int(db.is_locked(min(dates))), SOURCE),
    )

    # Team -> group letter, from every match that has it (StatsBomb leaves a few blank)
    team_group = {}
    for m in matches:
        for side in ("home", "away"):
            g = (m[f"{side}_team"].get(f"{side}_team_group") or "").replace("Group ", "")
            if g:
                team_group[m[f"{side}_team"][f"{side}_team_name"]] = g

    counts = {"fixtures": 0, "players": 0, "events": 0, "lineups": 0}
    seen_players = set()

    for m in matches:
        fid = f"{EDITION}_{m['match_id']}"
        home_name, away_name = m["home_team"]["home_team_name"], m["away_team"]["away_team_name"]
        home, away = team_code(home_name), team_code(away_name)
        for code, name in ((home, home_name), (away, away_name)):
            conn.execute("INSERT OR IGNORE INTO teams VALUES (?,?,?,?, 'national')",
                         (code, name, name, CONFED_OF.get(code)))

        stage = STAGES[m["competition_stage"]["name"]]
        group = None
        if stage == "group":
            group = team_group.get(home_name) or team_group.get(away_name)

        events = json.loads((data_dir / "events" / f"{m['match_id']}.json").read_text(encoding="utf-8"))
        lineups = json.loads((data_dir / "lineups" / f"{m['match_id']}.json").read_text(encoding="utf-8"))

        extra_time = int(any(e.get("period") in (3, 4) for e in events))
        pen = {home: 0, away: 0}
        shootout = False
        by_id = {e["id"]: e for e in events}
        event_rows = []

        for e in events:
            etype = e["type"]["name"]
            period = e.get("period")
            team = team_code(e["team"]["name"]) if "team" in e else None
            player = pid(e["player"]["id"]) if "player" in e else None
            minute = e.get("minute", 0)

            if etype == "Shot":
                outcome = e["shot"]["outcome"]["name"]
                is_pen = e["shot"]["type"]["name"] == "Penalty"
                if period == 5:
                    shootout = True
                    if outcome == "Goal":
                        pen[team] += 1
                    event_rows.append((minute, team, player,
                                       "shootout_score" if outcome == "Goal" else "shootout_miss", None))
                    continue
                if outcome == "Goal":
                    assist = None
                    kp = e["shot"].get("key_pass_id")
                    if kp and by_id.get(kp, {}).get("pass", {}).get("goal_assist"):
                        assist = pid(by_id[kp]["player"]["id"])
                    event_rows.append((minute, team, player, "pen_goal" if is_pen else "goal", assist))
                elif is_pen:
                    event_rows.append((minute, team, player, "pen_miss", None))
            elif etype == "Own Goal Against":
                # credited to the team that conceded; scorer is the defender
                event_rows.append((minute, team, player, "own_goal", None))

        # Cards come from lineups (cleaner than events; includes cards to bench)
        for t in lineups:
            tcode = team_code(t["team_name"])
            for p in t["lineup"]:
                for c in p.get("cards", []):
                    ctype = CARD_TYPES.get(c["card_type"])
                    if ctype:
                        # cards shown during/after a shootout still count for suspensions
                        minute = 120 if c.get("period") == 5 else minute_of(c["time"])
                        event_rows.append((minute, tcode, pid(p["player_id"]), ctype, None))

        conn.execute(
            """INSERT OR REPLACE INTO fixtures
               (fixture_id, edition_id, stage, grp, leg, match_no, date, home_team_id, away_team_id,
                home_score, away_score, extra_time, pen_home, pen_away, neutral_venue)
               VALUES (?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?)""",
            (fid, EDITION, stage, group, m["match_date"], home, away, m["home_score"], m["away_score"],
             extra_time, pen[home] if shootout else None, pen[away] if shootout else None,
             0 if "QAT" in (home, away) else 1),
        )

        # Players, squads, lineups
        conn.execute("DELETE FROM lineups WHERE fixture_id = ?", (fid,))
        for t in lineups:
            tcode = team_code(t["team_name"])
            for p in t["lineup"]:
                ppid = pid(p["player_id"])
                first_pos = p["positions"][0]["position"] if p["positions"] else None
                conn.execute(
                    """INSERT INTO players (player_id, full_name, known_as, birth_date, nationality, position, is_created)
                       VALUES (?,?,?,NULL,?,?,0)
                       ON CONFLICT(player_id) DO UPDATE SET
                         position = COALESCE(players.position, excluded.position)""",
                    (ppid, p["player_name"], p.get("player_nickname"),
                     (p.get("country") or {}).get("name"),
                     position_group(first_pos) if first_pos else None),
                )
                seen_players.add(ppid)
                conn.execute("INSERT OR IGNORE INTO squads VALUES (?,?,?,?)",
                             (EDITION, tcode, ppid, p.get("jersey_number")))
                if p["positions"]:
                    first, last = p["positions"][0], p["positions"][-1]
                    started = int(first["start_reason"] == "Starting XI")
                    conn.execute("INSERT INTO lineups VALUES (?,?,?,?,?,?)",
                                 (fid, tcode, ppid, started,
                                  0 if started else minute_of(first["from"]),
                                  minute_of(last["to"]) if last["to"] else None))
                    counts["lineups"] += 1

        conn.execute("DELETE FROM match_events WHERE fixture_id = ?", (fid,))
        for minute, team, player, etype, assist in event_rows:
            conn.execute(
                """INSERT INTO match_events (fixture_id, minute, added_time, team_id, player_id, event_type, assist_player_id)
                   VALUES (?,?,NULL,?,?,?,?)""",
                (fid, minute, team, player, etype, assist),
            )
        counts["events"] += len(event_rows)
        counts["fixtures"] += 1

    counts["players"] = len(seen_players)
    conn.commit()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--db", type=Path, default=db.DEFAULT_DB)
    args = ap.parse_args()
    conn = db.init_db(args.db)
    pack = Path(__file__).resolve().parent.parent / "data_packs" / EDITION / "ruleset.json"
    counts = import_season(args.data_dir, conn)
    db.load_ruleset(conn, pack)
    from footsim.engine import assign_match_numbers
    counts["bracket_slots"] = assign_match_numbers(conn, EDITION)
    cal_csv = pack.with_name("calendar.csv")
    if cal_csv.exists():
        import csv as _csv
        conn.execute("DELETE FROM calendar_events WHERE edition_id = ?", (EDITION,))
        with open(cal_csv, encoding="utf-8", newline="") as fh:
            for r in _csv.DictReader(fh):
                conn.execute("INSERT INTO calendar_events (edition_id, team_id, date, event_type, notes) "
                             "VALUES (?,?,?,?,?)", (r["edition_id"], r["team_id"] or None, r["date"],
                                                    r["event_type"], r["notes"]))
        conn.commit()
        counts["calendar"] = conn.execute("SELECT COUNT(*) FROM calendar_events WHERE edition_id = ?",
                                          (EDITION,)).fetchone()[0]
    awards_csv = pack.with_name("awards.csv")
    if awards_csv.exists():
        from footsim.stats import load_awards
        counts["awards"] = load_awards(conn, awards_csv)
    print(f"Imported {EDITION}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
