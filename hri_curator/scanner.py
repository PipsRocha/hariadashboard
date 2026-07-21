from __future__ import annotations

import json
import re
import time
import bisect
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hri_curator.config import SCANNER_VERSION, layout, load_profile, load_subject, persist_current_config, profile_hash
from hri_curator.database import Database
from hri_curator.discovery import TrialCandidate, discover
from hri_curator.eventlog import write_event
from hri_curator.fingerprint import build as build_fingerprint
from hri_curator.metadata import AcquisitionMetadata, BagMetadata, load_acquisition_metadata, load_bag_metadata
from hri_curator.paths import relative_path
from hri_curator.qc import TopicQC, apply_timings, fast_topic_qc, trial_status, usability


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def trial_uid(subject_id: str, task: str, collection: str, trial: str) -> str:
    parts = [subject_id, task, collection, trial]
    return "_".join(re.sub(r"[^A-Za-z0-9_-]", "-", part).strip("-") for part in parts)


def scan(root: str | Path, *, deep: bool = False, force: bool = False,
         dry_run: bool = False, recheck: set[str] | None = None) -> dict[str, Any]:
    if deep:
        _require_ros()
    paths = layout(root)
    subject = load_subject(paths.root)
    profile = load_profile(paths.root)
    db = Database(paths.database, readonly=dry_run)
    if not dry_run:
        persist_current_config(paths.root, subject, profile)
        db.initialize(subject)
    candidates = discover(paths.root)
    p_hash = profile_hash(profile)
    requested_level = "deep" if deep else "fast"
    started = time.monotonic()
    summary: dict[str, Any] = {
        "subject_id": subject.subject_id, "discovered": len(candidates),
        "processed": 0, "changed": 0, "skipped": 0, "scan_errors": 0,
        "qc_failures": 0, "qc_warnings": 0,
        "dry_run": dry_run, "trials": [],
    }
    run_id: int | None = None
    if not dry_run:
        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO scan_runs (started_at, options_json) VALUES (?, ?)",
                (now_iso(), json.dumps({"deep": deep, "force": force, "recheck": sorted(recheck or [])})),
            )
            run_id = cursor.lastrowid
        write_event(paths.root, "scan_started", deep=deep, force=force, discovered=len(candidates))

    for index, candidate in enumerate(candidates, 1):
        normalized = profile.task_normalization.get(candidate.task_raw, candidate.task_raw)
        uid = trial_uid(subject.subject_id, normalized, candidate.collection_session_id, candidate.trial_directory_id)
        payload, digest = build_fingerprint(candidate, p_hash)
        existing = _existing_trial(db, uid)
        selected_recheck = bool(existing and recheck and _status_selected(existing["technical_qc_status"], recheck))
        level_sufficient = bool(existing and (
            existing["scan_level"] == "deep" or requested_level == "fast"
        ))
        unchanged = bool(existing and existing["scan_status"] == "complete" and
                         existing["fingerprint_sha256"] == digest and level_sufficient)
        if unchanged and not force and not selected_recheck:
            summary["skipped"] += 1
            summary["trials"].append({"trial_uid": uid, "action": "SKIP", "status": existing["technical_qc_status"]})
            print(f"[{index}/{len(candidates)}] SKIP  {uid} unchanged")
            continue
        action = "SCAN" if not existing else "RESCAN"
        if dry_run:
            summary["processed"] += 1
            summary["trials"].append({"trial_uid": uid, "action": action, "status": "DRY_RUN"})
            print(f"[{index}/{len(candidates)}] {action:<6} {uid} dry-run")
            continue
        try:
            result = _scan_trial(candidate, uid, subject.subject_id, normalized, profile, deep)
            _persist_trial(db, result, payload, digest, p_hash)
            from hri_curator.preview import invalidate_preview
            invalidate_preview(paths.root, uid)
            summary["processed"] += 1
            if existing: summary["changed"] += 1
            if result["scan_status"] != "complete": summary["scan_errors"] += 1
            if result["technical_qc_status"] == "FAIL": summary["qc_failures"] += 1
            elif result["technical_qc_status"] == "PASS_WITH_WARNINGS": summary["qc_warnings"] += 1
            summary["trials"].append({"trial_uid": uid, "action": action, "status": result["technical_qc_status"], "reasons": result["reasons"]})
            suffix = f" [{','.join(result['reasons'])}]" if result["reasons"] else ""
            print(f"[{index}/{len(candidates)}] {result['technical_qc_status']:<18} {uid}{suffix}")
        except Exception as exc:
            summary["processed"] += 1
            summary["scan_errors"] += 1
            summary["qc_failures"] += 1
            result = _failed_result(candidate, uid, subject.subject_id, normalized, deep, str(exc))
            _persist_trial(db, result, payload, digest, p_hash)
            write_event(paths.root, "trial_scan_error", trial_uid=uid, error=str(exc))
            summary["trials"].append({"trial_uid": uid, "action": action, "status": "FAIL", "reasons": result["reasons"]})
            print(f"[{index}/{len(candidates)}] FAIL  {uid} {exc}")

    summary["elapsed_sec"] = round(time.monotonic() - started, 3)
    summary["failures"] = summary["scan_errors"]
    if not dry_run:
        _write_reports(paths.reports, db, summary)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE scan_runs SET completed_at=?, discovered=?, processed=?, skipped=?, failures=?, "
                "scan_errors=?, qc_failures=?, qc_warnings=? WHERE scan_run_id=?",
                (now_iso(), summary["discovered"], summary["processed"], summary["skipped"],
                 summary["scan_errors"], summary["scan_errors"], summary["qc_failures"],
                 summary["qc_warnings"], run_id),
            )
        write_event(paths.root, "scan_completed", processed=summary["processed"], skipped=summary["skipped"],
                    scan_errors=summary["scan_errors"], qc_failures=summary["qc_failures"],
                    qc_warnings=summary["qc_warnings"], elapsed_sec=summary["elapsed_sec"])
    return summary


