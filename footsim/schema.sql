-- Football Simulator schema (v1)
-- Four layers: BASELINE (real history, never edited), RULES, STRENGTH, TIMELINE (user changes).

PRAGMA foreign_keys = ON;

------------------------------------------------------------
-- BASELINE: real history
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS competitions (
    competition_id   TEXT PRIMARY KEY,          -- e.g. 'WC', 'UCL'
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('international','club')),
    confederation    TEXT                       -- FIFA, UEFA, CAF ...
);

CREATE TABLE IF NOT EXISTS editions (
    edition_id       TEXT PRIMARY KEY,          -- e.g. 'WC2022'
    competition_id   TEXT NOT NULL REFERENCES competitions(competition_id),
    season           TEXT NOT NULL,             -- '2022' or '2023-24'
    start_date       TEXT NOT NULL,             -- YYYY-MM-DD
    end_date         TEXT NOT NULL,
    locked           INTEGER NOT NULL DEFAULT 0, -- 1 = began before 2001-01-01 (canon)
    source           TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    team_id          TEXT PRIMARY KEY,          -- 'GER', 'ARG', 'c_realmadrid'
    name             TEXT NOT NULL,
    country          TEXT,
    confederation    TEXT,
    kind             TEXT NOT NULL CHECK (kind IN ('national','club'))
);

CREATE TABLE IF NOT EXISTS players (
    player_id        TEXT PRIMARY KEY,          -- 'p_messi'
    full_name        TEXT NOT NULL,
    known_as         TEXT,
    birth_date       TEXT,
    nationality      TEXT,
    position         TEXT CHECK (position IN ('GK','DF','MF','FW')),
    is_created       INTEGER NOT NULL DEFAULT 0 -- 1 = user-created player
);

CREATE TABLE IF NOT EXISTS squads (
    edition_id       TEXT NOT NULL REFERENCES editions(edition_id),
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    shirt_number     INTEGER,
    PRIMARY KEY (edition_id, team_id, player_id)
);

CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id       TEXT PRIMARY KEY,
    edition_id       TEXT NOT NULL REFERENCES editions(edition_id),
    stage            TEXT NOT NULL,             -- group, R32, R16, QF, SF, 3P, F
    grp              TEXT,                      -- 'A'..'H' for group stage
    leg              INTEGER,                   -- 1/2 for two-legged ties, NULL otherwise
    match_no         INTEGER,                   -- official match number (drives bracket slots)
    date             TEXT NOT NULL,
    home_team_id     TEXT NOT NULL REFERENCES teams(team_id),
    away_team_id     TEXT NOT NULL REFERENCES teams(team_id),
    home_score       INTEGER NOT NULL,          -- after extra time, excluding shootout
    away_score       INTEGER NOT NULL,
    extra_time       INTEGER NOT NULL DEFAULT 0,
    pen_home         INTEGER,                   -- shootout, NULL if none
    pen_away         INTEGER,
    neutral_venue    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lineups (
    fixture_id       TEXT NOT NULL REFERENCES fixtures(fixture_id),
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    started          INTEGER NOT NULL,
    minute_on        INTEGER,                   -- 0 for starters
    minute_off       INTEGER,                   -- NULL = played to the end
    PRIMARY KEY (fixture_id, player_id)
);

CREATE TABLE IF NOT EXISTS match_events (
    event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id       TEXT NOT NULL REFERENCES fixtures(fixture_id),
    minute           INTEGER NOT NULL,
    added_time       INTEGER,                   -- 90+3 -> minute 90, added_time 3
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    player_id        TEXT REFERENCES players(player_id),
    event_type       TEXT NOT NULL CHECK (event_type IN
                       ('goal','own_goal','pen_goal','pen_miss',
                        'yellow','second_yellow','red',
                        'shootout_score','shootout_miss')),
    assist_player_id TEXT REFERENCES players(player_id)
);

CREATE TABLE IF NOT EXISTS awards (
    edition_id       TEXT NOT NULL REFERENCES editions(edition_id),
    award            TEXT NOT NULL,             -- golden_boot, golden_ball, best_young, golden_glove
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    rank             INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (edition_id, award, rank)
);

