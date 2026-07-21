from __future__ import annotations

import bisect
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from hri_curator.config import layout, load_profile
from hri_curator.database import Database
from hri_curator.eventlog import write_event
from hri_curator.paths import safe_join

PREVIEW_SCHEMA_VERSION = 2

_jobs: dict[str, dict[str, Any]] = {}
_jobs_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def preview_state(root: str | Path, trial_uid: str) -> dict[str, Any]:
    paths = layout(root)
    _require_trial(paths.database, trial_uid)
    payload = _ready_payload(paths.root, trial_uid)
    if payload is not None:
        return payload
    with _jobs_guard:
        job = _jobs.get(_job_key(paths.root, trial_uid))
        return dict(job) if job else {"status": "missing", "trial_uid": trial_uid, "progress": 0.0}


def start_preview(root: str | Path, trial_uid: str) -> dict[str, Any]:
    paths = layout(root)
    _require_trial(paths.database, trial_uid)
    ready = _ready_payload(paths.root, trial_uid)
    if ready is not None:
        return ready
    key = _job_key(paths.root, trial_uid)
    with _jobs_guard:
        existing = _jobs.get(key)
        if existing and existing["status"] == "preparing":
            return dict(existing)
        _jobs[key] = {"status": "preparing", "trial_uid": trial_uid, "progress": 0.0}
    threading.Thread(target=_worker, args=(paths.root, trial_uid), daemon=True).start()
    return dict(_jobs[key])


def prepare_preview(root: str | Path, trial_uid: str) -> dict[str, Any]:
    """Build a preview synchronously; primarily useful for tests and maintenance."""
    paths = layout(root)
    _require_trial(paths.database, trial_uid)
    ready = _ready_payload(paths.root, trial_uid)
    return ready if ready is not None else _build_preview(paths.root, trial_uid)


def invalidate_preview(root: str | Path, trial_uid: str) -> None:
    paths = layout(root)
    for candidate in (paths.cache / trial_uid, paths.cache / f".{trial_uid}.tmp"):
        if candidate.exists():
            shutil.rmtree(candidate)
    with _jobs_guard:
        _jobs.pop(_job_key(paths.root, trial_uid), None)


def load_preview(root: str | Path, trial_uid: str) -> dict[str, Any] | None:
    payload = _ready_payload(layout(root).root, trial_uid)
    return payload if payload and payload.get("status") == "ready" else None


def nearest_frame(root: str | Path, trial_uid: str, topic_key: str, t_ns: int) -> Path:
    payload = load_preview(root, trial_uid)
    if not payload or topic_key not in payload["streams"]: raise KeyError(topic_key)
    frames = payload["streams"][topic_key]["frames"]
    if not frames: raise FileNotFoundError(topic_key)
    times = [item["t_ns"] for item in frames]
    pos = bisect.bisect_left(times, t_ns)
    if pos >= len(times): pos = len(times) - 1
    elif pos > 0 and abs(times[pos - 1] - t_ns) <= abs(times[pos] - t_ns): pos -= 1
    filename = frames[pos]["file"]
    if Path(filename).name != filename: raise ValueError("Invalid cached frame name")
    return layout(root).cache / trial_uid / topic_key / filename


def clean_cache(root: str | Path) -> None:
    paths = layout(root)
    if paths.cache.exists(): shutil.rmtree(paths.cache)
    paths.cache.mkdir(parents=True, exist_ok=True)
    with _jobs_guard:
        prefix = str(paths.root) + "::"
        for key in [key for key in _jobs if key.startswith(prefix)]: _jobs.pop(key, None)


def _worker(root: Path, trial_uid: str) -> None:
    key = _job_key(root, trial_uid)
    try:
        payload = _build_preview(root, trial_uid, key)
        with _jobs_guard: _jobs[key] = payload
    except Exception as exc:
        write_event(root, "preview_failed", trial_uid=trial_uid, error=str(exc))
        with _jobs_guard:
            _jobs[key] = {
                "status": "failed", "trial_uid": trial_uid, "progress": 0.0,
                "error": str(exc), "retryable": True,
            }