def _status_selected(status: str, values: set[str]) -> bool:
    normalized = {value.lower() for value in values}
    return (status == "FAIL" and ("failure" in normalized or "failures" in normalized)) or (
        status == "PASS_WITH_WARNINGS" and ("warning" in normalized or "warnings" in normalized)
    )


def _existing_trial(db: Database, uid: str) -> dict[str, Any] | None:
    try:
        return db.row(
            "SELECT t.scan_status, t.scan_level, t.technical_qc_status, f.fingerprint_sha256 "
            "FROM trials t LEFT JOIN trial_fingerprints f USING(trial_uid) WHERE trial_uid=?", (uid,)
        )
    except Exception as exc:
        if "scan_level" not in str(exc): raise
        row = db.row(
            "SELECT t.scan_status, t.technical_qc_status, f.fingerprint_sha256 "
            "FROM trials t LEFT JOIN trial_fingerprints f USING(trial_uid) WHERE trial_uid=?", (uid,)
        )
        if row:
            deep = db.row("SELECT 1 FROM topic_qc WHERE trial_uid=? AND coverage_ratio IS NOT NULL LIMIT 1", (uid,))
            row["scan_level"] = "deep" if deep else "fast"
        return row


def _scan_trial(candidate: TrialCandidate, uid: str, subject_id: str, task: str,
                profile: Any, deep: bool) -> dict[str, Any]:
    reasons = candidate.discovery_reasons
    bag: BagMetadata | None = None
    acquisition = AcquisitionMetadata()
    if candidate.metadata_file.is_file():
        try: bag = load_bag_metadata(candidate.metadata_file)
        except Exception: reasons.append("metadata_parse_error")
    if candidate.session_metadata_file.is_file():
        try: acquisition = load_acquisition_metadata(candidate.session_metadata_file)
        except Exception: reasons.append("session_metadata_parse_error")

    duration = bag.duration.nanoseconds / 1e9 if bag else 0.0
    rows = fast_topic_qc(profile, bag.topics_with_message_count if bag else [], duration)
    phase_intervals: list[dict[str, Any]] = []
    terminal_phase = acquisition.current_phase
    if deep and bag and candidate.mcap_files:
        try:
            timings, phase_values = _deep_read(candidate, profile)
            for row in rows:
                apply_timings(row, profile.required_topics[row.topic_key], timings.get(row.topic_name, []),
                              bag.starting_time.nanoseconds_since_epoch, duration)
            phase_intervals = _phase_intervals(phase_values, bag.starting_time.nanoseconds_since_epoch,
                                              bag.starting_time.nanoseconds_since_epoch + bag.duration.nanoseconds)
            if phase_values: terminal_phase = phase_values[-1][1]
            _apply_camera_sync(rows, timings, profile, reasons)
            _validate_phase_transitions(phase_values, task, profile, reasons)
        except RuntimeError as exc:
            if str(exc).startswith("mcap_unreadable"):
                reasons.append("mcap_unreadable")
            else:
                raise

    terminal_expected = profile.terminal_phases.get(task, [])
    if terminal_expected and terminal_phase not in terminal_expected:
        reasons.append("terminal_phase_not_reached")
    status, all_reasons = trial_status(rows, reasons)
    flags = usability(rows)
    outcome = acquisition.task_outcome or profile.controller_outcome_map.get((acquisition.reason or "").lower(), "unknown")
    mcap = candidate.mcap_files[0] if len(candidate.mcap_files) == 1 else None
    return {
        "trial_uid": uid, "subject_id": subject_id, "task_raw": candidate.task_raw,
        "task_normalized": task, "collection_session_id": candidate.collection_session_id,
        "trial_directory_id": candidate.trial_directory_id,
        "relative_trial_path": candidate.relative_path,
        "relative_mcap_path": relative_path(candidate.path.parents[2], mcap) if mcap else None,
        "mcap_filename": mcap.name if mcap else None,
        "bag_size_bytes": mcap.stat().st_size if mcap else None,
        "duration_sec": duration if bag else None,
        "starting_time_ns": bag.starting_time.nanoseconds_since_epoch if bag else None,
        "message_count": bag.message_count if bag else None,
        "ros_distro": bag.ros_distro if bag else None,
        "storage_identifier": bag.storage_identifier if bag else None,
        "condition_acquired": acquisition.condition or "unknown",
        "task_outcome_acquired": outcome,
        "primary_anomaly_acquired": acquisition.primary_anomaly or acquisition.anomaly_family or "",
        "secondary_consequence_acquired": acquisition.secondary_consequence or "",
        "controller_terminal_phase": terminal_phase,
        "controller_completion_reason": acquisition.reason,
        "session_id_acquired": acquisition.session_id,
        "episode_id_acquired": str(acquisition.episode_id) if acquisition.episode_id is not None else None,
        "experiment_config_sha256": acquisition.experiment_config_sha256,
        "scan_level": "deep" if deep else "fast",
        "scan_status": "complete" if bag and not any(r in reasons for r in ("metadata_parse_error", "mcap_unreadable")) else "failed",
        "technical_qc_status": status, "reasons": all_reasons,
        "topic_rows": rows, "phase_intervals": phase_intervals, **flags,
        "all_topics": bag.topics_with_message_count if bag else [],
    }


