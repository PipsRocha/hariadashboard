from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from hri_curator.config import SCHEMA_VERSION, SubjectConfig


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS subjects (
  subject_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
  dataset_version TEXT NOT NULL, reviewer_assignment TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trials (
  trial_uid TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
  task_raw TEXT NOT NULL, task_normalized TEXT NOT NULL,
  collection_session_id TEXT NOT NULL, trial_directory_id TEXT NOT NULL,
  relative_trial_path TEXT NOT NULL UNIQUE, relative_mcap_path TEXT,
  mcap_filename TEXT, bag_size_bytes INTEGER, duration_sec REAL,
  starting_time_ns INTEGER, message_count INTEGER, ros_distro TEXT,
  storage_identifier TEXT, condition_acquired TEXT, task_outcome_acquired TEXT,
  primary_anomaly_acquired TEXT, secondary_consequence_acquired TEXT,
  controller_terminal_phase TEXT, controller_completion_reason TEXT,
  session_id_acquired TEXT, episode_id_acquired TEXT,
  experiment_config_sha256 TEXT, scan_status TEXT NOT NULL,
  scan_level TEXT NOT NULL DEFAULT 'fast',
  technical_qc_status TEXT NOT NULL, technical_qc_reasons TEXT NOT NULL DEFAULT '',
  usable_rgb INTEGER NOT NULL DEFAULT 0, usable_depth INTEGER NOT NULL DEFAULT 0,
  usable_dual_view INTEGER NOT NULL DEFAULT 0, usable_dual_rgbd INTEGER NOT NULL DEFAULT 0,
  usable_proprio INTEGER NOT NULL DEFAULT 0, usable_task_state INTEGER NOT NULL DEFAULT 0,
  last_scanned_at TEXT
);
CREATE TABLE IF NOT EXISTS trial_fingerprints (
  trial_uid TEXT PRIMARY KEY REFERENCES trials(trial_uid) ON DELETE CASCADE,
  fingerprint_json TEXT NOT NULL, fingerprint_sha256 TEXT NOT NULL,
  scanner_version TEXT NOT NULL, qc_profile_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
  trial_uid TEXT NOT NULL REFERENCES trials(trial_uid) ON DELETE CASCADE,
  topic_name TEXT NOT NULL, message_type TEXT NOT NULL, message_count INTEGER NOT NULL,
  PRIMARY KEY (trial_uid, topic_name)
);
CREATE TABLE IF NOT EXISTS topic_qc (
  trial_uid TEXT NOT NULL REFERENCES trials(trial_uid) ON DELETE CASCADE,
  topic_key TEXT NOT NULL, topic_name TEXT NOT NULL, message_type TEXT,
  message_count INTEGER NOT NULL, expected_hz REAL, mean_hz REAL,
  first_message_offset_sec REAL, last_message_offset_sec REAL,
  coverage_ratio REAL, median_dt_ms REAL, p95_dt_ms REAL, max_gap_ms REAL,
  required INTEGER NOT NULL DEFAULT 1, stream_status TEXT NOT NULL,
  qc_status TEXT NOT NULL, qc_reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (trial_uid, topic_key)
);
CREATE TABLE IF NOT EXISTS phase_intervals (
  trial_uid TEXT NOT NULL REFERENCES trials(trial_uid) ON DELETE CASCADE,
  phase TEXT NOT NULL, start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL,
  PRIMARY KEY (trial_uid, phase, start_ns)
);
CREATE TABLE IF NOT EXISTS reviews (
  trial_uid TEXT PRIMARY KEY REFERENCES trials(trial_uid) ON DELETE CASCADE,
  review_status TEXT NOT NULL DEFAULT 'unreviewed', condition_reviewed TEXT,
  task_outcome_reviewed TEXT, semantic_validity TEXT,
  primary_anomaly_reviewed TEXT, secondary_consequence_reviewed TEXT,
  anomaly_present INTEGER, usable_for_normal_training INTEGER,
  usable_for_anomaly_evaluation INTEGER, manual_exclusion_reason TEXT,
  reviewer_id TEXT, reviewed_at TEXT, updated_at TEXT, notes TEXT, review_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS annotations (
  annotation_id TEXT PRIMARY KEY, trial_uid TEXT NOT NULL REFERENCES trials(trial_uid) ON DELETE CASCADE,
  annotation_type TEXT NOT NULL, family TEXT, subtype TEXT,
  onset_ns INTEGER NOT NULL, offset_ns INTEGER NOT NULL,
  phase_at_onset TEXT, internal_phase_at_onset TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
  severity TEXT, confidence REAL, reviewer_id TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS qc_reason_codes (code TEXT PRIMARY KEY, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scan_runs (
  scan_run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
  completed_at TEXT, discovered INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
  scan_errors INTEGER NOT NULL DEFAULT 0, qc_failures INTEGER NOT NULL DEFAULT 0,
  qc_warnings INTEGER NOT NULL DEFAULT 0, options_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

REASON_CODES = {
    "metadata_parse_error": ("failure", "Bag metadata YAML could not be parsed"),
    "session_metadata_parse_error": ("warning", "Acquisition metadata YAML could not be parsed"),
    "metadata_missing": ("failure", "Bag metadata YAML is missing"),
    "session_metadata_missing": ("warning", "Acquisition metadata YAML is missing"),
    "missing_mcap": ("failure", "No MCAP file was found"),
    "multiple_mcap_files": ("failure", "More than one MCAP file was found"),
    "mcap_unreadable": ("failure", "MCAP could not be read"),
    "scan_exception": ("failure", "Unexpected scanner exception"),
    "required_topic_missing": ("failure", "Required topic is absent"),
    "required_topic_zero_messages": ("failure", "Required topic has zero messages"),
    "optional_topic_missing": ("warning", "Expected optional topic is absent"),
    "optional_topic_zero_messages": ("warning", "Expected optional topic has zero messages"),
    "required_stream_partial": ("warning", "Stream covers too little of the trial"),
    "optional_stream_partial": ("warning", "Optional stream covers too little of the trial"),
    "stream_starts_late": ("warning", "Stream starts late"),
    "stream_ends_early": ("warning", "Stream ends early"),
    "low_topic_frequency": ("warning", "Topic frequency is below threshold"),
    "large_timestamp_gap": ("warning", "Stream contains a large timestamp gap"),
    "camera_sync_warning": ("warning", "Camera p95 timestamp skew exceeds threshold"),
    "terminal_phase_not_reached": ("warning", "Configured terminal phase was not reached"),
    "invalid_phase_sequence": ("warning", "Observed phase transition is not allowed"),
    "phase_sequence_not_checked": ("warning", "No phase transition rules are configured"),
}


class Database:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = path
        self.readonly = readonly

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) if self.readonly else sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if not self.readonly: conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self, subject: SubjectConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            self._install_validation(conn)
            conn.execute(
                "INSERT INTO subjects VALUES (?, ?, ?, ?) ON CONFLICT(subject_id) DO UPDATE SET "
                "schema_version=excluded.schema_version, dataset_version=excluded.dataset_version, "
                "reviewer_assignment=excluded.reviewer_assignment",
                (subject.subject_id, SCHEMA_VERSION, subject.dataset_version, subject.reviewer_assignment),
            )
            conn.executemany(
                "INSERT INTO qc_reason_codes (code, description) VALUES (?, ?) "
                "ON CONFLICT(code) DO UPDATE SET description=excluded.description",
                [(code, f"{severity}: {description}") for code, (severity, description) in REASON_CODES.items()],
            )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        def columns(table: str) -> set[str]:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

        if "scan_level" not in columns("trials"):
            conn.execute("ALTER TABLE trials ADD COLUMN scan_level TEXT NOT NULL DEFAULT 'fast'")
            conn.execute(
                "UPDATE trials SET scan_level='deep' WHERE EXISTS ("
                "SELECT 1 FROM topic_qc q WHERE q.trial_uid=trials.trial_uid "
                "AND q.coverage_ratio IS NOT NULL)"
            )
        review_columns = columns("reviews")
        if "updated_at" not in review_columns:
            conn.execute("ALTER TABLE reviews ADD COLUMN updated_at TEXT")
            conn.execute("UPDATE reviews SET updated_at=reviewed_at WHERE reviewed_at IS NOT NULL")
        run_columns = columns("scan_runs")
        for name in ("scan_errors", "qc_failures", "qc_warnings"):
            if name not in run_columns:
                conn.execute(f"ALTER TABLE scan_runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,)
        )

    def _install_validation(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS validate_trials_insert BEFORE INSERT ON trials
        WHEN NEW.scan_status NOT IN ('complete','failed')
          OR NEW.scan_level NOT IN ('fast','deep')
          OR NEW.technical_qc_status NOT IN ('PASS','PASS_WITH_WARNINGS','FAIL')
        BEGIN SELECT RAISE(ABORT, 'invalid trial status'); END;
        CREATE TRIGGER IF NOT EXISTS validate_trials_update BEFORE UPDATE ON trials
        WHEN NEW.scan_status NOT IN ('complete','failed')
          OR NEW.scan_level NOT IN ('fast','deep')
          OR NEW.technical_qc_status NOT IN ('PASS','PASS_WITH_WARNINGS','FAIL')
        BEGIN SELECT RAISE(ABORT, 'invalid trial status'); END;
        CREATE TRIGGER IF NOT EXISTS validate_topic_qc_insert BEFORE INSERT ON topic_qc
        WHEN NEW.stream_status NOT IN ('stream_missing','stream_complete','stream_partial','stream_low_frequency','stream_large_gap')
          OR NEW.qc_status NOT IN ('PASS','PASS_WITH_WARNINGS','FAIL')
        BEGIN SELECT RAISE(ABORT, 'invalid topic QC status'); END;
        CREATE TRIGGER IF NOT EXISTS validate_reviews_insert BEFORE INSERT ON reviews
        WHEN NEW.review_status NOT IN ('unreviewed','in_progress','reviewed','needs_second_review')
        BEGIN SELECT RAISE(ABORT, 'invalid review status'); END;
        CREATE TRIGGER IF NOT EXISTS validate_reviews_update BEFORE UPDATE ON reviews
        WHEN NEW.review_status NOT IN ('unreviewed','in_progress','reviewed','needs_second_review')
        BEGIN SELECT RAISE(ABORT, 'invalid review status'); END;
        CREATE TRIGGER IF NOT EXISTS validate_annotations_insert BEFORE INSERT ON annotations
        WHEN NEW.offset_ns < NEW.onset_ns OR (NEW.confidence IS NOT NULL AND (NEW.confidence < 0 OR NEW.confidence > 1))
        BEGIN SELECT RAISE(ABORT, 'invalid annotation'); END;
        """)

    def rows(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def row(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.rows(query, params)
        return rows[0] if rows else None
