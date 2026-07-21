from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from hri_curator.config import QCProfile, TopicRule
from hri_curator.metadata import TopicCount


@dataclass
class TopicQC:
    topic_key: str
    topic_name: str
    message_type: str | None
    message_count: int
    expected_hz: float
    required: bool = True
    mean_hz: float | None = None
    first_message_offset_sec: float | None = None
    last_message_offset_sec: float | None = None
    coverage_ratio: float | None = None
    median_dt_ms: float | None = None
    p95_dt_ms: float | None = None
    max_gap_ms: float | None = None
    stream_status: str = "stream_missing"
    qc_status: str = "FAIL"
    qc_reason: str = "required_topic_missing"


def fast_topic_qc(profile: QCProfile, topics: list[TopicCount], duration_sec: float) -> list[TopicQC]:
    by_name = {entry.topic_metadata.name: entry for entry in topics}
    rows: list[TopicQC] = []
    for key, rule in profile.required_topics.items():
        found = by_name.get(rule.topic)
        count = found.message_count if found else 0
        msg_type = found.topic_metadata.type if found else None
        if found is None:
            rows.append(TopicQC(
                key, rule.topic, None, 0, rule.expected_hz,
                required=rule.required_for_trial,
                qc_status="FAIL" if rule.required_for_trial else "PASS_WITH_WARNINGS",
                qc_reason="required_topic_missing" if rule.required_for_trial else "optional_topic_missing",
            ))
        elif count == 0:
            rows.append(TopicQC(key, rule.topic, msg_type, 0, rule.expected_hz,
                                required=rule.required_for_trial,
                                stream_status="stream_missing", qc_status="FAIL",
                                qc_reason="required_topic_zero_messages"))
            if not rule.required_for_trial:
                rows[-1].qc_status = "PASS_WITH_WARNINGS"
                rows[-1].qc_reason = "optional_topic_zero_messages"
        else:
            mean = count / duration_sec if duration_sec > 0 else 0
            status = "PASS" if mean >= rule.minimum_mean_hz else "PASS_WITH_WARNINGS"
            reason = "" if status == "PASS" else "low_topic_frequency"
            rows.append(TopicQC(key, rule.topic, msg_type, count, rule.expected_hz,
                                required=rule.required_for_trial,
                                mean_hz=mean, stream_status="stream_complete",
                                qc_status=status, qc_reason=reason))
    return rows


def apply_timings(row: TopicQC, rule: TopicRule, timestamps_ns: list[int], bag_start_ns: int,
                  duration_sec: float) -> TopicQC:
    if not timestamps_ns:
        return row
    values = np.asarray(timestamps_ns, dtype=np.int64)
    gaps_ms = np.diff(values).astype(np.float64) / 1e6
    first = (int(values[0]) - bag_start_ns) / 1e9
    last = (int(values[-1]) - bag_start_ns) / 1e9
    coverage = max(0.0, last - first)
    row.first_message_offset_sec = first
    row.last_message_offset_sec = last
    row.coverage_ratio = min(1.0, coverage / duration_sec) if duration_sec > 0 else 0.0
    row.mean_hz = (len(values) - 1) / coverage if coverage > 0 and len(values) > 1 else 0.0
    if len(gaps_ms):
        row.median_dt_ms = float(np.median(gaps_ms))
        row.p95_dt_ms = float(np.percentile(gaps_ms, 95))
        row.max_gap_ms = float(np.max(gaps_ms))

    reasons: list[str] = []
    if row.coverage_ratio < rule.minimum_coverage_ratio:
        row.stream_status = "stream_partial"
        reasons.append("required_stream_partial" if rule.required_for_trial else "optional_stream_partial")
        if first > duration_sec * (1 - rule.minimum_coverage_ratio): reasons.append("stream_starts_late")
        if duration_sec - last > duration_sec * (1 - rule.minimum_coverage_ratio): reasons.append("stream_ends_early")
    elif row.mean_hz < rule.minimum_mean_hz:
        row.stream_status = "stream_low_frequency"
        reasons.append("low_topic_frequency")
    elif row.max_gap_ms is not None and row.max_gap_ms > rule.maximum_gap_ms:
        row.stream_status = "stream_large_gap"
        reasons.append("large_timestamp_gap")
    else:
        row.stream_status = "stream_complete"
    row.qc_reason = ",".join(dict.fromkeys(reasons))
    row.qc_status = "PASS" if not reasons else "PASS_WITH_WARNINGS"
    return row


def trial_status(rows: list[TopicQC], discovery_reasons: list[str]) -> tuple[str, list[str]]:
    reasons = list(discovery_reasons)
    reasons.extend(reason for row in rows for reason in row.qc_reason.split(",") if reason)
    reasons = list(dict.fromkeys(reasons))
    from hri_curator.database import REASON_CODES
    if any(REASON_CODES.get(reason, ("failure", ""))[0] == "failure" for reason in reasons):
        return "FAIL", reasons
    if any(row.qc_status == "FAIL" for row in rows): return "FAIL", reasons
    if any(row.qc_status == "PASS_WITH_WARNINGS" for row in rows): return "PASS_WITH_WARNINGS", reasons
    return "PASS", reasons


def usability(rows: list[TopicQC]) -> dict[str, bool]:
    okay = {row.topic_key: row.message_count > 0 and row.qc_status != "FAIL" for row in rows}
    return {
        "usable_rgb": okay.get("camera1_rgb", False) or okay.get("camera2_rgb", False),
        "usable_depth": okay.get("camera1_depth", False) or okay.get("camera2_depth", False),
        "usable_dual_view": okay.get("camera1_rgb", False) and okay.get("camera2_rgb", False),
        "usable_dual_rgbd": all(okay.get(key, False) for key in ("camera1_rgb", "camera1_depth", "camera2_rgb", "camera2_depth")),
        "usable_proprio": all(okay.get(key, False) for key in ("robot_pose", "external_joint_torques", "external_wrench", "measured_joint_states")),
        "usable_task_state": okay.get("task_phase", False),
    }
