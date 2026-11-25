from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meetings (
    meeting_id INTEGER PRIMARY KEY,
    meeting_date TEXT NOT NULL,
    iso_date TEXT NOT NULL,
    track_name TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS races (
    race_id INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL,
    race_date TEXT NOT NULL,
    iso_date TEXT NOT NULL,
    race_time TEXT NOT NULL,
    race_dt TEXT NOT NULL,
    track_name TEXT NOT NULL,
    race_number INTEGER NOT NULL,
    race_title TEXT,
    race_type TEXT,
    race_class TEXT,
    race_distance REAL,
    race_going REAL,
    race_handicap INTEGER DEFAULT 0,
    race_forecast TEXT,
    race_tricast TEXT,
    is_completed INTEGER DEFAULT 1,
    active_runners INTEGER DEFAULT 0,
    FOREIGN KEY (meeting_id) REFERENCES meetings(meeting_id)
);

CREATE INDEX IF NOT EXISTS idx_races_iso_date ON races(iso_date);
CREATE INDEX IF NOT EXISTS idx_races_dt ON races(race_dt);
CREATE INDEX IF NOT EXISTS idx_races_track_dist ON races(track_name, race_distance);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL,
    trap_number INTEGER NOT NULL,
    dog_id INTEGER,
    dog_name TEXT NOT NULL,
    dog_sire TEXT,
    dog_dam TEXT,
    dog_born TEXT,
    dog_sex TEXT,
    trainer_name TEXT,
    sp_str TEXT,
    decimal_odds REAL,
    exchange_lay_odds REAL,
    market_pos INTEGER,
    result_position INTEGER,
    is_winner INTEGER DEFAULT 0,
    btn_distance TEXT,
    sectional_time REAL,
    run_time REAL,
    adjusted_time REAL,
    dog_weight REAL,
    comment TEXT,
    UNIQUE(race_id, trap_number),
    FOREIGN KEY (race_id) REFERENCES races(race_id)
);

CREATE INDEX IF NOT EXISTS idx_entries_dog_id ON entries(dog_id);
CREATE INDEX IF NOT EXISTS idx_entries_race_id ON entries(race_id);
CREATE INDEX IF NOT EXISTS idx_entries_trainer ON entries(trainer_name);

CREATE TABLE IF NOT EXISTS dog_history_cache (
    dog_id INTEGER NOT NULL,
    race_id INTEGER NOT NULL,
    race_dt TEXT NOT NULL,
    iso_date TEXT NOT NULL,
    track_name TEXT NOT NULL,
    race_class TEXT,
    race_distance REAL,
    race_going REAL,
    trap_number INTEGER,
    result_position INTEGER,
    sectional_time REAL,
    run_time REAL,
    adjusted_time REAL,
    race_win_time REAL,
    dog_weight REAL,
    decimal_odds REAL,
    comment TEXT,
    PRIMARY KEY (dog_id, race_id)
);

CREATE INDEX IF NOT EXISTS idx_dog_hist_dog_dt ON dog_history_cache(dog_id, race_dt);

CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    model_name TEXT NOT NULL,
    train_races INTEGER NOT NULL,
    val_races INTEGER NOT NULL,
    train_entries INTEGER NOT NULL,
    val_entries INTEGER NOT NULL,
    log_loss REAL NOT NULL,
    brier_score REAL NOT NULL,
    top1_accuracy REAL NOT NULL,
    top3_accuracy REAL NOT NULL,
    lay_accuracy REAL NOT NULL,
    lay_roi_pct REAL NOT NULL,
    lay_bets_count INTEGER NOT NULL,
    feature_importances_json TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    race_id INTEGER NOT NULL,
    trap_number INTEGER NOT NULL,
    dog_id INTEGER,
    dog_name TEXT NOT NULL,
    model_run_id INTEGER,
    win_prob REAL NOT NULL,
    lose_prob REAL NOT NULL,
    fair_odds REAL NOT NULL,
    predicted_rank INTEGER NOT NULL,
    is_lay_pick INTEGER DEFAULT 0,
    lay_score REAL DEFAULT 0.0,
    lay_ev REAL,
    reasons_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (race_id, trap_number)
);

CREATE INDEX IF NOT EXISTS idx_predictions_race ON predictions(race_id);

CREATE TABLE IF NOT EXISTS paper_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL UNIQUE,
    iso_date TEXT NOT NULL,
    race_dt TEXT NOT NULL,
    track_name TEXT NOT NULL,
    race_number INTEGER NOT NULL,
    trap_number INTEGER NOT NULL,
    dog_name TEXT NOT NULL,
    win_prob REAL NOT NULL,
    lose_prob REAL NOT NULL,
    lay_odds REAL NOT NULL,
    odds_source TEXT DEFAULT 'SP_REAL',
    placed_before_race INTEGER DEFAULT 0,
    stake REAL NOT NULL,
    liability REAL NOT NULL,
    actual_position INTEGER,
    status TEXT NOT NULL, -- PENDING, WON_LAY, LOST_LAY, VOID
    pnl REAL NOT NULL,
    bankroll_after REAL NOT NULL,
    error_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL UNIQUE,
    iso_date TEXT NOT NULL,
    race_dt TEXT NOT NULL,
    track_name TEXT NOT NULL,
    race_number INTEGER NOT NULL,
    trap_number INTEGER NOT NULL,
    dog_name TEXT NOT NULL,
    win_prob REAL NOT NULL,
    lose_prob REAL NOT NULL,
    lay_odds REAL NOT NULL,
    stake REAL NOT NULL,
    liability REAL NOT NULL,
    actual_position INTEGER,
    status TEXT NOT NULL,
    pnl REAL NOT NULL,
    bankroll_after REAL NOT NULL,
    error_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    phase TEXT NOT NULL,
    message TEXT NOT NULL,
    meta_json TEXT
);
"""


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Safe lightweight migrations for existing SQLite files
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(paper_bets)").fetchall()
        }
        if "odds_source" not in cols:
            conn.execute("ALTER TABLE paper_bets ADD COLUMN odds_source TEXT DEFAULT 'SP_REAL'")
        if "placed_before_race" not in cols:
            conn.execute("ALTER TABLE paper_bets ADD COLUMN placed_before_race INTEGER DEFAULT 0")
        entry_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(entries)").fetchall()
        }
        if "exchange_lay_odds" not in entry_cols:
            conn.execute("ALTER TABLE entries ADD COLUMN exchange_lay_odds REAL")
        conn.commit()


def get_bankroll_config(db_path: Path, default_start: float, default_stake: float) -> tuple[float, float]:
    with sqlite3.connect(db_path) as conn:
        rows = dict(
            conn.execute(
                "SELECT key, value FROM app_settings WHERE key IN ('bankroll_start', 'lay_stake')"
            ).fetchall()
        )
    start = float(rows.get("bankroll_start", default_start))
    stake = float(rows.get("lay_stake", default_stake))
    return start, stake


def save_bankroll_config(db_path: Path, bankroll_start: float, lay_stake: float) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('bankroll_start', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(round(bankroll_start, 2)),),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('lay_stake', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(round(lay_stake, 2)),),
        )


@contextmanager
def get_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def log_study_event(
    db_path: Path,
    created_at: str,
    level: str,
    phase: str,
    message: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO study_logs (created_at, level, phase, message, meta_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (created_at, level, phase, message, meta_json),
        )
        row_id = cur.lastrowid
    return {
        "id": row_id,
        "created_at": created_at,
        "level": level,
        "phase": phase,
        "message": message,
        "meta": meta or {},
    }