def _failed_result(candidate: TrialCandidate, uid: str, subject_id: str, task: str,
                   deep: bool, error: str) -> dict[str, Any]:
    return {
        "trial_uid": uid, "subject_id": subject_id, "task_raw": candidate.task_raw, "task_normalized": task,
        "collection_session_id": candidate.collection_session_id, "trial_directory_id": candidate.trial_directory_id,
        "relative_trial_path": candidate.relative_path, "relative_mcap_path": None, "mcap_filename": None,
        "bag_size_bytes": None, "duration_sec": None, "starting_time_ns": None, "message_count": None,
        "ros_distro": None, "storage_identifier": None, "condition_acquired": "unknown",
        "task_outcome_acquired": "unknown", "primary_anomaly_acquired": "",
        "secondary_consequence_acquired": "", "controller_terminal_phase": None,
        "controller_completion_reason": None, "session_id_acquired": None, "episode_id_acquired": None,
        "experiment_config_sha256": None, "scan_level": "deep" if deep else "fast",
        "scan_status": "failed", "technical_qc_status": "FAIL",
        "reasons": ["scan_exception"], "topic_rows": [], "phase_intervals": [], "all_topics": [],
        "usable_rgb": False, "usable_depth": False, "usable_dual_view": False,
        "usable_dual_rgbd": False, "usable_proprio": False, "usable_task_state": False,
        "error": error,
    }


