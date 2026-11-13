"""SQLite storage for daily Garmin metrics."""
from __future__ import annotations

import sqlite3
import json
from typing import Any

from afterset.connection import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_exercise_sets_raw (
    activity_id INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sleep_stages (
    date        TEXT NOT NULL,
    start_gmt   TEXT NOT NULL,
    end_gmt     TEXT NOT NULL,
    stage       TEXT NOT NULL,  -- deep, light, rem, awake
    PRIMARY KEY (date, start_gmt)
);

CREATE TABLE IF NOT EXISTS heart_rate_readings (
    timestamp_gmt   TEXT PRIMARY KEY,
    date            TEXT NOT NULL,
    bpm             INTEGER
);

CREATE TABLE IF NOT EXISTS stress_readings (
    timestamp_gmt   TEXT PRIMARY KEY,
    date            TEXT NOT NULL,
    stress_level    INTEGER
);

CREATE TABLE IF NOT EXISTS hrv_readings (
    timestamp_gmt   TEXT PRIMARY KEY,
    date            TEXT NOT NULL,
    hrv_ms          REAL
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id                 INTEGER PRIMARY KEY,
    date                        TEXT NOT NULL,
    name                        TEXT,
    type                        TEXT,
    start_gmt                   TEXT,
    end_gmt                     TEXT,
    duration_seconds            REAL,
    calories                    REAL,
    avg_hr                      INTEGER,
    max_hr                      INTEGER,
    aerobic_training_effect     REAL,
    anaerobic_training_effect   REAL,
    training_load               REAL,
    training_effect_label       TEXT,
    body_battery_impact         INTEGER,
    hr_zone_1_seconds           REAL,
    hr_zone_2_seconds           REAL,
    hr_zone_3_seconds           REAL,
    hr_zone_4_seconds           REAL,
    hr_zone_5_seconds           REAL,
    moderate_intensity_minutes  INTEGER,
    vigorous_intensity_minutes  INTEGER
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date                        TEXT PRIMARY KEY,
    -- Sleep
    sleep_score                 INTEGER,
    sleep_duration_seconds      INTEGER,
    sleep_deep_seconds          INTEGER,
    sleep_light_seconds         INTEGER,
    sleep_rem_seconds           INTEGER,
    sleep_awake_seconds         INTEGER,
    sleep_hrv                   REAL,
    sleep_spo2                  REAL,
    -- Stress
    stress_avg                  INTEGER,
    stress_max                  INTEGER,
    stress_rest                 REAL,   -- avg stress during sleep (from dailySleepDTO.avgSleepStress)
    -- Body Battery
    body_battery_start          INTEGER,
    body_battery_end            INTEGER,
    -- Heart Rate
    resting_hr                  INTEGER,
    -- Activity
    steps                       INTEGER,
    active_calories             INTEGER,
    -- Workouts (aggregated; comma-separated types if multiple)
    workout_types               TEXT,
    workout_duration_seconds    INTEGER,
    workout_training_load       REAL
);
"""

# Columns populated during sync
METRIC_COLS: list[str] = [
    "sleep_score",
    "sleep_duration_seconds",
    "sleep_deep_seconds",
    "sleep_light_seconds",
    "sleep_rem_seconds",
    "sleep_awake_seconds",
    "sleep_hrv",
    "sleep_spo2",
    "stress_avg",
    "stress_max",
    "stress_rest",
    "body_battery_start",
    "body_battery_end",
    "resting_hr",
    "steps",
    "active_calories",
    "workout_types",
    "workout_duration_seconds",
    "workout_training_load",
]


def init_db() -> None:
    """Create all tables if they don't exist."""
    with connect() as conn:
        conn.executescript(SCHEMA)


def upsert_rows(rows: list[dict[str, Any]]) -> int:
    """Insert or update rows keyed by date. Returns count written."""
    if not rows:
        return 0

    cols = ["date"] + METRIC_COLS
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in METRIC_COLS)

    sql = f"""
        INSERT INTO daily_metrics ({col_names})
        VALUES ({placeholders})
        ON CONFLICT(date) DO UPDATE SET {update_clause}
    """

    with connect() as conn:
        data = [tuple(row.get(c) for c in cols) for row in rows]
        conn.executemany(sql, data)

    return len(rows)


def upsert_sleep_stages(date: str, stages: list[dict[str, str]]) -> None:
    """Insert or replace sleep stage intervals for a given date."""
    if not stages:
        return
    sql = """
        INSERT INTO sleep_stages (date, start_gmt, end_gmt, stage)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date, start_gmt) DO UPDATE SET
            end_gmt = excluded.end_gmt,
            stage   = excluded.stage
    """
    with connect() as conn:
        conn.executemany(sql, [(date, s["start_gmt"], s["end_gmt"], s["stage"]) for s in stages])


def upsert_timeseries(table: str, date: str, rows: list[dict[str, Any]]) -> None:
    """Generic upsert for heart_rate_readings, stress_readings, hrv_readings."""
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    update_cols = [c for c in cols if c != "timestamp_gmt"]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    sql = f"""
        INSERT INTO {table} ({col_names})
        VALUES ({placeholders})
        ON CONFLICT(timestamp_gmt) DO UPDATE SET {update_clause}
    """
    with connect() as conn:
        conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


def upsert_activities(activities: list[dict[str, Any]]) -> None:
    """Insert or replace activity rows keyed by activity_id."""
    if not activities:
        return
    cols = [
        "activity_id", "date", "name", "type", "start_gmt", "end_gmt",
        "duration_seconds", "calories", "avg_hr", "max_hr",
        "aerobic_training_effect", "anaerobic_training_effect",
        "training_load", "training_effect_label", "body_battery_impact",
        "hr_zone_1_seconds", "hr_zone_2_seconds", "hr_zone_3_seconds",
        "hr_zone_4_seconds", "hr_zone_5_seconds",
        "moderate_intensity_minutes", "vigorous_intensity_minutes",
    ]
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    update_cols = [c for c in cols if c != "activity_id"]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    sql = f"""
        INSERT INTO activities ({col_names})
        VALUES ({placeholders})
        ON CONFLICT(activity_id) DO UPDATE SET {update_clause}
    """
    with connect() as conn:
        conn.executemany(sql, [tuple(a.get(c) for c in cols) for a in activities])
        for activity in activities:
            _refresh_workout(conn, activity['activity_id'])


def load_all() -> list[dict[str, Any]]:
    """Return every row from daily_metrics, sorted by date."""
    with connect() as conn:
        cursor = conn.execute(
            f"SELECT {', '.join(['date'] + METRIC_COLS)} FROM daily_metrics ORDER BY date"
        )
        return [dict(row) for row in cursor.fetchall()]


def upsert_exercise_sets(activity_id: int, payload: dict) -> None:
    """Preserve the original Garmin response for later review and correction."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO activity_exercise_sets_raw (activity_id, payload_json)
            VALUES (?, ?) ON CONFLICT(activity_id) DO UPDATE SET
            payload_json = excluded.payload_json, fetched_at = CURRENT_TIMESTAMP""",
            (activity_id, json.dumps(payload)),
        )
        _refresh_workout(conn, activity_id)


def _refresh_workout(conn: sqlite3.Connection, activity_id: int) -> None:
    """Keep the raw projection in the same transaction as Garmin sync writes."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='workout_versions'").fetchone() is None:
        return  # A standalone wellness CLI database may not have the MCP schema yet.
    from afterset import workouts
    workouts.save_snapshot(conn, activity_id, 0,
                           workouts.raw_workout(activity_id, workouts.recording(conn, activity_id)),
                           'garmin', replace_raw=True)
