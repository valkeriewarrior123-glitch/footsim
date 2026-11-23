"""Stats and awards, always derived from match events.

Only results that count in the branch are used: every group match, and knockout
matches whose slot is `valid`. Pending or unplayed fixtures contribute nothing.

    python -m footsim.stats WC2022 [branch_id]
"""
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from footsim import db, engine
from footsim.timeline import Fixture, active_fixtures

GOALS = ("goal", "pen_goal")


@dataclass
class PlayerLine:
    player_id: str
    team_id: str
    apps: int = 0
    starts: int = 0
    sub_apps: int = 0
    minutes: int = 0
    goals: int = 0
    pen_goals: int = 0
    pen_misses: int = 0
    assists: int = 0
    own_goals: int = 0
    yellows: int = 0
    reds: int = 0               # second yellows + straight reds
    clean_sheets: int = 0       # goalkeepers who played the whole match
    shootout_scored: int = 0
    shootout_missed: int = 0


@dataclass
class TeamLine:
    team_id: str
    played: int = 0
    won: int = 0
    drawn: int = 0              # results after 120 min; shootouts count as draws
    lost: int = 0
    gf: int = 0
    ga: int = 0
    clean_sheets: int = 0
    shootouts_won: int = 0
    finish: str = ""


@dataclass
class EditionStats:
    edition_id: str
    players: dict = field(default_factory=dict)
    teams: dict = field(default_factory=dict)
    awards: dict = field(default_factory=dict)        # award -> [player_id ranked]
    records: dict = field(default_factory=dict)
    resolution: engine.Resolution | None = None
    ratings: dict = field(default_factory=dict)       # player_id -> {avg, matches, score, ratings}
    match_ratings: dict = field(default_factory=dict)  # fixture_id -> {player_id: rating}


def counted_fixtures(conn, edition_id: str, branch_id: int | None):
    res = engine.resolve(conn, edition_id, branch_id)
    valid_ko = engine.played_fixture_ids(res)
    fx = active_fixtures(conn, edition_id, branch_id)
    played = [f for f in fx.values() if f.played and (f.stage == "group" or f.fixture_id in valid_ko)]
    return sorted(played, key=lambda f: f.date), res


def _match_length(f: Fixture) -> int:
    return 120 if f.extra_time else 90


def _add_match(players: dict, teams: dict, f: Fixture, gk: set) -> None:
    length = _match_length(f)
    for side, opp, scored, conceded in ((f.home, f.away, f.home_score, f.away_score),
                                        (f.away, f.home, f.away_score, f.home_score)):
        t = teams.setdefault(side, TeamLine(side))
        t.played += 1
        t.gf += scored
        t.ga += conceded
        if scored > conceded:
            t.won += 1
        elif scored < conceded:
            t.lost += 1
        else:
            t.drawn += 1
            if f.winner() == side:
                t.shootouts_won += 1
        if conceded == 0:
            t.clean_sheets += 1

    conceded_by = {f.home: f.away_score, f.away: f.home_score}
    for l in f.lineup:
        p = players.setdefault(l["player_id"], PlayerLine(l["player_id"], l["team_id"]))
        on = 0 if l["started"] else (l["minute_on"] or 0)
        off = length if l["minute_off"] is None else min(l["minute_off"], length)
        p.apps += 1
        p.starts += int(bool(l["started"]))
        p.sub_apps += int(not l["started"])
        p.minutes += max(0, off - on)
        if l["player_id"] in gk and l["started"] and l["minute_off"] is None and conceded_by[l["team_id"]] == 0:
            p.clean_sheets += 1

    sent_off = set()
    for e in f.events:
        pid, et = e["player_id"], e["event_type"]
        if pid:
            p = players.setdefault(pid, PlayerLine(pid, e["team_id"]))
            if et in GOALS:
                p.goals += 1
                p.pen_goals += et == "pen_goal"
            elif et == "pen_miss":
                p.pen_misses += 1
            elif et == "own_goal":
                p.own_goals += 1
            elif et == "yellow":
                p.yellows += 1
            elif et in ("second_yellow", "red") and pid not in sent_off:
                p.reds += 1
                sent_off.add(pid)
            elif et == "shootout_score":
                p.shootout_scored += 1
            elif et == "shootout_miss":
                p.shootout_missed += 1
        if et in GOALS and e.get("assist_player_id"):
            a = players.setdefault(e["assist_player_id"], PlayerLine(e["assist_player_id"], e["team_id"]))
            a.assists += 1


def _finishes(res: engine.Resolution, teams: dict) -> None:
    order = {"F": None, "3P": None}
    for s in res.slots:
        if s.status == "valid" and s.stage in order:
            order[s.stage] = s.fixture
    if order["F"]:
        teams[order["F"].winner()].finish = "Champion"
        teams[order["F"].loser()].finish = "Runner-up"
    if order["3P"]:
        teams[order["3P"].winner()].finish = "Third"
        teams[order["3P"].loser()].finish = "Fourth"
    labels = {"QF": "Quarter-finals", "R16": "Round of 16", "R32": "Round of 32"}
    for s in res.slots:
        if s.status == "valid" and s.stage in labels:
            lost = s.fixture.loser()
            if lost in teams and not teams[lost].finish:
                teams[lost].finish = labels[s.stage]
    for g, rows in res.tables.items():
        done = all(r.played == len(rows) - 1 for r in rows)
        for r in rows[2:] if done else []:
            if r.team in teams and not teams[r.team].finish:
                teams[r.team].finish = "Group stage"


def golden_boot(players: dict, top: int = 3) -> list[str]:
    """FIFA order: goals, then assists, then fewer minutes played."""
    ranked = sorted((p for p in players.values() if p.goals > 0),
                    key=lambda p: (-p.goals, -p.assists, p.minutes))
    return [p.player_id for p in ranked[:top]]