def _deep_read(candidate: TrialCandidate, profile: Any) -> tuple[dict[str, list[int]], list[tuple[int, str]]]:
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from std_msgs.msg import String
    required = {rule.topic for rule in profile.required_topics.values()}
    phase_topic = profile.required_topics["task_phase"].topic
    timings: dict[str, list[int]] = defaultdict(list)
    phases: list[tuple[int, str]] = []
    try:
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(candidate.path), storage_id="mcap"),
                    rosbag2_py.ConverterOptions("", ""))
        reader.set_filter(rosbag2_py.StorageFilter(topics=sorted(required)))
        while reader.has_next():
            topic, data, timestamp = reader.read_next()
            timings[topic].append(timestamp)
            if topic == phase_topic:
                phases.append((timestamp, deserialize_message(data, String).data))
    except Exception as exc:
        raise RuntimeError(f"mcap_unreadable: {exc}") from exc
    return dict(timings), phases


def _require_ros() -> None:
    try:
        import rosbag2_py  # noqa: F401
        import rclpy  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Deep scan requires the ROS 2 Jazzy container") from exc


def _phase_intervals(values: list[tuple[int, str]], bag_start: int, bag_end: int) -> list[dict[str, Any]]:
    if not values: return []
    compact: list[tuple[int, str]] = []
    for timestamp, phase in values:
        if not compact or compact[-1][1] != phase: compact.append((timestamp, phase))
    return [{"phase": phase, "start_ns": start - bag_start,
             "end_ns": (compact[index + 1][0] if index + 1 < len(compact) else bag_end) - bag_start}
            for index, (start, phase) in enumerate(compact)]


def _apply_camera_sync(rows: list[TopicQC], timings: dict[str, list[int]], profile: Any,
                       reasons: list[str]) -> None:
    by_key = {row.topic_key: row for row in rows}
    for pair in profile.camera_sync_pairs:
        if len(pair) != 2 or pair[0] not in by_key or pair[1] not in by_key: continue
        left = timings.get(by_key[pair[0]].topic_name, [])
        right = timings.get(by_key[pair[1]].topic_name, [])
        if not left or not right: continue
        skews: list[float] = []
        for timestamp in left:
            pos = bisect.bisect_left(right, timestamp)
            nearest = []
            if pos < len(right): nearest.append(abs(right[pos] - timestamp))
            if pos: nearest.append(abs(right[pos - 1] - timestamp))
            if nearest: skews.append(min(nearest) / 1e6)
        if skews and float(__import__("numpy").percentile(skews, 95)) > profile.maximum_p95_sync_skew_ms:
            reasons.append("camera_sync_warning")
            for key in pair:
                if by_key[key].qc_status == "PASS": by_key[key].qc_status = "PASS_WITH_WARNINGS"
                by_key[key].qc_reason = ",".join(filter(None, [by_key[key].qc_reason, "camera_sync_warning"]))


def _validate_phase_transitions(values: list[tuple[int, str]], task: str, profile: Any,
                                reasons: list[str]) -> None:
    allowed = {tuple(pair) for pair in profile.allowed_phase_transitions.get(task, []) if len(pair) == 2}
    if not values: return
    if not allowed:
        reasons.append("phase_sequence_not_checked")
        return
    compact: list[str] = []
    for _, phase in values:
        if not compact or compact[-1] != phase: compact.append(phase)
    if any((left, right) not in allowed for left, right in zip(compact, compact[1:])):
        reasons.append("invalid_phase_sequence")