CREATE TABLE IF NOT EXISTS calendar_events (
    calendar_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id       TEXT REFERENCES editions(edition_id),
    team_id          TEXT REFERENCES teams(team_id),
    date             TEXT NOT NULL,
    event_type       TEXT NOT NULL,             -- squad_announcement, camp_start, friendly, squad_deadline
    notes            TEXT
);

------------------------------------------------------------
-- RULES: one structured ruleset per edition (stored as JSON)
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rulesets (
    edition_id       TEXT PRIMARY KEY REFERENCES editions(edition_id),
    ruleset_json     TEXT NOT NULL,
    source           TEXT
);

------------------------------------------------------------
-- STRENGTH: inputs for the roller
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS team_ratings (
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    date             TEXT NOT NULL,
    elo              REAL NOT NULL,
    source           TEXT,
    PRIMARY KEY (team_id, date)
);

CREATE TABLE IF NOT EXISTS historical_finishes (
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    competition_id   TEXT NOT NULL REFERENCES competitions(competition_id),
    season           TEXT NOT NULL,
    final_position   INTEGER NOT NULL,
    PRIMARY KEY (team_id, competition_id, season)
);

------------------------------------------------------------
-- TIMELINE: user changes. Baseline + ordered overrides.
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS branches (
    branch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    parent_branch_id INTEGER REFERENCES branches(branch_id),  -- forks
    fork_seq         INTEGER,                   -- parent's overrides up to this seq are inherited
    created_player_id TEXT REFERENCES players(player_id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Squad changes in a branch (add created player, drop/replace someone)
CREATE TABLE IF NOT EXISTS squad_overrides (
    branch_id        INTEGER NOT NULL REFERENCES branches(branch_id),
    edition_id       TEXT NOT NULL REFERENCES editions(edition_id),
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    action           TEXT NOT NULL CHECK (action IN ('add','remove')),
    seq              INTEGER NOT NULL,
    PRIMARY KEY (branch_id, edition_id, team_id, player_id)
);

-- A replaced or new fixture result in a branch.
-- origin: user_edit | rolled | reused_historical | second_order_generated
CREATE TABLE IF NOT EXISTS fixture_overrides (
    override_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id        INTEGER NOT NULL REFERENCES branches(branch_id),
    seq              INTEGER NOT NULL,          -- order of application
    fixture_id       TEXT NOT NULL,             -- baseline id, or new id for fixtures that never happened
    edition_id       TEXT NOT NULL REFERENCES editions(edition_id),
    stage            TEXT NOT NULL,
    grp              TEXT,
    leg              INTEGER,
    match_no         INTEGER,
    date             TEXT NOT NULL,
    home_team_id     TEXT NOT NULL REFERENCES teams(team_id),
    away_team_id     TEXT NOT NULL REFERENCES teams(team_id),
    home_score       INTEGER,                   -- NULL while status = pending
    away_score       INTEGER,
    extra_time       INTEGER NOT NULL DEFAULT 0,
    pen_home         INTEGER,
    pen_away         INTEGER,
    status           TEXT NOT NULL CHECK (status IN ('overridden','generated','pending')),
    origin           TEXT NOT NULL,
    locked           INTEGER NOT NULL DEFAULT 0, -- 1 = bulk roller must not change it
    reason           TEXT,
    UNIQUE (branch_id, fixture_id)
);

CREATE TABLE IF NOT EXISTS event_overrides (
    event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    override_id      INTEGER NOT NULL REFERENCES fixture_overrides(override_id) ON DELETE CASCADE,
    minute           INTEGER NOT NULL,
    added_time       INTEGER,
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    player_id        TEXT REFERENCES players(player_id),
    event_type       TEXT NOT NULL,
    assist_player_id TEXT REFERENCES players(player_id)
);

CREATE TABLE IF NOT EXISTS lineup_overrides (
    override_id      INTEGER NOT NULL REFERENCES fixture_overrides(override_id) ON DELETE CASCADE,
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    started          INTEGER NOT NULL,
    minute_on        INTEGER,
    minute_off       INTEGER,
    PRIMARY KEY (override_id, player_id)
);

CREATE TABLE IF NOT EXISTS injuries (
    injury_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id        INTEGER NOT NULL REFERENCES branches(branch_id),
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    start_date       TEXT NOT NULL,
    end_date         TEXT,                      -- NULL = unknown
    description      TEXT
);

CREATE TABLE IF NOT EXISTS player_career_events (
    career_event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id        INTEGER NOT NULL REFERENCES branches(branch_id),
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    date             TEXT NOT NULL,
    event_type       TEXT NOT NULL,             -- call_up, camp, friendly, club_move ...
    team_id          TEXT REFERENCES teams(team_id),
    notes            TEXT
);

-- Log of every displacement under the replacement rule
CREATE TABLE IF NOT EXISTS displacement_log (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id        INTEGER NOT NULL REFERENCES branches(branch_id),
    edition_id       TEXT NOT NULL REFERENCES editions(edition_id),
    slot             TEXT NOT NULL,             -- e.g. 'R16 slot 1A'
    historical_team_id TEXT REFERENCES teams(team_id),
    new_team_id      TEXT REFERENCES teams(team_id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Voted/panel awards (Golden Ball, Golden Glove, Best Young) can't be derived
-- from match events; in a branch the user sets them here. Computed awards
-- (Golden Boot) are always recalculated and never stored.
CREATE TABLE IF NOT EXISTS award_overrides (
    branch_id        INTEGER NOT NULL REFERENCES branches(branch_id),
    edition_id       TEXT NOT NULL REFERENCES editions(edition_id),
    award            TEXT NOT NULL,
    rank             INTEGER NOT NULL DEFAULT 1,
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    PRIMARY KEY (branch_id, edition_id, award, rank)
);

------------------------------------------------------------
-- CREATED PLAYER (one per branch; forks copy it)
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS created_players (
    player_id        TEXT PRIMARY KEY REFERENCES players(player_id),
    branch_id        INTEGER NOT NULL REFERENCES branches(branch_id),
    team_id          TEXT NOT NULL REFERENCES teams(team_id),      -- national team
    edition_id       TEXT NOT NULL REFERENCES editions(edition_id),
    replaces_player_id TEXT NOT NULL REFERENCES players(player_id),
    tier             TEXT NOT NULL,
    overall          INTEGER NOT NULL CHECK (overall BETWEEN 1 AND 99),
    detail_position  TEXT,                                          -- e.g. 'Left winger'
    shirt_number     INTEGER
);

-- What the player does in each match. role:
--   auto     not chosen yet: takes the replaced player's spot if he played, else unused
--   inherit  takes the replaced player's spot
--   start    starts in place of target_player_id
--   sub      comes on at `minute` for target_player_id
--   unused   doesn't play
CREATE TABLE IF NOT EXISTS player_roles (
    branch_id        INTEGER NOT NULL REFERENCES branches(branch_id),
    fixture_id       TEXT NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('auto','inherit','start','sub','unused')),
    target_player_id TEXT REFERENCES players(player_id),
    minute           INTEGER,
    PRIMARY KEY (branch_id, fixture_id)
);

-- Detailed roles each real player actually played (ST, LW, CAM, CB, ...) and roughly for how long
CREATE TABLE IF NOT EXISTS player_positions (
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    role             TEXT NOT NULL,
    minutes          INTEGER NOT NULL,
    PRIMARY KEY (player_id, role)
);

-- Suspension swaps the engine made, so each change can report only the new ones
CREATE TABLE IF NOT EXISTS suspension_log (
    branch_id        INTEGER NOT NULL REFERENCES branches(branch_id),
    edition_id       TEXT NOT NULL,
    fixture_id       TEXT NOT NULL,
    player_id        TEXT NOT NULL,
    PRIMARY KEY (branch_id, edition_id, fixture_id, player_id)
);

CREATE INDEX IF NOT EXISTS ix_fixtures_edition   ON fixtures(edition_id);
CREATE INDEX IF NOT EXISTS ix_events_fixture     ON match_events(fixture_id);
CREATE INDEX IF NOT EXISTS ix_overrides_branch   ON fixture_overrides(branch_id, seq);

------------------------------------------------------------
-- DATA NOTES: things a data pack had to estimate or couldn't supply (shown to the user, never hidden)
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS data_notes (
    edition_id       TEXT NOT NULL REFERENCES editions(edition_id),
    fixture_id       TEXT,                      -- NULL = the whole edition
    kind             TEXT NOT NULL,             -- subs_estimated, lineup_incomplete, scorer_unknown, added_to_squad ...
    note             TEXT NOT NULL
);
