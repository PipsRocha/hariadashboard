from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hri_curator.config import SCHEMA_VERSION, layout, load_subject
from hri_curator.database import Database
from hri_curator.eventlog import write_event


class ReviewConflict(ValueError):
    pass


class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    annotation_id: str = Field(default_factory=lambda: str(uuid4()))
    annotation_type: Literal["anomaly", "human_action", "robot_event", "task_failure", "recovery", "consequence", "uncertain_region"]
    family: str | None = None
    subtype: str | None = None
    onset_ns: int = Field(ge=0)
    offset_ns: int = Field(ge=0)
    phase_at_onset: str | None = None
    internal_phase_at_onset: str | None = None
    primary: bool = False
    severity: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    reviewer_id: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def valid_interval(self) -> "Annotation":
        if self.offset_ns < self.onset_ns:
            raise ValueError("annotation offset must be at or after onset")
        return self


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    trial_uid: str
    review_version: int = Field(default=0, ge=0)
    review_status: Literal["unreviewed", "in_progress", "reviewed", "needs_second_review"] = "unreviewed"
    condition_reviewed: Literal["normal", "anomaly", "ambiguous", "exclude"] | None = None
    task_outcome_reviewed: Literal["success", "recovered", "incomplete", "failure", "operator_abort", "robot_abort", "unknown"] | None = None
    semantic_validity: Literal["valid", "questionable", "invalid"] | None = None
    primary_anomaly_reviewed: str | None = None
    secondary_consequence_reviewed: str | None = None
    anomaly_present: bool | None = None
    usable_for_normal_training: bool | None = None
    usable_for_anomaly_evaluation: bool | None = None
    manual_exclusion_reason: str | None = None
    reviewer_id: str | None = None
    reviewed_at: str | None = None
    updated_at: str | None = None
    notes: str = ""
    annotations: list[Annotation] = Field(default_factory=list)

    @model_validator(mode="after")
    def completed_review_is_explicit(self) -> "Review":
        if self.review_status != "reviewed": return self
        required = {
            "condition_reviewed": self.condition_reviewed,
            "task_outcome_reviewed": self.task_outcome_reviewed,
            "semantic_validity": self.semantic_validity,
            "anomaly_present": self.anomaly_present,
            "usable_for_normal_training": self.usable_for_normal_training,
            "usable_for_anomaly_evaluation": self.usable_for_anomaly_evaluation,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing: raise ValueError(f"reviewed trials require: {', '.join(missing)}")
        if self.anomaly_present and not self.primary_anomaly_reviewed:
            raise ValueError("reviewed anomalous trials require primary_anomaly_reviewed")
        if (self.condition_reviewed == "exclude" or self.semantic_validity == "invalid") and not self.manual_exclusion_reason:
            raise ValueError("excluded or invalid trials require manual_exclusion_reason")
        return self


def load_review(root: str | Path, trial_uid: str) -> Review:
    paths = layout(root)
    db = Database(paths.database)
    row = db.row("SELECT * FROM reviews WHERE trial_uid=?", (trial_uid,))
    if not row: raise KeyError(trial_uid)
    file = paths.reviews / f"{trial_uid}.review.yaml"
    if file.exists():
        sidecar = Review.model_validate(yaml.safe_load(file.read_text(encoding="utf-8")))
        if sidecar.review_version >= int(row["review_version"]): return sidecar
    annotations = db.rows("SELECT * FROM annotations WHERE trial_uid=? ORDER BY onset_ns", (trial_uid,))
    annotation_payloads = []
    for annotation in annotations:
        annotation.pop("trial_uid", None)
        annotation["primary"] = bool(annotation.pop("is_primary"))
        annotation_payloads.append(annotation)
    payload = {
        **row, "schema_version": SCHEMA_VERSION,
        "notes": row["notes"] or "",
        "reviewer_id": row["reviewer_id"] or load_subject(paths.root).reviewer_assignment,
        "anomaly_present": _from_bool(row["anomaly_present"]),
        "usable_for_normal_training": _from_bool(row["usable_for_normal_training"]),
        "usable_for_anomaly_evaluation": _from_bool(row["usable_for_anomaly_evaluation"]),
        "annotations": annotation_payloads,
    }
    return Review.model_validate(payload)


def save_review(root: str | Path, review: Review) -> Review:
    paths = layout(root)
    db = Database(paths.database)
    trial = db.row("SELECT duration_sec FROM trials WHERE trial_uid=?", (review.trial_uid,))
    if not trial: raise KeyError(review.trial_uid)
    current = db.row("SELECT review_version FROM reviews WHERE trial_uid=?", (review.trial_uid,))
    current_version = int(current["review_version"] if current else 0)
    if review.review_version != current_version:
        raise ReviewConflict(f"review changed from version {review.review_version} to {current_version}")
    _prepare_review(db, review, int(float(trial["duration_sec"] or 0) * 1e9))
    now = datetime.now(UTC).isoformat()
    review.schema_version = SCHEMA_VERSION
    review.review_version = current_version + 1
    review.updated_at = now
    if review.review_status == "reviewed" and not review.reviewed_at: review.reviewed_at = now
    _validate_privacy(paths.root, review)
    _write_sidecar(paths.reviews / f"{review.trial_uid}.review.yaml", review)
    _persist_review(db, review)
    write_event(paths.root, "review_saved", trial_uid=review.trial_uid,
                review_status=review.review_status, review_version=review.review_version)
    return review


def reconcile_sidecars(root: str | Path) -> int:
    paths = layout(root)
    db = Database(paths.database)
    count = 0
    for file in paths.reviews.glob("*.review.yaml"):
        try:
            review = Review.model_validate(yaml.safe_load(file.read_text(encoding="utf-8")))
            current = db.row("SELECT review_version FROM reviews WHERE trial_uid=?", (review.trial_uid,))
            if current is None: continue
            if review.review_version > int(current["review_version"]):
                trial = db.row("SELECT duration_sec FROM trials WHERE trial_uid=?", (review.trial_uid,))
                _prepare_review(db, review, int(float(trial["duration_sec"] or 0) * 1e9))
                _validate_privacy(paths.root, review)
                _persist_review(db, review); count += 1
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return count


def _prepare_review(db: Database, review: Review, duration_ns: int) -> None:
    if review.reviewer_id is None:
        raise ValueError("reviewer_id is required")
    phases = db.rows("SELECT phase,start_ns,end_ns FROM phase_intervals WHERE trial_uid=? ORDER BY start_ns", (review.trial_uid,))
    for annotation in review.annotations:
        if annotation.offset_ns > duration_ns:
            raise ValueError("annotation exceeds trial duration")
        if annotation.phase_at_onset is None:
            match = next((row for row in phases if row["start_ns"] <= annotation.onset_ns <= row["end_ns"]), None)
            if match: annotation.phase_at_onset = match["phase"]
        if annotation.reviewer_id is None: annotation.reviewer_id = review.reviewer_id


def _persist_review(db: Database, review: Review) -> None:
    with db.transaction() as conn:
        conn.execute("""
          INSERT INTO reviews (trial_uid, review_status, condition_reviewed, task_outcome_reviewed,
            semantic_validity, primary_anomaly_reviewed, secondary_consequence_reviewed,
            anomaly_present, usable_for_normal_training, usable_for_anomaly_evaluation,
            manual_exclusion_reason, reviewer_id, reviewed_at, updated_at, notes, review_version)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(trial_uid) DO UPDATE SET
            review_status=excluded.review_status, condition_reviewed=excluded.condition_reviewed,
            task_outcome_reviewed=excluded.task_outcome_reviewed, semantic_validity=excluded.semantic_validity,
            primary_anomaly_reviewed=excluded.primary_anomaly_reviewed,
            secondary_consequence_reviewed=excluded.secondary_consequence_reviewed,
            anomaly_present=excluded.anomaly_present,
            usable_for_normal_training=excluded.usable_for_normal_training,
            usable_for_anomaly_evaluation=excluded.usable_for_anomaly_evaluation,
            manual_exclusion_reason=excluded.manual_exclusion_reason, reviewer_id=excluded.reviewer_id,
            reviewed_at=excluded.reviewed_at, updated_at=excluded.updated_at,
            notes=excluded.notes, review_version=excluded.review_version
        """, (review.trial_uid, review.review_status, review.condition_reviewed, review.task_outcome_reviewed,
              review.semantic_validity, review.primary_anomaly_reviewed, review.secondary_consequence_reviewed,
              _bool(review.anomaly_present), _bool(review.usable_for_normal_training),
              _bool(review.usable_for_anomaly_evaluation), review.manual_exclusion_reason,
              review.reviewer_id, review.reviewed_at, review.updated_at, review.notes, review.review_version))
        conn.execute("DELETE FROM annotations WHERE trial_uid=?", (review.trial_uid,))
        for ann in review.annotations:
            conn.execute("INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                ann.annotation_id, review.trial_uid, ann.annotation_type, ann.family, ann.subtype,
                ann.onset_ns, ann.offset_ns, ann.phase_at_onset, ann.internal_phase_at_onset,
                int(ann.primary), ann.severity, ann.confidence, ann.reviewer_id or review.reviewer_id, ann.notes,
            ))


def _write_sidecar(target: Path, review: Review) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".yaml.tmp")
    with temp.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(review.model_dump(), stream, sort_keys=False)
        stream.flush(); os.fsync(stream.fileno())
    temp.replace(target)


def _validate_privacy(root: Path, review: Review) -> None:
    text = yaml.safe_dump(review.model_dump(), sort_keys=False)
    absolute = re.compile(r"(?:^|[\s'\"])(?:/|[A-Za-z]:[\\/])", re.MULTILINE)
    if absolute.search(text): raise ValueError("review contains an absolute filesystem path")
    if root.name and re.search(rf"\b{re.escape(root.name)}\b", text, re.IGNORECASE):
        raise ValueError("review contains the private source-folder identity")


def _bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _from_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)