def _persist_trial(db: Database, result: dict[str, Any], fingerprint: dict[str, Any], digest: str, p_hash: str) -> None:
    columns = [
        "trial_uid", "subject_id", "task_raw", "task_normalized", "collection_session_id", "trial_directory_id",
        "relative_trial_path", "relative_mcap_path", "mcap_filename", "bag_size_bytes", "duration_sec",
        "starting_time_ns", "message_count", "ros_distro", "storage_identifier", "condition_acquired",
        "task_outcome_acquired", "primary_anomaly_acquired", "secondary_consequence_acquired",
        "controller_terminal_phase", "controller_completion_reason", "session_id_acquired", "episode_id_acquired",
        "experiment_config_sha256", "scan_status", "scan_level", "technical_qc_status", "technical_qc_reasons",
        "usable_rgb", "usable_depth", "usable_dual_view", "usable_dual_rgbd", "usable_proprio",
        "usable_task_state", "last_scanned_at",
    ]
    values = {**result, "technical_qc_reasons": ",".join(result["reasons"]), "last_scanned_at": now_iso()}
    for key in ("usable_rgb", "usable_depth", "usable_dual_view", "usable_dual_rgbd", "usable_proprio", "usable_task_state"):
        values[key] = int(bool(values[key]))
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns[1:])
    with db.transaction() as conn:
        conn.execute(f"INSERT INTO trials ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT(trial_uid) DO UPDATE SET {updates}",
                     tuple(values.get(column) for column in columns))
        conn.execute("DELETE FROM topics WHERE trial_uid=?", (result["trial_uid"],))
        conn.execute("DELETE FROM topic_qc WHERE trial_uid=?", (result["trial_uid"],))
        conn.execute("DELETE FROM phase_intervals WHERE trial_uid=?", (result["trial_uid"],))
        for topic in result["all_topics"]:
            conn.execute("INSERT INTO topics VALUES (?, ?, ?, ?)", (result["trial_uid"], topic.topic_metadata.name,
                         topic.topic_metadata.type, topic.message_count))
        for row in result["topic_rows"]:
            conn.execute("INSERT INTO topic_qc VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                result["trial_uid"], row.topic_key, row.topic_name, row.message_type, row.message_count,
                row.expected_hz, row.mean_hz, row.first_message_offset_sec, row.last_message_offset_sec,
                row.coverage_ratio, row.median_dt_ms, row.p95_dt_ms, row.max_gap_ms, int(row.required),
                row.stream_status, row.qc_status, row.qc_reason,
            ))
        for interval in result["phase_intervals"]:
            conn.execute("INSERT INTO phase_intervals VALUES (?, ?, ?, ?)",
                         (result["trial_uid"], interval["phase"], interval["start_ns"], interval["end_ns"]))
        conn.execute("INSERT OR IGNORE INTO reviews (trial_uid) VALUES (?)", (result["trial_uid"],))
        conn.execute("INSERT INTO trial_fingerprints VALUES (?, ?, ?, ?, ?) ON CONFLICT(trial_uid) DO UPDATE SET fingerprint_json=excluded.fingerprint_json, fingerprint_sha256=excluded.fingerprint_sha256, scanner_version=excluded.scanner_version, qc_profile_hash=excluded.qc_profile_hash",
                     (result["trial_uid"], json.dumps({**fingerprint, "scan_level": result["scan_level"]}, sort_keys=True),
                      digest, SCANNER_VERSION, p_hash))