def _build_preview(root: Path, trial_uid: str, job_key: str | None = None) -> dict[str, Any]:
    paths = layout(root)
    lock = _trial_lock(_job_key(paths.root, trial_uid))
    with lock:
        ready = _ready_payload(paths.root, trial_uid)
        if ready is not None: return ready
        db = Database(paths.database)
        trial = db.row("SELECT * FROM trials WHERE trial_uid=?", (trial_uid,))
        if not trial: raise KeyError(trial_uid)
        if not trial["relative_trial_path"] or trial["starting_time_ns"] is None:
            raise ValueError("Trial has no readable deep-scanned bag")
        trial_path = safe_join(paths.root, trial["relative_trial_path"])
        profile = load_profile(paths.root)
        rgb = {key: rule.topic for key, rule in profile.required_topics.items()
               if key in ("camera1_rgb", "camera2_rgb")}
        try:
            import rosbag2_py
            from rclpy.serialization import deserialize_message
            from sensor_msgs.msg import CompressedImage
        except ImportError as exc:
            raise RuntimeError("Preview generation requires the ROS 2 Jazzy container") from exc

        target = paths.cache / trial_uid
        temporary = paths.cache / f".{trial_uid}.tmp"
        if temporary.exists(): shutil.rmtree(temporary)
        temporary.mkdir(parents=True, exist_ok=True)
        streams = {key: {"topic": topic, "frames": []} for key, topic in rgb.items()}
        by_topic = {topic: key for key, topic in rgb.items()}
        counts = db.rows("SELECT topic_name,message_count FROM topics WHERE trial_uid=?", (trial_uid,))
        expected = sum(row["message_count"] for row in counts if row["topic_name"] in by_topic) or 1
        processed = 0
        try:
            reader = rosbag2_py.SequentialReader()
            reader.open(rosbag2_py.StorageOptions(uri=str(trial_path), storage_id="mcap"),
                        rosbag2_py.ConverterOptions("", ""))
            reader.set_filter(rosbag2_py.StorageFilter(topics=sorted(by_topic)))
            while reader.has_next():
                topic, raw, timestamp = reader.read_next()
                key = by_topic.get(topic)
                if not key: continue
                msg = deserialize_message(raw, CompressedImage)
                extension = _extension(msg.format)
                stream_dir = temporary / key
                stream_dir.mkdir(exist_ok=True)
                filename = f"{timestamp}.{extension}"
                (stream_dir / filename).write_bytes(bytes(msg.data))
                streams[key]["frames"].append({"t_ns": timestamp - trial["starting_time_ns"], "file": filename})
                processed += 1
                if job_key and processed % 50 == 0:
                    with _jobs_guard:
                        if job_key in _jobs: _jobs[job_key]["progress"] = min(0.99, processed / expected)
            fingerprint, topics = _cache_identity(db, trial_uid, profile)
            payload = {
                "status": "ready", "progress": 1.0, "trial_uid": trial_uid,
                "preview_schema_version": PREVIEW_SCHEMA_VERSION, "source_fingerprint": fingerprint,
                "selected_topics": topics,
                "duration_ns": int((trial["duration_sec"] or 0) * 1e9), "streams": streams,
                "phase_intervals": db.rows("SELECT phase,start_ns,end_ns FROM phase_intervals WHERE trial_uid=? ORDER BY start_ns", (trial_uid,)),
                "availability": db.rows("SELECT topic_key,stream_status,first_message_offset_sec,last_message_offset_sec FROM topic_qc WHERE trial_uid=?", (trial_uid,)),
            }
            (temporary / "index.json").write_text(json.dumps(payload), encoding="utf-8")
            if target.exists(): shutil.rmtree(target)
            temporary.replace(target)
            _enforce_cache_limit(paths.cache, target)
            return payload
        except Exception:
            if temporary.exists(): shutil.rmtree(temporary)
            raise


def _ready_payload(root: Path, trial_uid: str) -> dict[str, Any] | None:
    paths = layout(root)
    index = paths.cache / trial_uid / "index.json"
    if not index.exists(): return None
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
        db = Database(paths.database)
        profile = load_profile(paths.root)
        fingerprint, topics = _cache_identity(db, trial_uid, profile)
        if (payload.get("preview_schema_version") != PREVIEW_SCHEMA_VERSION or
                payload.get("source_fingerprint") != fingerprint or
                payload.get("selected_topics") != topics):
            shutil.rmtree(index.parent)
            return None
        index.touch()
        payload["status"] = "ready"
        return payload
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        if index.parent.exists(): shutil.rmtree(index.parent)
        return None


def _cache_identity(db: Database, trial_uid: str, profile: Any) -> tuple[str, dict[str, str]]:
    row = db.row("SELECT fingerprint_sha256 FROM trial_fingerprints WHERE trial_uid=?", (trial_uid,))
    if not row: raise KeyError(trial_uid)
    topics = {key: profile.required_topics[key].topic for key in ("camera1_rgb", "camera2_rgb")}
    return row["fingerprint_sha256"], topics


def _require_trial(database: Path, trial_uid: str) -> None:
    if not Database(database).row("SELECT 1 FROM trials WHERE trial_uid=?", (trial_uid,)):
        raise KeyError(trial_uid)


def _extension(value: str) -> str:
    return "png" if "png" in value.lower() else "jpg"


def _job_key(root: Path, uid: str) -> str:
    return f"{root}::{uid}"


def _trial_lock(key: str) -> threading.Lock:
    with _jobs_guard:
        return _locks.setdefault(key, threading.Lock())


def _enforce_cache_limit(cache: Path, current: Path) -> None:
    limit = int(float(os.environ.get("HRI_CURATOR_CACHE_MAX_GB", "20")) * 1024 ** 3)
    entries: list[tuple[float, Path, int]] = []
    total = 0
    for directory in cache.iterdir():
        if not directory.is_dir() or directory.name.startswith("."): continue
        size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
        index = directory / "index.json"
        stamp = index.stat().st_mtime if index.exists() else directory.stat().st_mtime
        entries.append((stamp, directory, size)); total += size
    for _, directory, size in sorted(entries):
        if total <= limit: break
        if directory == current: continue
        shutil.rmtree(directory); total -= size
