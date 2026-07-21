from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class TopicMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    type: str
    serialization_format: str = "cdr"


class TopicCount(BaseModel):
    model_config = ConfigDict(extra="ignore")
    topic_metadata: TopicMetadata
    message_count: int = Field(ge=0)


class Duration(BaseModel):
    nanoseconds: int = Field(ge=0)


class StartingTime(BaseModel):
    nanoseconds_since_epoch: int = Field(ge=0)


class BagMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: int
    storage_identifier: str
    duration: Duration
    starting_time: StartingTime
    message_count: int = Field(ge=0)
    topics_with_message_count: list[TopicCount]
    relative_file_paths: list[str] = Field(default_factory=list)
    ros_distro: str = ""


class AcquisitionMetadata(BaseModel):
    """Allowlisted acquisition values. Private paths and source identity are ignored."""

    model_config = ConfigDict(extra="ignore")
    condition: str | None = None
    anomaly_family: str | None = None
    primary_anomaly: str | None = None
    secondary_consequence: str | None = None
    current_internal_phase: str | None = None
    current_phase: str | None = None
    reason: str | None = None
    session_id: str | None = None
    episode_id: str | int | None = None
    experiment_config_sha256: str | None = None
    task_outcome: str | None = None


def load_bag_metadata(path: Path) -> BagMetadata:
    data = _load_yaml(path)
    info = data.get("rosbag2_bagfile_information", data)
    return BagMetadata.model_validate(info)


def load_acquisition_metadata(path: Path) -> AcquisitionMetadata:
    return AcquisitionMetadata.model_validate(_load_yaml(path))


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path.name}")
    return data
