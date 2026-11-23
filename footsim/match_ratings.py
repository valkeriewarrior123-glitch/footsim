"""Player match ratings (a 3-10 scale, like the ratings on football apps) and the voted awards built on them.

A rating starts at 6.3 and moves with what the player did: goals and assists
(worth more for defenders and midfielders), the result and margin, clean sheets,
goalkeeping, cards, own goals and missed penalties. Short substitute
appearances are pulled toward 6.0 unless they changed the game.

Voted awards in a branch are calculated from these ratings:
  Golden Ball   best tournament score: average rating + how far the team went,
                among players who played at least half their team's minutes
  Golden Glove  best goalkeeper: rating, clean sheets, shootout saves, how far the team went
  Best Young    best Golden Ball score among players born on/after the cut-off date
"""
from collections import defaultdict

GOAL_VALUE = {"FW": 1.0, "MF": 1.2, "DF": 1.4, "GK": 2.0}
FINISH_BONUS = {"Champion": 0.7, "Runner-up": 0.4, "Third": 0.25, "Fourth": 0.2, "Quarter-finals": 0.08,
                "Round of 16": 0.0, "Group stage": -0.1, "": 0.0}
YOUNG_CUTOFF = {"WC2022": "2001-01-01"}


def rate_match(f, positions: dict) -> dict:
    """player_id -> rating for one played fixture."""
    length = 120 if f.extra_time else 90
    out = {}
    winner = f.winner()
    for l in f.lineup:
        pid, team = l["player_id"], l["team_id"]
        on = 0 if l["started"] else (l["minute_on"] or 0)
        off = length if l["minute_off"] is None else min(l["minute_off"], length)
        mins = max(0, off - on)
        if mins <= 0:
            continue
        pos = positions.get(pid) or "MF"
        scored = f.home_score if team == f.home else f.away_score
        conceded = f.away_score if team == f.home else f.home_score
        r = 6.3
        r += 0.35 if scored > conceded else (-0.25 if scored < conceded else (0.2 if winner == team else 0.05))
        r += max(-0.5, min(0.5, 0.1 * (scored - conceded)))
        for e in f.events:
            et = e["event_type"]
            if e.get("player_id") == pid:
                if et == "goal":
                    r += GOAL_VALUE[pos]
                elif et == "pen_goal":
                    r += 0.8 * GOAL_VALUE[pos]
                elif et == "own_goal":
                    r -= 0.8
                elif et == "pen_miss":
                    r -= 0.6
                elif et == "yellow":
                    r -= 0.25
                elif et in ("second_yellow", "red"):
                    r -= 1.5
                elif et == "shootout_score":
                    r += 0.1
                elif et == "shootout_miss":
                    r -= 0.3
            if e.get("assist_player_id") == pid and et in ("goal", "pen_goal"):
                r += 0.9
        if pos == "GK":
            r -= 0.25 * conceded
            if conceded == 0 and mins >= 60:
                r += 0.9
            if winner == team and f.pen_home is not None:
                opp_misses = sum(1 for e in f.events if e["event_type"] == "shootout_miss" and e["team_id"] != team)
                r += 0.35 * opp_misses
        elif conceded == 0 and mins >= 60:
            r += {"DF": 0.6, "MF": 0.2}.get(pos, 0.0)
        # a short cameo only counts in proportion, unless it changed the game
        if mins < 45:
            r = 6.0 + (r - 6.0) * min(1.0, (mins + 25) / 70) if r < 7.5 else r
        out[pid] = round(max(3.0, min(10.0, r)), 2)
    return out


def tournament_scores(fixtures, players: dict, teams: dict, positions: dict) -> dict:
    """player_id -> {avg, matches, minutes, score} over the counted fixtures."""
    ratings = defaultdict(list)
    for f in fixtures:
        for pid, r in rate_match(f, positions).items():
            ratings[pid].append(r)
    team_minutes = defaultdict(int)
    for f in fixtures:
        length = 120 if f.extra_time else 90
        team_minutes[f.home] += length
        team_minutes[f.away] += length
    out = {}
    for pid, rs in ratings.items():
        line = players.get(pid)
        if line is None:
            continue
        finish = teams.get(line.team_id).finish if line.team_id in teams else ""
        avg = sum(rs) / len(rs)
        share = line.minutes / max(1, team_minutes[line.team_id])
        out[pid] = {"avg": round(avg, 2), "matches": len(rs), "minutes": line.minutes, "share": share,
                    "score": avg + FINISH_BONUS.get(finish, 0.0) + 0.04 * len(rs),
                    "ratings": rs}
    return out


def voted_awards(edition_id: str, scores: dict, players: dict, teams: dict, positions: dict,
                 birth: dict, fixtures) -> dict:
    eligible = {pid: s for pid, s in scores.items() if s["share"] >= 0.5}
    ball = sorted(eligible, key=lambda p: -eligible[p]["score"])[:3]

    gk = {}
    for pid, s in scores.items():
        if positions.get(pid) != "GK" or s["share"] < 0.5:
            continue
        line = players[pid]
        finish = teams.get(line.team_id).finish if line.team_id in teams else ""
        saves = 0
        for f in fixtures:
            if f.pen_home is not None and f.winner() == line.team_id and \
                    any(l["player_id"] == pid for l in f.lineup):
                saves += sum(1 for e in f.events if e["event_type"] == "shootout_miss" and e["team_id"] != line.team_id)
        gk[pid] = s["avg"] + 0.15 * line.clean_sheets + 0.1 * saves + FINISH_BONUS.get(finish, 0.0)
    glove = sorted(gk, key=lambda p: -gk[p])[:1]

    cutoff = YOUNG_CUTOFF.get(edition_id, "2001-01-01")
    young = sorted((p for p in eligible if (birth.get(p) or "0000") >= cutoff), key=lambda p: -eligible[p]["score"])[:1]
    return {"golden_ball": ball, "golden_glove": glove, "best_young": young}
