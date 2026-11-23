"""Timeline branches: baseline + ordered overrides.

Builds the "active" view of an edition for a branch (or the pure baseline when
branch_id is None) and lets callers create branches and record overrides.
"""
from dataclasses import dataclass, field


@dataclass
class Fixture:
    fixture_id: str
    stage: str
    grp: str | None
    match_no: int | None
    date: str
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    extra_time: int = 0
    pen_home: int | None = None
    pen_away: int | None = None
    status: str = "historical"       # historical | overridden | generated | pending
    origin: str = "baseline"
    override_id: int | None = None
    events: list = field(default_factory=list)   # dicts: minute, team_id, player_id, event_type, assist_player_id
    lineup: list = field(default_factory=list)   # dicts: team_id, player_id, started, minute_on, minute_off

    @property
    def played(self) -> bool:
        return self.home_score is not None and self.status != "pending"

    def winner(self) -> str | None:
        if not self.played:
            return None
        if self.home_score != self.away_score:
            return self.home if self.home_score > self.away_score else self.away
        if self.pen_home is not None and self.pen_home != self.pen_away:
            return self.home if self.pen_home > self.pen_away else self.away
        return None

    def loser(self) -> str | None:
        w = self.winner()
        return None if w is None else (self.away if w == self.home else self.home)

    def teams(self) -> frozenset:
        return frozenset((self.home, self.away))


