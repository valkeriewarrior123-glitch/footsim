"""Team strength: World Football Elo ratings computed from every international result.

Source data: github.com/martj42/international_results (results.csv).
Formula: eloratings.net method.
  Rn = Ro + K * G * (W - We)
  We = 1 / (10^(-dr/400) + 1), dr = rating difference (+100 for the home side)
  K  = 60 World Cup finals, 50 continental finals, 40 qualifiers / Nations League,
       30 other tournaments, 20 friendlies
  G  = 1 (win by 0-1), 1.5 (by 2), (11 + N) / 8 (by N >= 3)

Also fits the goal model the roller uses: each side's goals ~ Poisson(lambda),
lambda_home = exp(a + b*dr/400), lambda_away = exp(a - b*dr/400).

    python -m footsim.ratings path/to/results.csv     # load ratings + fit model
"""
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from footsim import db

MODEL_PATH = Path(__file__).resolve().parent.parent / "data_packs" / "goal_model.json"
HOME_ADV = 100
START = 1500.0

CONTINENTAL_FINALS = {
    "UEFA Euro", "Copa América", "African Cup of Nations", "AFC Asian Cup", "Gold Cup",
    "CONCACAF Championship", "Oceania Nations Cup", "Confederations Cup", "CONMEBOL–UEFA Cup of Champions",
}

# results.csv name -> our team id, where it differs from the teams table name
NAME_FIX = {}


def k_factor(tournament: str) -> int:
    if tournament == "FIFA World Cup":
        return 60
    if tournament in CONTINENTAL_FINALS:
        return 50
    if "qualification" in tournament or "Nations League" in tournament:
        return 40
    if tournament == "Friendly":
        return 20
    return 30


def g_factor(margin: int) -> float:
    margin = abs(margin)
    if margin <= 1:
        return 1.0
    if margin == 2:
        return 1.5
    return (11 + margin) / 8


def expected(dr: float) -> float:
    return 1 / (10 ** (-dr / 400) + 1)


def compute(results_csv: Path):
    """Yield (row, pre_home_elo, pre_away_elo) for every match, in date order, updating as it goes."""
    rows = list(csv.DictReader(open(results_csv, encoding="utf-8")))
    rows.sort(key=lambda r: r["date"])
    elo = defaultdict(lambda: START)
    for r in rows:
        if r["home_score"] in ("", "NA") or r["away_score"] in ("", "NA"):
            continue
        h, a = r["home_team"], r["away_team"]
        hs, as_ = int(r["home_score"]), int(r["away_score"])
        rh, ra = elo[h], elo[a]
        home = 0 if r["neutral"].upper() == "TRUE" else HOME_ADV
        we = expected(rh + home - ra)
        w = 1.0 if hs > as_ else 0.0 if hs < as_ else 0.5
        delta = k_factor(r["tournament"]) * g_factor(hs - as_) * (w - we)
        yield r, rh, ra, home
        elo[h] = rh + delta
        elo[a] = ra - delta


def load(conn, results_csv: Path, since: str = "2000-01-01") -> int:
    """Store each known team's rating after every match it played since `since`."""
    teams = {name: tid for tid, name in conn.execute("SELECT team_id, name FROM teams WHERE kind = 'national'")}
    teams.update(NAME_FIX)
    conn.execute("DELETE FROM team_ratings WHERE source = 'computed_elo'")
    n = 0
    last = {}
    for r, rh, ra, home in compute(results_csv):
        hs, as_ = int(r["home_score"]), int(r["away_score"])
        we = expected(rh + home - ra)
        w = 1.0 if hs > as_ else 0.0 if hs < as_ else 0.5
        delta = k_factor(r["tournament"]) * g_factor(hs - as_) * (w - we)
        for name, new in ((r["home_team"], rh + delta), (r["away_team"], ra - delta)):
            if name in teams and r["date"] >= since:
                last[(teams[name], r["date"])] = new
    for (tid, date), elo in last.items():
        conn.execute("INSERT OR REPLACE INTO team_ratings VALUES (?,?,?, 'computed_elo')", (tid, date, round(elo, 1)))
        n += 1
    conn.commit()
    return n


def rating(conn, team_id: str, before_date: str, default: float = START) -> float:
    """A team's Elo going into a date (its rating after its last match before that date)."""
    row = conn.execute("SELECT elo FROM team_ratings WHERE team_id = ? AND date < ? ORDER BY date DESC LIMIT 1",
                       (team_id, before_date)).fetchone()
    return row[0] if row else default


# ------------------------------------------------------------------ goal model

def fit_goal_model(results_csv: Path, start: str = "2000-01-01", end: str = "2022-11-19") -> dict:
    """Poisson fit of goals on Elo difference, competitive matches only."""
    import numpy as np
    X, Yh, Ya = [], [], []
    for r, rh, ra, home in compute(results_csv):
        if not (start <= r["date"] < end) or r["tournament"] == "Friendly":
            continue
        X.append((rh + home - ra) / 400)
        Yh.append(int(r["home_score"]))
        Ya.append(int(r["away_score"]))
    x, yh, ya = np.array(X), np.array(Yh), np.array(Ya)
    # stack both sides: home uses +x, away uses -x
    xs, ys = np.concatenate([x, -x]), np.concatenate([yh, ya])
    a, b = 0.2, 0.5
    for _ in range(50):                       # Newton's method on the Poisson log-likelihood
        lam = np.exp(a + b * xs)
        g = np.array([np.sum(ys - lam), np.sum((ys - lam) * xs)])
        H = -np.array([[np.sum(lam), np.sum(lam * xs)], [np.sum(lam * xs), np.sum(lam * xs * xs)]])
        step = np.linalg.solve(H, g)
        a, b = a - step[0], b - step[1]
        if abs(step).max() < 1e-9:
            break
    return {"a": float(a), "b": float(b), "matches": int(len(x)), "range": [start, end],
            "mean_goals_per_team": float(ys.mean())}


def load_model() -> dict:
    if MODEL_PATH.exists():
        return json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    return {"a": 0.2, "b": 0.75}             # fallback if the model hasn't been fitted


def lambdas(elo_home: float, elo_away: float, home_adv: float = 0.0, model: dict | None = None):
    m = model or load_model()
    dr = (elo_home + home_adv - elo_away) / 400
    return math.exp(m["a"] + m["b"] * dr), math.exp(m["a"] - m["b"] * dr)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("results_csv", type=Path)
    ap.add_argument("--db", type=Path, default=db.DEFAULT_DB)
    args = ap.parse_args()
    conn = db.init_db(args.db)
    n = load(conn, args.results_csv)
    model = fit_goal_model(args.results_csv)
    MODEL_PATH.write_text(json.dumps(model, indent=2), encoding="utf-8")
    print(f"Stored {n} ratings. Goal model: a={model['a']:.3f} b={model['b']:.3f} "
          f"from {model['matches']} matches ({model['mean_goals_per_team']:.2f} goals per team)")


if __name__ == "__main__":
    main()