def _write_reports(directory: Path, db: Database, summary: dict[str, Any]) -> None:
    import csv
    import statistics
    directory.mkdir(parents=True, exist_ok=True)
    trials = db.rows("SELECT trial_uid, task_normalized, duration_sec, scan_status, scan_level, "
                     "technical_qc_status, technical_qc_reasons FROM trials ORDER BY trial_uid")
    topics = db.rows("SELECT q.*,t.duration_sec FROM topic_qc q JOIN trials t USING(trial_uid) "
                     "ORDER BY q.topic_key,q.trial_uid")
    task_rows = db.rows("SELECT task_normalized, technical_qc_status, COUNT(*) count FROM trials GROUP BY task_normalized,technical_qc_status")
    review_rows = db.rows("SELECT review_status, COUNT(*) count FROM reviews GROUP BY review_status")
    condition_rows = db.rows("SELECT condition_reviewed,COUNT(*) count FROM reviews "
                             "WHERE condition_reviewed IS NOT NULL GROUP BY condition_reviewed")
    total_trials = len(trials)
    sensor_counts: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    frequency: list[dict[str, Any]] = []
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in topics: by_topic[row["topic_key"]].append(row)
    for topic_key, rows in sorted(by_topic.items()):
        states = _counts(rows, "stream_status")
        sensor_counts.append({
            "topic_key": topic_key, "required": bool(rows[0]["required"]), "total": len(rows),
            "complete": states.get("stream_complete", 0),
            "partial": states.get("stream_partial", 0),
            "missing": states.get("stream_missing", 0),
            "low_rate": states.get("stream_low_frequency", 0),
            "large_gap": states.get("stream_large_gap", 0),
            "percent_complete": round(100 * states.get("stream_complete", 0) / max(1, len(rows)), 1),
        })
        affected = [row for row in rows if row["stream_status"] != "stream_complete"]
        if affected:
            values = []
            proportions = []
            for row in affected:
                duration = float(row["duration_sec"] or 0)
                proportion = 1.0 if row["message_count"] == 0 else max(0.0, 1.0 - float(row["coverage_ratio"] or 0))
                values.append(duration * proportion); proportions.append(proportion)
            worst_index = max(range(len(values)), key=values.__getitem__)
            missing.append({
                "topic_key": topic_key, "affected_trials": len(affected), "total_trials": total_trials,
                "affected_percent": round(100 * len(affected) / max(1, total_trials), 1),
                "total_missing_duration_sec": round(sum(values), 3),
                "median_missing_proportion": round(statistics.median(proportions), 4),
                "worst_trial": affected[worst_index]["trial_uid"],
                "worst_missing_duration_sec": round(values[worst_index], 3),
            })
        hz = [float(row["mean_hz"]) for row in rows if row["mean_hz"] is not None]
        gaps = [float(row["max_gap_ms"]) for row in rows if row["max_gap_ms"] is not None]
        frequency.append({
            "topic_key": topic_key,
            "median_hz": round(statistics.median(hz), 3) if hz else None,
            "minimum_hz": round(min(hz), 3) if hz else None,
            "median_max_gap_ms": round(statistics.median(gaps), 3) if gaps else None,
            "worst_max_gap_ms": round(max(gaps), 3) if gaps else None,
        })
    affected_by_reason: dict[str, list[str]] = defaultdict(list)
    for row in trials:
        for reason in filter(None, row["technical_qc_reasons"].split(",")):
            affected_by_reason[reason].append(row["trial_uid"])
    payload = {**summary, "status_counts": _counts(trials, "technical_qc_status"),
               "scan_status_counts": _counts(trials, "scan_status"),
               "sensor_counts": sensor_counts, "task_distribution": task_rows,
               "review_progress": review_rows, "reviewed_conditions": condition_rows,
               "missing_duration": missing, "frequency_and_gaps": frequency,
               "affected_trials_by_reason": dict(affected_by_reason)}
    (directory / "latest_scan_summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = [f"# Scan summary: {summary['subject_id']}", "", f"- Trials discovered: {summary['discovered']}",
             f"- Processed: {summary['processed']}", f"- Skipped: {summary['skipped']}",
             f"- Scan errors: {summary['scan_errors']}", f"- QC failures: {summary['qc_failures']}",
             f"- QC warnings: {summary['qc_warnings']}", f"- Elapsed: {summary['elapsed_sec']:.2f} s", "", "## Technical QC"]
    lines.extend(f"- {key}: {value}" for key, value in sorted(payload["status_counts"].items()))
    lines.extend(["", "## Tasks"])
    lines.extend(f"- {row['task_normalized']} / {row['technical_qc_status']}: {row['count']}" for row in task_rows)
    lines.extend(["", "## Review progress"])
    lines.extend(f"- {row['review_status']}: {row['count']}" for row in review_rows)
    lines.extend(["", "## Sensor completeness"])
    lines.extend(
        f"- {row['topic_key']}: {row['complete']}/{row['total']} complete ({row['percent_complete']:.1f}%), "
        f"{row['partial']} partial, {row['missing']} missing, {row['low_rate']} low-rate, {row['large_gap']} large-gap"
        for row in sensor_counts
    )
    lines.extend(["", "## Missing duration"])
    lines.extend(
        f"- {row['topic_key']}: {row['affected_trials']}/{row['total_trials']} affected "
        f"({row['affected_percent']:.1f}%), {row['total_missing_duration_sec']:.3f} s missing, "
        f"worst {row['worst_trial']}"
        for row in missing
    )
    lines.extend(["", "## Frequency and gaps"])
    lines.extend(
        f"- {row['topic_key']}: median {row['median_hz']} Hz, minimum {row['minimum_hz']} Hz, "
        f"median max-gap {row['median_max_gap_ms']} ms, worst {row['worst_max_gap_ms']} ms"
        for row in frequency
    )
    (directory / "latest_scan_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (directory / "qc_failures.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["trial_uid", "task_normalized", "scan_status", "scan_level", "technical_qc_status", "technical_qc_reasons"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(row for row in trials if row["technical_qc_status"] != "PASS")


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for row in rows: result[row[key]] += 1
    return dict(result)
