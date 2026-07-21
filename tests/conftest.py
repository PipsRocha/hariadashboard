from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hri_curator.config import DEFAULT_TOPICS, initialize


@pytest.fixture
def subject(tmp_path: Path) -> Path:
    root = tmp_path / "private_person_folder"
    trial = root / "handover" / "fr3_20260716_120000_000001" / "20260716_120100_000002"
    trial.mkdir(parents=True)
    (trial / "20260716_120100_000002_0.mcap").write_bytes(b"fixture")
    topic_rows = []
    for key, spec in DEFAULT_TOPICS.items():
        count = 0 if key == "camera2_depth" else (300 if spec["expected_hz"] == 30 else 10000 if spec["expected_hz"] == 1000 else 200)
        topic_rows.append({"topic_metadata": {"name": spec["topic"], "type": "std_msgs/msg/String" if key == "task_phase" else "sensor_msgs/msg/CompressedImage", "serialization_format": "cdr"}, "message_count": count})
    metadata = {"rosbag2_bagfile_information": {
        "version": 9, "storage_identifier": "mcap", "duration": {"nanoseconds": 10_000_000_000},
        "starting_time": {"nanoseconds_since_epoch": 1_700_000_000_000_000_000},
        "message_count": sum(x["message_count"] for x in topic_rows),
        "topics_with_message_count": topic_rows, "relative_file_paths": ["20260716_120100_000002_0.mcap"],
        "ros_distro": "jazzy", "files": [], "compression_format": "", "compression_mode": "",
    }}
    (trial / "metadata.yaml").write_text(yaml.safe_dump(metadata))
    acquisition = {
        "condition": "normal", "primary_anomaly": "", "secondary_consequence": "",
        "current_phase": "DONE", "current_internal_phase": "DONE", "reason": "done",
        "session_id": "session-1", "episode_id": 1, "experiment_config_sha256": "abc",
        "subject_id": "do_not_trust", "bag_path": "/private/source/path",
        "config_path": "/private/config.yaml", "launch_arguments": {"robot_ip": "10.0.0.1"},
    }
    (trial / "session_metadata.yaml").write_text(yaml.safe_dump(acquisition))
    initialize(root, "S001", "reviewer_01")
    return root
