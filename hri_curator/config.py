from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hri_curator.paths import subject_root

SCHEMA_VERSION = 2
SCANNER_VERSION = "2"


class SubjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    subject_id: str
    dataset_version: str = "pilot_v1"
    reviewer_assignment: str

    @field_validator("subject_id")
    @classmethod
    def valid_subject_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,31}", value):
            raise ValueError("subject ID must be 2-32 letters, digits, '_' or '-'")
        return value

    @field_validator("reviewer_assignment")
    @classmethod
    def valid_reviewer(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 64:
            raise ValueError("reviewer must be 1-64 characters")
        return value


class TopicRule(BaseModel):
    topic: str
    required_for_trial: bool = True
    expected_hz: float = Field(gt=0)
    minimum_mean_hz: float = Field(ge=0)
    maximum_gap_ms: float = Field(gt=0)
    minimum_coverage_ratio: float = Field(ge=0, le=1)


class QCProfile(BaseModel):
    schema_version: int = SCHEMA_VERSION
    profile_id: str = "dual_rgbd_fr3_v1"
    profile_version: int = 1
    task_normalization: dict[str, str] = {"handover": "unscrew", "pour": "pour", "tray": "tray"}
    required_topics: dict[str, TopicRule]
    camera_sync_pairs: list[list[str]] = [["camera1_rgb", "camera2_rgb"]]
    maximum_p95_sync_skew_ms: float = 50.0
    terminal_phases: dict[str, list[str]] = {
        "unscrew": ["DONE"], "pour": ["DONE"], "tray": ["DONE"]
    }
    allowed_phase_transitions: dict[str, list[list[str]]] = {}
    controller_outcome_map: dict[str, str] = {"done": "success"}


DEFAULT_TOPICS: dict[str, dict[str, Any]] = {
    "camera1_rgb": {"topic": "/camera1/camera1/color/image_raw/compressed", "expected_hz": 30, "minimum_mean_hz": 27, "maximum_gap_ms": 250, "minimum_coverage_ratio": 0.95},
    "camera1_depth": {"topic": "/camera1/camera1/depth/image_rect_raw/compressedDepth", "required_for_trial": False, "expected_hz": 30, "minimum_mean_hz": 27, "maximum_gap_ms": 250, "minimum_coverage_ratio": 0.95},
    "camera2_rgb": {"topic": "/camera2/camera2/color/image_raw/compressed", "expected_hz": 30, "minimum_mean_hz": 27, "maximum_gap_ms": 250, "minimum_coverage_ratio": 0.95},
    "camera2_depth": {"topic": "/camera2/camera2/depth/image_rect_raw/compressedDepth", "required_for_trial": False, "expected_hz": 30, "minimum_mean_hz": 27, "maximum_gap_ms": 250, "minimum_coverage_ratio": 0.95},
    "robot_pose": {"topic": "/franka_robot_state_broadcaster/current_pose", "expected_hz": 1000, "minimum_mean_hz": 900, "maximum_gap_ms": 50, "minimum_coverage_ratio": 0.98},
    "external_joint_torques": {"topic": "/franka_robot_state_broadcaster/external_joint_torques", "expected_hz": 1000, "minimum_mean_hz": 900, "maximum_gap_ms": 50, "minimum_coverage_ratio": 0.98},
    "external_wrench": {"topic": "/franka_robot_state_broadcaster/external_wrench_in_base_frame", "expected_hz": 1000, "minimum_mean_hz": 900, "maximum_gap_ms": 50, "minimum_coverage_ratio": 0.98},
    "measured_joint_states": {"topic": "/franka_robot_state_broadcaster/measured_joint_states", "expected_hz": 1000, "minimum_mean_hz": 900, "maximum_gap_ms": 50, "minimum_coverage_ratio": 0.98},
    "task_phase": {"topic": "/task_state/phase", "expected_hz": 20, "minimum_mean_hz": 15, "maximum_gap_ms": 500, "minimum_coverage_ratio": 0.90},
}


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def curation(self) -> Path: return self.root / "_curation"
    @property
    def subject_file(self) -> Path: return self.curation / "subject.yaml"
    @property
    def database(self) -> Path: return self.curation / "curator.sqlite"
    @property
    def profile_file(self) -> Path: return self.curation / "config" / "qc_profile.yaml"
    @property
    def reviews(self) -> Path: return self.curation / "reviews"
    @property
    def exports(self) -> Path: return self.curation / "exports"
    @property
    def reports(self) -> Path: return self.curation / "reports"
    @property
    def cache(self) -> Path: return self.curation / "cache"
    @property
    def logs(self) -> Path: return self.curation / "logs"


def layout(root: str | Path) -> Layout:
    return Layout(subject_root(root))


def load_subject(root: str | Path) -> SubjectConfig:
    paths = layout(root)
    if not paths.subject_file.exists():
        raise ValueError(f"Not initialized: {paths.root}. Run hri-curator init first.")
    data = yaml.safe_load(paths.subject_file.read_text())
    subject = SubjectConfig.model_validate(data)
    if subject.schema_version > SCHEMA_VERSION:
        raise ValueError(f"Unsupported subject schema {subject.schema_version}")
    if subject.schema_version < SCHEMA_VERSION:
        subject = subject.model_copy(update={"schema_version": SCHEMA_VERSION})
    return subject


def load_profile(root: str | Path) -> QCProfile:
    paths = layout(root)
    data = yaml.safe_load(paths.profile_file.read_text())
    if not isinstance(data, dict): raise ValueError("QC profile must be a YAML mapping")
    required = data.get("required_topics", {})
    for key in ("camera1_depth", "camera2_depth"):
        if key in required and "required_for_trial" not in required[key]:
            required[key]["required_for_trial"] = False
    profile = QCProfile.model_validate(data)
    if profile.schema_version > SCHEMA_VERSION:
        raise ValueError(f"Unsupported QC profile schema {profile.schema_version}")
    if profile.schema_version < SCHEMA_VERSION:
        profile = profile.model_copy(update={"schema_version": SCHEMA_VERSION})
    return profile


def persist_current_config(root: str | Path, subject: SubjectConfig, profile: QCProfile) -> None:
    paths = layout(root)
    subject_data = yaml.safe_load(paths.subject_file.read_text())
    profile_data = yaml.safe_load(paths.profile_file.read_text())
    if subject_data != subject.model_dump(mode="python"):
        _atomic_yaml(paths.subject_file, subject.model_dump())
    if profile_data != profile.model_dump(mode="python"):
        _atomic_yaml(paths.profile_file, profile.model_dump())


def profile_hash(profile: QCProfile) -> str:
    value = profile.model_dump_json(exclude_none=False)
    return hashlib.sha256(value.encode()).hexdigest()


def initialize(root: str | Path, subject_id: str, reviewer: str) -> Layout:
    paths = layout(root)
    config = SubjectConfig(subject_id=subject_id, reviewer_assignment=reviewer)
    if paths.subject_file.exists():
        existing = load_subject(paths.root)
        if existing != config:
            raise ValueError("Subject root is already initialized with different settings")
        profile = load_profile(paths.root)
        persist_current_config(paths.root, existing, profile)
        from hri_curator.database import Database
        Database(paths.database).initialize(existing)
        return paths
    for directory in (paths.curation, paths.profile_file.parent, paths.reviews, paths.exports, paths.reports, paths.cache, paths.logs):
        directory.mkdir(parents=True, exist_ok=True)
    _atomic_yaml(paths.subject_file, config.model_dump())
    _atomic_yaml(paths.profile_file, QCProfile(required_topics=DEFAULT_TOPICS).model_dump())
    from hri_curator.database import Database
    Database(paths.database).initialize(config)
    return paths


def _atomic_yaml(path: Path, data: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    temp.replace(path)