def _voted_awards(conn, edition_id: str, branch_id: int | None) -> dict:
    out = defaultdict(list)
    for r in conn.execute("SELECT award, rank, player_id FROM awards WHERE edition_id = ? "
                          "AND award <> 'golden_boot' ORDER BY award, rank", (edition_id,)):
        out[r["award"]].append(r["player_id"])
    if branch_id is not None:
        over = defaultdict(dict)
        for r in conn.execute("SELECT award, rank, player_id FROM award_overrides WHERE branch_id = ? "
                              "AND edition_id = ? ORDER BY rank", (branch_id, edition_id)):
            over[r["award"]][r["rank"]] = r["player_id"]
        for award, ranks in over.items():
            current = dict(enumerate(out.get(award, []), 1))
            current.update(ranks)
            out[award] = [current[k] for k in sorted(current)]
    return dict(out)


def edition_stats(conn, edition_id: str, branch_id: int | None = None) -> EditionStats:
    played, res = counted_fixtures(conn, edition_id, branch_id)
    gk = {r[0] for r in conn.execute("SELECT player_id FROM players WHERE position = 'GK'")}
    players, teams = {}, {}
    for f in played:
        _add_match(players, teams, f, gk)
    _finishes(res, teams)

    st = EditionStats(edition_id, players, teams, resolution=res)
    from footsim import match_ratings as mr
    positions = dict(conn.execute("SELECT player_id, position FROM players"))
    st.ratings = mr.tournament_scores(played, players, teams, positions)
    st.match_ratings = {f.fixture_id: mr.rate_match(f, positions) for f in played}
    if branch_id is None:
        st.awards = _voted_awards(conn, edition_id, None)          # real history: the real winners
    else:
        birth = dict(conn.execute("SELECT player_id, birth_date FROM players"))
        if res.champion:
            st.awards = mr.voted_awards(edition_id, st.ratings, players, teams, positions, birth, played)
        else:                                           # voted awards wait for the final
            st.awards = {"golden_ball": [], "golden_glove": [], "best_young": []}
    st.awards["golden_boot"] = golden_boot(players)
    assisters = sorted((p for p in players.values() if p.assists), key=lambda p: (-p.assists, p.minutes))
    st.awards["most_assists"] = [p.player_id for p in assisters[:3]]

    if played:
        hi = max(played, key=lambda f: f.home_score + f.away_score)
        st.records["highest_scoring_match"] = (hi.fixture_id, f"{hi.home} {hi.home_score}-{hi.away_score} {hi.away}")
        st.records["total_goals"] = sum(f.home_score + f.away_score for f in played)
        st.records["goals_per_match"] = round(st.records["total_goals"] / len(played), 2)
        best_att = max(teams.values(), key=lambda t: t.gf)
        st.records["top_scoring_team"] = (best_att.team_id, best_att.gf)
    return st


def player_career(conn, player_id: str, branch_id: int | None = None) -> dict:
    """Totals for one player across every edition loaded, as seen in a branch."""
    totals = defaultdict(int)
    per_edition = {}
    for (eid,) in conn.execute("SELECT edition_id FROM editions ORDER BY start_date"):
        try:
            st = edition_stats(conn, eid, branch_id)
        except KeyError:
            continue                       # edition without a ruleset
        line = st.players.get(player_id)
        if line:
            per_edition[eid] = line
            for k, v in vars(line).items():
                if isinstance(v, int):
                    totals[k] += v
    return {"totals": dict(totals), "editions": per_edition}


def load_awards(conn, csv_path: Path) -> int:
    n = 0
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            conn.execute("INSERT OR REPLACE INTO awards VALUES (?,?,?,?)",
                         (r["edition_id"], r["award"], r["player_id"], int(r["rank"])))
            n += 1
    conn.commit()
    return n


def set_award(conn, branch_id: int, edition_id: str, award: str, player_id: str, rank: int = 1) -> None:
    if award == "golden_boot":
        raise ValueError("Golden Boot is calculated from goals; change match events instead")
    conn.execute("INSERT OR REPLACE INTO award_overrides VALUES (?,?,?,?,?)",
                 (branch_id, edition_id, award, rank, player_id))
    conn.commit()


def report(conn, st: EditionStats) -> str:
    name = dict(conn.execute("SELECT player_id, COALESCE(known_as, full_name) FROM players"))
    out = [f"== {st.edition_id} stats =="]
    out.append("Top scorers (goals / assists / minutes)")
    for p in sorted(st.players.values(), key=lambda p: (-p.goals, -p.assists, p.minutes))[:10]:
        out.append(f"  {name.get(p.player_id, p.player_id):28} {p.team_id}  {p.goals} / {p.assists} / {p.minutes}'")
    out.append("Awards")
    for award, ids in st.awards.items():
        out.append(f"  {award:14} " + ", ".join(name.get(i, i) for i in ids))
    out.append("Team finishes")
    for t in sorted(st.teams.values(), key=lambda t: ["Champion", "Runner-up", "Third", "Fourth",
                    "Quarter-finals", "Round of 16", "Group stage", ""].index(t.finish)):
        out.append(f"  {t.team_id:4} {t.finish:15} W{t.won} D{t.drawn} L{t.lost} {t.gf}-{t.ga} CS{t.clean_sheets}")
    out.append("Records")
    for k, v in st.records.items():
        out.append(f"  {k}: {v}")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    c = db.connect()
    b = int(sys.argv[2]) if len(sys.argv) > 2 else None
    print(report(c, edition_stats(c, sys.argv[1] if len(sys.argv) > 1 else "WC2022", b)))
