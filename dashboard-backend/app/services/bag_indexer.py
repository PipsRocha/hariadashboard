"""
Pre-processes a recorded ROS 2 bag (.mcap or .db3) into a time-indexed
on-disk archive the frontend can scrub through freely:

    session_out/
    ├── index.json                # topic list + global time range
    └── <topic_slug>/
        ├── data.jsonl            # one JSON entry per message, keyed by "t"
        ├── latest.json           # most recent decoded message
        ├── <t>.jpg               # per-frame JPEGs (image topics)
        └── latest.jpg

Runs as a FastAPI background task; progress is reported through
`session_state.session.status` and polled by the frontend.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from app.services.session_state import session

# Types too large / not useful to fully deserialise for the dashboard.
# Their timestamps are still written so the timeline shows coverage.
SKIP_TYPES = {
    "std_msgs/msg/String",              # robot_description URDF — huge
    "moveit_msgs/msg/PlanningScene",
    "moveit_msgs/msg/DisplayTrajectory",
    "moveit_msgs/msg/AttachedCollisionObject",
    "moveit_msgs/msg/CollisionObject",
    "rcl_interfaces/msg/ParameterEvent",
    "rosgraph_msgs/msg/Clock",
    "tf2_msgs/msg/TFMessage",
}

NUMERIC_HINTS = ["Float", "Int", "UInt", "Bool", "JointState", "Imu",
                 "Wrench", "Twist", "Pose", "Vector3", "Odometry", "NavSatFix"]


def slug(topic: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", topic).strip("_")


def is_numeric_type(msg_type: str) -> bool:
    return any(n in msg_type for n in NUMERIC_HINTS)


def wipe_dir(path: Path) -> None:
    """Empty a session output directory so archives never mix."""
    from app.services.session_cache import session_cache
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    session_cache.invalidate()


def find_bag_root(base: Path) -> Path:
    """Locate the directory containing metadata.yaml inside an extracted archive."""
    if (base / "metadata.yaml").exists():
        return base
    for m in sorted(base.rglob("metadata.yaml")):
        return m.parent
    return base


# ---------------------------------------------------------------------------
# Entry point (run as a background task)
# ---------------------------------------------------------------------------

def preprocess_bag(bag_path: Path, out_dir: Path) -> None:
    session.set_status("processing", "Reading bag metadata…")
    try:
        storage_id = _detect_storage(bag_path)
        wipe_dir(out_dir)
        if storage_id == "mcap":
            _preprocess_mcap(bag_path, out_dir)
        elif storage_id == "sqlite3":
            _preprocess_sqlite(bag_path, out_dir)
        else:
            raise FileNotFoundError(
                f"No .mcap or .db3 files found in {bag_path}"
            )
        session.set_status("ready", "Done")
    except Exception:
        session.set_status("error", "", traceback.format_exc())


def _detect_storage(bag_path: Path) -> Optional[str]:
    meta_file = bag_path / "metadata.yaml"
    if meta_file.exists():
        import yaml
        meta = yaml.safe_load(meta_file.read_text())
        if isinstance(meta, dict):
            info = meta.get("rosbag2_bagfile_information", meta)
            if isinstance(info, dict) and info.get("storage_identifier"):
                return info["storage_identifier"]
    if list(bag_path.glob("*.mcap")):
        return "mcap"
    if list(bag_path.glob("*.db3")):
        return "sqlite3"
    return None


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

class _TopicWriter:
    """Per-topic sink with a long-lived file handle. Opening data.jsonl per
    message (the previous approach) dominated indexing time on large bags;
    latest.json / latest.jpg are only written once, on close()."""

    def __init__(self, out_dir: Path, topic: str, msg_type: str) -> None:
        self.topic    = topic
        self.msg_type = msg_type
        self.tdir     = out_dir / slug(topic)
        self.tdir.mkdir(parents=True, exist_ok=True)
        self._jsonl = (self.tdir / "data.jsonl").open("a", encoding="utf-8")
        self._last_entry: Optional[dict] = None
        self._last_img:   Optional[bytes] = None

    def timestamp_only(self, t: float) -> None:
        self._jsonl.write(json.dumps({"t": t}) + "\n")

    def add(self, t: float, raw: Any, img_bytes: Optional[bytes] = None) -> None:
        if img_bytes is not None:
            (self.tdir / f"{t:.3f}.jpg").write_bytes(img_bytes)
            self._last_img = img_bytes
            entry = {"t": t, "type": "image", "frame": f"{t:.3f}.jpg"}
        else:
            if not isinstance(raw, dict):
                raw = {"_raw_str": str(raw)}
            entry = {"t": t, "_raw": raw}
            if "JointState" in self.msg_type and "name" in raw:
                entry["__names"]  = raw.get("name", [])
                entry["position"] = raw.get("position", [])
                entry["velocity"] = raw.get("velocity", [])
                entry["effort"]   = raw.get("effort", [])
            elif is_numeric_type(self.msg_type):
                # Top-level numeric fields so chart panels can plot without _raw
                entry.update(_numeric_summary(raw))
            self._last_entry = entry
        self._jsonl.write(json.dumps(entry) + "\n")

    def close(self) -> None:
        try:
            self._jsonl.close()
        except Exception:
            pass
        if self._last_img is not None:
            (self.tdir / "latest.jpg").write_bytes(self._last_img)
        if self._last_entry is not None:
            (self.tdir / "latest.json").write_text(
                json.dumps({"t": self._last_entry["t"], "topic": self.topic,
                            "msg_type": self.msg_type, **self._last_entry}),
                encoding="utf-8",
            )


def _write_index(out_dir: Path, topic_info: dict, t_start: float, t_end: float) -> None:
    topics = [
        {**ti, "topic": name, "active": False, "last_msg": ti.get("t_end", t_end)}
        for name, ti in topic_info.items()
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.json").write_text(json.dumps({
        "timestamp": time.time(),
        "t_start": t_start,
        "t_end": t_end,
        "topics": topics,
    }), encoding="utf-8")


def _new_topic_info(msg_type: str, topic: str, t: float) -> dict:
    return {
        "msg_type": msg_type, "slug": slug(topic), "count": 0,
        "t_start": t, "t_end": t,
        "is_image": "Image" in msg_type,
        "is_num":   is_numeric_type(msg_type),
        "is_table": "JointState" in msg_type,
    }


# ---------------------------------------------------------------------------
# MCAP
# ---------------------------------------------------------------------------

def _preprocess_mcap(bag_path: Path, out_dir: Path) -> None:
    from mcap.reader import make_reader
    from mcap_ros2.decoder import DecoderFactory

    mcap_files = sorted(bag_path.glob("*.mcap"))

    # Pass 1: count messages for progress reporting.
    session.set_progress("Counting messages…")
    total = 0
    for mf in mcap_files:
        with mf.open("rb") as f:
            summary = make_reader(f).get_summary()
            if summary and summary.statistics:
                total += summary.statistics.message_count
            else:
                for _ in make_reader(f).iter_messages():
                    total += 1

    topic_info: dict = {}
    writers: dict[str, _TopicWriter] = {}
    t_start = t_end = None
    processed = 0
    last_pct = -1

    try:
        for mf in mcap_files:
            with mf.open("rb") as f:
                reader = make_reader(f, decoder_factories=[DecoderFactory()])
                for schema, channel, message, decoded in reader.iter_decoded_messages():
                    processed += 1
                    pct = int(processed / max(1, total) * 100)
                    if pct != last_pct:
                        last_pct = pct
                        session.set_progress(f"Processing… {pct}% ({processed}/{total} msgs)")

                    t        = message.log_time / 1e9
                    topic    = channel.topic
                    msg_type = schema.name if schema else "unknown"

                    t_start = t if t_start is None else min(t_start, t)
                    t_end   = t if t_end   is None else max(t_end,   t)

                    ti = topic_info.setdefault(topic, _new_topic_info(msg_type, topic, t))
                    ti["count"] += 1
                    ti["t_start"] = min(ti["t_start"], t)
                    ti["t_end"]   = max(ti["t_end"],   t)

                    w = writers.get(topic)
                    if w is None:
                        w = writers[topic] = _TopicWriter(out_dir, topic, msg_type)

                    if msg_type in SKIP_TYPES:
                        w.timestamp_only(t)
                        continue

                    raw = {}
                    try:
                        raw = _decoded_to_dict(decoded)
                    except Exception:
                        pass

                    img_bytes = None
                    if "Image" in msg_type:
                        img_bytes = _decode_image_bytes(decoded, msg_type)

                    w.add(t, raw, img_bytes)
    finally:
        for w in writers.values():
            w.close()

    _write_index(out_dir, topic_info, t_start or 0, t_end or 0)


# ---------------------------------------------------------------------------
# SQLite (.db3)
# ---------------------------------------------------------------------------

def _preprocess_sqlite(bag_path: Path, out_dir: Path) -> None:
    import sqlite3

    topic_info: dict = {}
    writers: dict[str, _TopicWriter] = {}
    msg_classes: dict[str, Any] = {}
    t_start = t_end = None

    try:
        for db_file in sorted(bag_path.glob("*.db3")):
            session.set_progress(f"Processing {db_file.name}…")
            conn = sqlite3.connect(str(db_file))
            cur = conn.cursor()

            cur.execute("SELECT id, name, type FROM topics")
            topics_map = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

            cur.execute("SELECT topic_id, timestamp, data FROM messages ORDER BY timestamp")
            for topic_id, ts_ns, data in cur:
                if topic_id not in topics_map:
                    continue
                topic_name, msg_type = topics_map[topic_id]
                t = ts_ns / 1e9

                t_start = t if t_start is None else min(t_start, t)
                t_end   = t if t_end   is None else max(t_end,   t)

                ti = topic_info.setdefault(topic_name, _new_topic_info(msg_type, topic_name, t))
                ti["count"] += 1
                ti["t_start"] = min(ti["t_start"], t)
                ti["t_end"]   = max(ti["t_end"], t)

                w = writers.get(topic_name)
                if w is None:
                    w = writers[topic_name] = _TopicWriter(out_dir, topic_name, msg_type)

                if msg_type in SKIP_TYPES:
                    w.timestamp_only(t)
                    continue

                raw = {}
                img_bytes = None
                try:
                    from rclpy.serialization import deserialize_message
                    if msg_type not in msg_classes:
                        from rosidl_runtime_py.utilities import get_message
                        msg_classes[msg_type] = get_message(msg_type)
                    msg = deserialize_message(bytes(data), msg_classes[msg_type])
                    raw = _decoded_to_dict(msg)
                    if "Image" in msg_type:
                        img_bytes = _decode_image_bytes(msg, msg_type)
                except Exception:
                    pass

                w.add(t, raw, img_bytes)

            conn.close()
    finally:
        for w in writers.values():
            w.close()

    _write_index(out_dir, topic_info, t_start or 0, t_end or 0)


def _numeric_summary(raw: Any, prefix: str = "", out: Optional[dict] = None,
                     depth: int = 0) -> dict:
    """Flatten numeric fields of a decoded message into dotted top-level keys
    (e.g. wrench.force.x). Header timestamps are skipped — they'd pollute
    every chart with epoch-sized values."""
    if out is None:
        out = {}
    if depth > 4 or len(out) >= 24:
        return out
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k.startswith("_") or k == "header":
                continue
            _numeric_summary(v, f"{prefix}{k}." if isinstance(v, dict) else f"{prefix}{k}",
                             out, depth + 1)
    elif isinstance(raw, bool):
        out[prefix.rstrip(".") or "value"] = float(raw)
    elif isinstance(raw, (int, float)):
        out[prefix.rstrip(".") or "value"] = raw
    elif isinstance(raw, list) and raw and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in raw[:8]):
        out[prefix.rstrip(".") or "value"] = raw[:64]
    return out


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------

def _decoded_to_dict(msg: Any) -> dict:
    def _conv(v: Any, d: int = 0) -> Any:
        if d > 5:
            return str(v)
        if hasattr(v, "get_fields_and_field_types"):
            return {f: _conv(getattr(v, f), d + 1) for f in v.get_fields_and_field_types()}
        if hasattr(v, "__slots__") and not isinstance(v, (str, bytes)):
            # mcap_ros2 decoded messages expose __slots__, not the rclpy API
            return {s.lstrip("_"): _conv(getattr(v, s), d + 1) for s in v.__slots__}
        if isinstance(v, (list, tuple)):
            return [_conv(x, d + 1) for x in v][:256]
        if isinstance(v, bytes):
            return list(v[:64])
        try:
            json.dumps(v)
            return v
        except (TypeError, ValueError):
            return str(v)

    result = _conv(msg)
    return result if isinstance(result, dict) else {"_value": result}


def _decode_image_bytes(msg: Any, msg_type: str) -> Optional[bytes]:
    """JPEG bytes for an Image/CompressedImage message. CompressedImage
    frames that are already JPEG are passed through untouched — decoding and
    re-encoding them dominated indexing time on camera-heavy bags. Returns
    None if the OpenCV / cv_bridge stack isn't available (dashboard degrades
    gracefully)."""
    try:
        if "Compressed" in msg_type:
            fmt = str(getattr(msg, "format", "")).lower()
            if "jpeg" in fmt or "jpg" in fmt:
                return bytes(msg.data)
        import cv2
        import numpy as np
        if "Compressed" in msg_type:
            arr = np.frombuffer(bytes(msg.data), dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            from cv_bridge import CvBridge
            frame = CvBridge().imgmsg_to_cv2(msg, desired_encoding="bgr8")
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else None
    except Exception:
        return None
