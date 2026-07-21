from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

import yaml

from hri_curator.config import layout, load_profile, load_subject, persist_current_config
from hri_curator.database import Database
from hri_curator.paths import validate_relative
from hri_curator.reviews import Review


TRIAL_COLUMNS = [
    "subject_id", "trial_uid", "task_raw", "task_normalized", "collection_session_id",
    "trial_directory_id", "relative_trial_path", "relative_mcap_path", "condition_acquired",
    "condition_reviewed", "condition_effective", "controller_terminal_phase",
    "controller_completion_reason", "task_outcome_reviewed", "task_outcome_effective",
    "primary_anomaly_acquired", "primary_anomaly_reviewed", "primary_anomaly_effective",
    "secondary_consequence_acquired", "secondary_consequence_reviewed", "secondary_consequence_effective",
    "anomaly_count", "first_anomaly_onset_sec",
    "last_anomaly_offset_sec", "duration_sec", "bag_size_bytes", "message_count",
    "technical_qc_status", "technical_qc_reasons", "usable_rgb", "usable_depth",
    "usable_dual_view", "usable_dual_rgbd", "usable_proprio", "usable_task_state",
    "semantic_validity", "usable_for_normal_training", "usable_for_anomaly_evaluation",
    "review_status", "reviewer_id", "reviewed_at", "manual_notes",
]


def export_all(root: str | Path) -> dict[str, int]:
    paths = layout(root)
    subject = load_subject(paths.root)
    persist_current_config(paths.root, subject, load_profile(paths.root))
    db = Database(paths.database)
    db.initialize(subject)
    for file in paths.reviews.glob("*.review.yaml"):
        review = Review.model_validate(yaml.safe_load(file.read_text(encoding="utf-8")))
        _validate_portable_row(review.model_dump(), paths.root.name, set())
    trials = db.rows("""
      SELECT t.*, r.review_status, r.condition_reviewed, r.task_outcome_reviewed,
        r.semantic_validity, r.primary_anomaly_reviewed, r.secondary_consequence_reviewed,
        r.usable_for_normal_training, r.usable_for_anomaly_evaluation, r.reviewer_id,
        r.reviewed_at, r.notes AS manual_notes,
        SUM(CASE WHEN a.annotation_type='anomaly' THEN 1 ELSE 0 END) AS anomaly_count,
        MIN(CASE WHEN a.annotation_type='anomaly' THEN a.onset_ns END) / 1e9 AS first_anomaly_onset_sec,
        MAX(CASE WHEN a.annotation_type='anomaly' THEN a.offset_ns END) / 1e9 AS last_anomaly_offset_sec
      FROM trials t LEFT JOIN reviews r USING(trial_uid) LEFT JOIN annotations a USING(trial_uid)
      GROUP BY t.trial_uid ORDER BY t.trial_uid
    """)
    trial_output: list[dict[str, Any]] = []
    for row in trials:
        validate_relative(row["relative_trial_path"])
        if row["relative_mcap_path"]: validate_relative(row["relative_mcap_path"])
        row["condition_effective"] = row["condition_reviewed"] or row["condition_acquired"]
        row["task_outcome_effective"] = row["task_outcome_reviewed"] or row["task_outcome_acquired"]
        row["primary_anomaly_effective"] = row["primary_anomaly_reviewed"] or row["primary_anomaly_acquired"]
        row["secondary_consequence_effective"] = row["secondary_consequence_reviewed"] or row["secondary_consequence_acquired"]
        row["review_status"] = row["review_status"] or "unreviewed"
        _validate_portable_row(row, paths.root.name, {"relative_trial_path", "relative_mcap_path"})
        trial_output.append(row)
    topic_columns = ["subject_id", "trial_uid", "relative_trial_path", "topic_name", "message_type",
                     "message_count", "expected_hz", "mean_hz", "first_message_offset_sec",
                     "last_message_offset_sec", "coverage_ratio", "median_dt_ms", "p95_dt_ms",
                     "max_gap_ms", "required", "qc_status", "qc_reason"]
    topics = db.rows("SELECT ? AS subject_id, q.*, t.relative_trial_path FROM topic_qc q JOIN trials t USING(trial_uid) ORDER BY q.trial_uid,q.topic_name", (subject.subject_id,))
    for row in topics: _validate_portable_row(row, paths.root.name, {"relative_trial_path"}, allow_ros_topics=True)
    annotation_columns = ["subject_id", "trial_uid", "annotation_id", "annotation_type", "family", "subtype",
                          "onset_ns", "offset_ns", "onset_sec", "offset_sec", "phase_at_onset", "primary",
                          "severity", "confidence", "reviewer_id", "notes"]
    annotations = db.rows('SELECT ? AS subject_id, a.*, onset_ns/1e9 AS onset_sec, offset_ns/1e9 AS offset_sec, is_primary AS "primary" FROM annotations a ORDER BY trial_uid,onset_ns', (subject.subject_id,))
    for row in annotations: _validate_portable_row(row, paths.root.name, set())
    phase_columns = ["subject_id", "trial_uid", "phase", "start_ns", "end_ns", "start_sec", "end_sec", "duration_sec"]
    phases = db.rows("SELECT ? AS subject_id, p.*, start_ns/1e9 AS start_sec, end_ns/1e9 AS end_sec, (end_ns-start_ns)/1e9 AS duration_sec FROM phase_intervals p ORDER BY trial_uid,start_ns", (subject.subject_id,))
    for row in phases: _validate_portable_row(row, paths.root.name, set())
    paths.exports.mkdir(parents=True, exist_ok=True)
    _write(paths.exports / "trials.csv", TRIAL_COLUMNS, trial_output)
    _write(paths.exports / "topic_qc.csv", topic_columns, topics)
    _write(paths.exports / "annotations.csv", annotation_columns, annotations)
    _write(paths.exports / "phase_intervals.csv", phase_columns, phases)
    return {"trials": len(trial_output), "topics": len(topics), "annotations": len(annotations), "phases": len(phases)}


def _write(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows: writer.writerow({column: row.get(column) for column in columns})
    temporary.replace(path)


def _validate_portable_row(row: dict[str, Any], private_folder: str, path_fields: set[str],
                           allow_ros_topics: bool = False) -> None:
    absolute = re.compile(r"(?:^|[\s'\"])(?:/|[A-Za-z]:[\\/])")
    for key, value in row.items():
        if value is None: continue
        text = str(value)
        if key in path_fields: validate_relative(text)
        if allow_ros_topics and key == "topic_name": continue
        if absolute.search(text): raise ValueError(f"Absolute filesystem path in export field {key}")
        if private_folder and re.search(rf"\b{re.escape(private_folder)}\b", text, re.IGNORECASE):
            raise ValueError(f"Private source-folder identity in export field {key}")
