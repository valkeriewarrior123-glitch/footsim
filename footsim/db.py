"""Database setup and connection helpers."""
import json
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path(__file__).resolve().parent.parent / "footsim.db"
CANON_CUTOFF = "2001-01-01"  # editions starting before this are locked


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


PACKS = Path(__file__).resolve().parent.parent / "data_packs"


def init_db(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open the database, adding any new tables and applying data-pack fixes.

    Safe to run on an existing database: user branches are never touched.
    """
    conn = connect(path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    _add_column(conn, "created_players", "roles", "TEXT")          # added after v1
    _add_column(conn, "branches", "mode", "TEXT NOT NULL DEFAULT 'focus'")
    apply_squad_lists(conn)
    apply_role_minutes(conn)
    apply_club_packs(conn)
    conn.commit()
    return conn


def apply_club_packs(conn: sqlite3.Connection) -> None:
    """Add any club data pack (data_packs/<name>/fixtures.csv + ruleset.json) this database doesn't have yet."""
    import json
    packs = Path(__file__).resolve().parent.parent / "data_packs"
    for rules in sorted(packs.glob("*/ruleset.json")):
        if not (rules.parent / "fixtures.csv").exists():
            continue
        edition = json.loads(rules.read_text(encoding="utf-8"))["edition_id"]
        if conn.execute("SELECT 1 FROM editions WHERE edition_id = ?", (edition,)).fetchone():
            load_ruleset(conn, rules)        # rule changes (zones, deductions) reach saves that already have the data
            continue
        from footsim.import_club_pack import import_pack
        import_pack(conn, rules.parent)


def _add_column(conn, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


ROLE_ORDER = ["GK", "RB", "CB", "LB", "RWB", "LWB", "CDM", "CM", "RM", "LM", "CAM", "RW", "LW", "ST"]
ROLE_GROUP = {"GK": "GK", "RB": "DF", "CB": "DF", "LB": "DF", "RWB": "DF", "LWB": "DF", "CDM": "MF", "CM": "MF",
              "RM": "MF", "LM": "MF", "CAM": "MF", "RW": "FW", "LW": "FW", "ST": "FW"}


def apply_role_minutes(conn: sqlite3.Connection) -> int:
    """Load each real player's detailed roles from the data packs (once)."""
    import csv
    if conn.execute("SELECT 1 FROM player_positions LIMIT 1").fetchone():
        return 0
    n = 0
    for f in PACKS.glob("*/player_roles.csv"):
        with open(f, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if conn.execute("SELECT 1 FROM players WHERE player_id = ?", (r["player_id"],)).fetchone():
                    conn.execute("INSERT OR REPLACE INTO player_positions VALUES (?,?,?)",
                                 (r["player_id"], r["role"], int(r["minutes"])))
                    n += 1
    return n


def roles_of(conn: sqlite3.Connection, player_id: str) -> list:
    """Detailed roles, most-played first. Created players use the roles chosen for them."""
    row = conn.execute("SELECT roles FROM created_players WHERE player_id = ?", (player_id,)).fetchone()
    if row and row[0]:
        return [r for r in row[0].split(",") if r]
    return [r[0] for r in conn.execute("SELECT role FROM player_positions WHERE player_id = ? ORDER BY minutes DESC",
                                       (player_id,))]


def apply_squad_lists(conn: sqlite3.Connection) -> int:
    """Fill missing positions and birth dates from official squad lists (matched by team + shirt number).

    Positions already known from match data are kept.
    """
    import csv
    n = 0
    for f in PACKS.glob("*/squad_positions.csv"):
        edition = f.parent.name
        if not conn.execute("SELECT 1 FROM editions WHERE edition_id = ?", (edition,)).fetchone():
            continue
        with open(f, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                cur = conn.execute(
                    "UPDATE players SET position = COALESCE(position, ?), birth_date = COALESCE(birth_date, ?) "
                    "WHERE is_created = 0 AND player_id = (SELECT player_id FROM squads WHERE edition_id = ? "
                    "AND team_id = ? AND shirt_number = ?) AND (position IS NULL OR birth_date IS NULL)",
                    (r["position"], r["birth_date"], edition, r["team_id"], int(r["shirt_number"])))
                n += cur.rowcount
    return n


def is_locked(start_date: str) -> bool:
    """Canon rule: anything that began before 1 Jan 2001 is immutable."""
    return start_date < CANON_CUTOFF


def load_ruleset(conn: sqlite3.Connection, ruleset_path: Path) -> str:
    data = json.loads(Path(ruleset_path).read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR REPLACE INTO rulesets (edition_id, ruleset_json, source) VALUES (?, ?, ?)",
        (data["edition_id"], json.dumps(data), data.get("source")),
    )
    conn.commit()
    return data["edition_id"]


def get_ruleset(conn: sqlite3.Connection, edition_id: str) -> dict:
    row = conn.execute(
        "SELECT ruleset_json FROM rulesets WHERE edition_id = ?", (edition_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"No ruleset for {edition_id}")
    return json.loads(row["ruleset_json"])


if __name__ == "__main__":
    c = init_db()
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"Database ready at {DEFAULT_DB}")
    print("Tables:", ", ".join(t for t in tables if t != "sqlite_sequence"))
