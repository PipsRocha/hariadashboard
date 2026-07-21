from __future__ import annotations

import json
from pathlib import Path

import yaml

from hri_curator.config import layout
from hri_curator.database import Database
from hri_curator.scanner import scan


def test_incremental_scan_and_missing_depth(subject: Path):
    first = scan(subject)
    assert first["processed"] == 1
    db = Database(layout(subject).database)
    trial = db.row("SELECT * FROM trials")
    assert trial["trial_uid"].startswith("S001_unscrew_")
    assert trial["technical_qc_status"] == "PASS_WITH_WARNINGS"
    assert trial["scan_level"] == "fast"
    assert trial["usable_dual_rgbd"] == 0
    depth = db.row("SELECT * FROM topic_qc WHERE topic_key='camera2_depth'")
    assert depth["stream_status"] == "stream_missing"
    assert depth["required"] == 0
    assert depth["qc_reason"] == "optional_topic_zero_messages"

    second = scan(subject)
    assert second["skipped"] == 1
    session = next(subject.rglob("session_metadata.yaml"))
    data = yaml.safe_load(session.read_text()); data["condition"] = "anomaly"; session.write_text(yaml.safe_dump(data))
    third = scan(subject)
    assert third["processed"] == 1 and third["changed"] == 1
    assert db.row("SELECT condition_acquired FROM trials")["condition_acquired"] == "anomaly"


def test_fast_scan_upgrades_to_deep_and_never_downgrades(subject: Path, monkeypatch):
    import hri_curator.scanner as scanner
    scan(subject)
    monkeypatch.setattr(scanner, "_require_ros", lambda: None)
    monkeypatch.setattr(scanner, "_deep_read", lambda candidate, profile: ({}, []))
    deep = scan(subject, deep=True)
    assert deep["processed"] == 1
    db = Database(layout(subject).database)
    assert db.row("SELECT scan_level FROM trials")["scan_level"] == "deep"
    fast = scan(subject)
    assert fast["skipped"] == 1
    assert db.row("SELECT scan_level FROM trials")["scan_level"] == "deep"


def test_changed_scan_invalidates_preview_cache(subject: Path):
    scan(subject)
    db = Database(layout(subject).database)
    uid = db.row("SELECT trial_uid FROM trials")["trial_uid"]
    cache = layout(subject).cache / uid
    cache.mkdir(parents=True)
    (cache / "index.json").write_text("{}")
    session = next(subject.rglob("session_metadata.yaml"))
    data = yaml.safe_load(session.read_text()); data["condition"] = "ambiguous"
    session.write_text(yaml.safe_dump(data))
    scan(subject)
    assert not cache.exists()


def test_dry_run_does_not_migrate_or_write(subject: Path):
    paths = layout(subject)
    subject_data = yaml.safe_load(paths.subject_file.read_text()); subject_data["schema_version"] = 1
    paths.subject_file.write_text(yaml.safe_dump(subject_data))
    profile_data = yaml.safe_load(paths.profile_file.read_text()); profile_data["schema_version"] = 1
    for key in ("camera1_depth", "camera2_depth"):
        profile_data["required_topics"][key].pop("required_for_trial", None)
    paths.profile_file.write_text(yaml.safe_dump(profile_data))
    before = {path: path.read_bytes() for path in (paths.subject_file, paths.profile_file, paths.database)}
    result = scan(subject, dry_run=True)
    assert result["dry_run"] is True
    assert before == {path: path.read_bytes() for path in before}


def test_reports_contain_no_private_paths(subject: Path):
    scan(subject)
    report = (layout(subject).reports / "latest_scan_summary.json").read_text()
    assert str(subject) not in report
    assert "private_person_folder" not in report
    assert json.loads(report)["subject_id"] == "S001"
    payload = json.loads(report)
    assert payload["scan_errors"] == 0
    assert payload["sensor_counts"] and payload["frequency_and_gaps"]
    assert payload["missing_duration"][0]["affected_percent"] >= 0
    log = (layout(subject).logs / "curator.log").read_text()
    assert str(subject) not in log and "private_person_folder" not in log


def test_deep_scan_fails_before_mutating_without_ros(subject: Path):
    import importlib.util
    import pytest
    if importlib.util.find_spec("rosbag2_py"):
        pytest.skip("ROS deep reader is available")
    database = layout(subject).database
    before = database.stat().st_mtime_ns
    with pytest.raises(RuntimeError, match="ROS 2 Jazzy container"):
        scan(subject, deep=True)
    assert database.stat().st_mtime_ns == before
