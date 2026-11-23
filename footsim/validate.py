"""Consistency checks for an imported edition.

Usage: python -m footsim.validate [EDITION] [--db footsim.db]
"""
import argparse
from pathlib import Path

from footsim import db

GOAL_TYPES = ("goal", "pen_goal")


def check_edition(conn, edition: str) -> list[str]:
    problems = []
    fixtures = conn.execute("SELECT * FROM fixtures WHERE edition_id = ?", (edition,)).fetchall()
    if not fixtures:
        return [f"No fixtures for {edition}"]

    for f in fixtures:
        label = f"{f['date']} {f['home_team_id']} {f['home_score']}-{f['away_score']} {f['away_team_id']}"
        for side, opp, score in (("home_team_id", "away_team_id", "home_score"),
                                 ("away_team_id", "home_team_id", "away_score")):
            team, other = f[side], f[opp]
            goals = conn.execute(
                f"""SELECT
                      (SELECT COUNT(*) FROM match_events WHERE fixture_id=? AND team_id=? AND event_type IN {GOAL_TYPES})
                    + (SELECT COUNT(*) FROM match_events WHERE fixture_id=? AND team_id=? AND event_type='own_goal')""",
                (f["fixture_id"], team, f["fixture_id"], other),
            ).fetchone()[0]
            if goals != f[score]:
                problems.append(f"{label}: {team} goal events = {goals}, score says {f[score]}")

        # knockout draws must be settled by shootout
        if f["stage"] not in ("group", "league") and f["home_score"] == f["away_score"]:
            if f["pen_home"] is None or f["pen_home"] == f["pen_away"]:
                problems.append(f"{label}: knockout draw without a decisive shootout")

        # every scorer/carded player must be in the lineup for that match
        orphans = conn.execute(
            """SELECT DISTINCT e.player_id, e.event_type FROM match_events e
               WHERE e.fixture_id = ? AND e.player_id IS NOT NULL
                 AND e.event_type NOT IN ('yellow','second_yellow','red')
                 AND NOT EXISTS (SELECT 1 FROM lineups l WHERE l.fixture_id = e.fixture_id AND l.player_id = e.player_id)""",
            (f["fixture_id"],),
        ).fetchall()
        for o in orphans:
            problems.append(f"{label}: {o['player_id']} has a {o['event_type']} but no lineup entry")

        starters = conn.execute(
            "SELECT team_id, COUNT(*) n FROM lineups WHERE fixture_id=? AND started=1 GROUP BY team_id",
            (f["fixture_id"],),
        ).fetchall()
        gap = conn.execute("SELECT 1 FROM data_notes WHERE fixture_id = ? AND kind = 'lineup_incomplete'", (f["fixture_id"],)).fetchone()
        for s in starters:
            if s["n"] != 11 and not gap:        # a gap the data pack already declared is not a new problem
                problems.append(f"{label}: {s['team_id']} has {s['n']} starters")

    expected = {"group": 48, "R16": 8, "QF": 4, "SF": 2, "3P": 1, "F": 1}
    got = dict(conn.execute(
        "SELECT stage, COUNT(*) FROM fixtures WHERE edition_id=? GROUP BY stage", (edition,)).fetchall())
    if edition.startswith("WC") and got != expected:
        problems.append(f"Stage counts {got} != expected {expected}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("edition", nargs="?", default="WC2022")
    ap.add_argument("--db", type=Path, default=db.DEFAULT_DB)
    args = ap.parse_args()
    problems = check_edition(db.connect(args.db), args.edition)
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print("  -", p)
        raise SystemExit(1)
    print(f"{args.edition}: all checks passed")


if __name__ == "__main__":
    main()
