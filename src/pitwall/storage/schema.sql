-- pitwall metadata schema. Per-lap telemetry traces live in Parquet on disk
-- (see storage/traces.py), never in this database -- the access pattern is
-- column slicing by distance range, which row-oriented blobs handle terribly.

CREATE TABLE IF NOT EXISTS schema_meta (
    version        INTEGER NOT NULL,
    applied_at_utc TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      INTEGER PRIMARY KEY,
    game            TEXT    NOT NULL,            -- 'acc' (multi-game ready)
    track_code      TEXT    NOT NULL,            -- ACC internal code, e.g. 'spa'
    track_name      TEXT    NOT NULL,            -- normalised display name
    car_code        TEXT    NOT NULL,
    car_name        TEXT    NOT NULL,
    session_type    TEXT,                        -- practice | qualifying | race
    started_at_utc  TEXT    NOT NULL,            -- ISO-8601 UTC
    ended_at_utc    TEXT,
    sector_count    INTEGER NOT NULL DEFAULT 3,
    track_length_m  REAL,
    air_temp_c      REAL,
    road_temp_c     REAL,
    pitwall_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_track_started ON sessions(track_name, started_at_utc);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at_utc);

CREATE TABLE IF NOT EXISTS laps (
    lap_id         INTEGER PRIMARY KEY,
    session_id     INTEGER NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    lap_number     INTEGER NOT NULL,
    stint_number   INTEGER NOT NULL DEFAULT 1,
    lap_time_ms    INTEGER,                      -- NULL for an unfinished/partial lap
    is_valid       INTEGER NOT NULL DEFAULT 1,   -- 0 = invalidated (off-track etc.)
    is_outlap      INTEGER NOT NULL DEFAULT 0,
    is_inlap       INTEGER NOT NULL DEFAULT 0,
    top_speed_kmh  REAL,
    avg_speed_kmh  REAL,
    fuel_used_kg   REAL,
    tyre_set       INTEGER,
    started_at_utc TEXT    NOT NULL,
    trace_path     TEXT    NOT NULL,             -- relative path to the lap's Parquet file
    point_count    INTEGER NOT NULL,
    UNIQUE(session_id, lap_number)
);
CREATE INDEX IF NOT EXISTS idx_laps_session ON laps(session_id);
CREATE INDEX IF NOT EXISTS idx_laps_session_valid_time ON laps(session_id, is_valid, lap_time_ms);
CREATE INDEX IF NOT EXISTS idx_laps_session_stint ON laps(session_id, stint_number);

CREATE TABLE IF NOT EXISTS sectors (
    sector_id        INTEGER PRIMARY KEY,
    lap_id           INTEGER NOT NULL REFERENCES laps(lap_id) ON DELETE CASCADE,
    sector_index     INTEGER NOT NULL,          -- 0-based
    sector_time_ms   INTEGER,
    start_distance_m REAL,                       -- lap distance where this sector begins
    end_distance_m   REAL,                       -- ...and ends; enables per-sector trace slicing
    UNIQUE(lap_id, sector_index)
);
CREATE INDEX IF NOT EXISTS idx_sectors_lap ON sectors(lap_id);