def create_branch(conn, name: str, parent_branch_id: int | None = None,
                  created_player_id: str | None = None) -> int:
    fork_seq = None
    if parent_branch_id is not None:
        fork_seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM fixture_overrides WHERE branch_id = ?", (parent_branch_id,)
        ).fetchone()[0]
    if parent_branch_id is not None and created_player_id is None:
        created_player_id = conn.execute("SELECT created_player_id FROM branches WHERE branch_id = ?",
                                         (parent_branch_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO branches (name, parent_branch_id, fork_seq, created_player_id) VALUES (?,?,?,?)",
        (name, parent_branch_id, fork_seq, created_player_id),
    )
    new = cur.lastrowid
    if parent_branch_id is not None:
        # Forks start with the parent's squad changes, roles, injuries and calendar.
        conn.execute("INSERT INTO squad_overrides SELECT ?, edition_id, team_id, player_id, action, seq "
                     "FROM squad_overrides WHERE branch_id = ?", (new, parent_branch_id))
        conn.execute("INSERT INTO player_roles SELECT ?, fixture_id, role, target_player_id, minute "
                     "FROM player_roles WHERE branch_id = ?", (new, parent_branch_id))
        conn.execute("INSERT INTO injuries (branch_id, player_id, start_date, end_date, description) "
                     "SELECT ?, player_id, start_date, end_date, description FROM injuries WHERE branch_id = ?",
                     (new, parent_branch_id))
        conn.execute("INSERT INTO player_career_events (branch_id, player_id, date, event_type, team_id, notes) "
                     "SELECT ?, player_id, date, event_type, team_id, notes FROM player_career_events "
                     "WHERE branch_id = ?", (new, parent_branch_id))
        conn.execute("INSERT INTO award_overrides SELECT ?, edition_id, award, rank, player_id "
                     "FROM award_overrides WHERE branch_id = ?", (new, parent_branch_id))
    conn.commit()
    return new


def _next_seq(conn, branch_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM fixture_overrides WHERE branch_id = ?", (branch_id,)
    ).fetchone()[0]


def set_result(conn, branch_id: int, fixture: Fixture, home_score: int | None, away_score: int | None,
               *, events: list | None = None, lineup: list | None = None, extra_time: int = 0,
               pen_home: int | None = None, pen_away: int | None = None,
               status: str = "overridden", origin: str = "user_edit", reason: str | None = None,
               edition_id: str) -> int:
    """Record (or replace) the result of one fixture in a branch."""
    conn.execute("DELETE FROM fixture_overrides WHERE branch_id = ? AND fixture_id = ?",
                 (branch_id, fixture.fixture_id))
    cur = conn.execute(
        """INSERT INTO fixture_overrides
           (branch_id, seq, fixture_id, edition_id, stage, grp, leg, match_no, date,
            home_team_id, away_team_id, home_score, away_score, extra_time, pen_home, pen_away,
            status, origin, reason)
           VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (branch_id, _next_seq(conn, branch_id), fixture.fixture_id, edition_id, fixture.stage, fixture.grp,
         fixture.match_no, fixture.date, fixture.home, fixture.away, home_score, away_score,
         extra_time, pen_home, pen_away, status, origin, reason),
    )
    oid = cur.lastrowid
    for e in events or []:
        conn.execute(
            """INSERT INTO event_overrides (override_id, minute, added_time, team_id, player_id, event_type, assist_player_id)
               VALUES (?,?,?,?,?,?,?)""",
            (oid, e["minute"], e.get("added_time"), e["team_id"], e.get("player_id"), e["event_type"],
             e.get("assist_player_id")),
        )
    for l in lineup or []:
        conn.execute("INSERT INTO lineup_overrides VALUES (?,?,?,?,?,?)",
                     (oid, l["team_id"], l["player_id"], l["started"], l.get("minute_on"), l.get("minute_off")))
    conn.commit()
    return oid


def _override_rows(conn, branch_id: int, edition_id: str, upto_seq: int | None = None) -> list:
    """Overrides for a branch, inheriting its parent's up to the fork point. Later seq wins."""
    row = conn.execute("SELECT parent_branch_id, fork_seq FROM branches WHERE branch_id = ?",
                       (branch_id,)).fetchone()
    rows = []
    if row and row["parent_branch_id"] is not None:
        rows = _override_rows(conn, row["parent_branch_id"], edition_id, row["fork_seq"])
    q = "SELECT * FROM fixture_overrides WHERE branch_id = ? AND edition_id = ?"
    args = [branch_id, edition_id]
    if upto_seq is not None:
        q += " AND seq <= ?"
        args.append(upto_seq)
    return rows + list(conn.execute(q + " ORDER BY seq", args))


def active_fixtures(conn, edition_id: str, branch_id: int | None = None, raw: bool = False) -> dict[str, Fixture]:
    """fixture_id -> Fixture as it stands in this branch.

    Unless raw=True, the branch's created player is placed into real-result
    lineups according to his per-match role (see player.py).
    """
    out = {}
    for f in conn.execute("SELECT * FROM fixtures WHERE edition_id = ?", (edition_id,)):
        fx = Fixture(f["fixture_id"], f["stage"], f["grp"], f["match_no"], f["date"],
                     f["home_team_id"], f["away_team_id"], f["home_score"], f["away_score"],
                     f["extra_time"], f["pen_home"], f["pen_away"])
        fx.events = [dict(e) for e in conn.execute(
            "SELECT minute, team_id, player_id, event_type, assist_player_id FROM match_events WHERE fixture_id = ?",
            (fx.fixture_id,))]
        fx.lineup = [dict(l) for l in conn.execute(
            "SELECT team_id, player_id, started, minute_on, minute_off FROM lineups WHERE fixture_id = ?",
            (fx.fixture_id,))]
        out[fx.fixture_id] = fx

    if branch_id is None:
        return out

    for o in _override_rows(conn, branch_id, edition_id):
        fx = Fixture(o["fixture_id"], o["stage"], o["grp"], o["match_no"], o["date"],
                     o["home_team_id"], o["away_team_id"], o["home_score"], o["away_score"],
                     o["extra_time"], o["pen_home"], o["pen_away"],
                     status=o["status"], origin=o["origin"], override_id=o["override_id"])
        fx.events = [dict(e) for e in conn.execute(
            "SELECT minute, team_id, player_id, event_type, assist_player_id FROM event_overrides WHERE override_id = ?",
            (o["override_id"],))]
        fx.lineup = [dict(l) for l in conn.execute(
            "SELECT team_id, player_id, started, minute_on, minute_off FROM lineup_overrides WHERE override_id = ?",
            (o["override_id"],))]
        out[fx.fixture_id] = fx
    if not raw:
        from footsim import player
        player.transform_fixtures(conn, edition_id, branch_id, out)
    return out
