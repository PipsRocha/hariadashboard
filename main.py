"""
HARIA Failure Dashboard — FastAPI Backend
-----------------------------------------
Handles recording, bag upload + pre-processing, and time-windowed
data queries for the frontend.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from cv_bridge import CvBridge


from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="HARIA Failure Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR              = Path(__file__).parent
ROS_SETUP             = "/opt/ros/jazzy/setup.bash"
UPLOAD_DIR            = Path("/home/pips/bags")
OUT_DIR               = BASE_DIR / "dashboard" / "out"
DASHBOARD_NODE_SCRIPT = BASE_DIR / "dashboard_node.py"
ROS_PYTHON            = "/usr/bin/python3"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Process handles
# ---------------------------------------------------------------------------

_bag_proc:  Optional[subprocess.Popen] = None
_node_proc: Optional[subprocess.Popen] = None
_process_status: dict = {"state": "idle", "progress": "", "error": ""}


def _ros_cmd(cmd: str) -> str:
    return f"source {ROS_SETUP} && {cmd}"


def _start_node() -> subprocess.Popen:
    cmd = _ros_cmd(f"{ROS_PYTHON} {DASHBOARD_NODE_SCRIPT} --out-dir {OUT_DIR}")
    return subprocess.Popen(cmd, shell=True, executable="/bin/bash")


def _kill(proc: Optional[subprocess.Popen]) -> None:
    if proc and proc.poll() is None:
        proc.kill(); proc.wait()


def _stop_all() -> None:
    global _bag_proc, _node_proc
    _kill(_bag_proc); _kill(_node_proc)
    _bag_proc = _node_proc = None


def _find_bag_root(base: Path) -> Path:
    for m in sorted(base.rglob("metadata.yaml")):
        return m.parent
    return base


def _storage_flag(bag_path: Path) -> str:
    meta = bag_path / "metadata.yaml"
    if meta.exists():
        txt = meta.read_text()
        if "mcap" in txt:   return "--storage mcap"
        if "sqlite3" in txt: return "--storage sqlite3"
    return ""


# ---------------------------------------------------------------------------
# Bag pre-processing (JSONL extraction)
# ---------------------------------------------------------------------------

def _preprocess_bag(bag_path: Path, out_dir: Path) -> None:
    """
    Read a ROS 2 bag and write per-topic JSONL files to out_dir.
    Uses the mcap or sqlite3 Python readers depending on bag format.
    Falls back to running ros2 bag play + dashboard_node if readers unavailable.
    """
    global _process_status
    _process_status = {"state": "processing", "progress": "Reading bag metadata…", "error": ""}

    meta_file = bag_path / "metadata.yaml"
    if not meta_file.exists():
        _process_status = {"state": "error", "progress": "", "error": "No metadata.yaml found"}
        return

    import yaml
    meta_raw = yaml.safe_load(meta_file.read_text())
    # yaml may return a string if the file has unexpected structure; normalise to dict
    if not isinstance(meta_raw, dict):
        meta_raw = {}
    meta = meta_raw
    # ROS 2 Humble+ puts storage_identifier nested; older versions put it at top level
    bag_info = meta.get("rosbag2_bagfile_information", meta)
    if not isinstance(bag_info, dict):
        bag_info = {}
    storage_id = bag_info.get("storage_identifier", "")
    # Also check file extension as fallback
    if not storage_id:
        mcap_files = list(bag_path.glob("*.mcap"))
        db3_files  = list(bag_path.glob("*.db3"))
        storage_id = "mcap" if mcap_files else "sqlite3"

    try:
        if storage_id == "mcap":
            _preprocess_mcap(bag_path, out_dir, meta)
        else:
            _preprocess_sqlite(bag_path, out_dir, meta)
        _process_status = {"state": "ready", "progress": "Done", "error": ""}
    except Exception as e:
        import traceback
        _process_status = {"state": "error", "progress": "", "error": traceback.format_exc()}


def _write_topic_entry(out_dir: Path, topic_slug: str, topic_name: str,
                        msg_type: str, t: float, raw: dict,
                        is_image: bool = False, img_bytes: Optional[bytes] = None) -> None:
    import re as _re

    def _slug(s): return _re.sub(r"[^a-zA-Z0-9]", "_", s).strip("_")

    slug = _slug(topic_name)
    tdir = out_dir / slug
    tdir.mkdir(parents=True, exist_ok=True)

    if is_image and img_bytes:
        frame_path = tdir / f"{t:.3f}.jpg"
        frame_path.write_bytes(img_bytes)
        # also update latest
        (tdir / "latest.jpg").write_bytes(img_bytes)
        entry = {"t": t, "type": "image", "frame": f"{t:.3f}.jpg"}
    else:
        # raw may be a string if deserialisation failed — normalise to dict
        if not isinstance(raw, dict):
            raw = {"_raw_str": str(raw)}
        entry = {"t": t, "_raw": raw}
        # extract numeric fields
        if "JointState" in msg_type and isinstance(raw, dict) and "name" in raw:
            entry["__names"]  = raw.get("name", [])
            entry["position"] = raw.get("position", [])
            entry["velocity"] = raw.get("velocity", [])
            entry["effort"]   = raw.get("effort", [])
        (tdir / "latest.json").write_text(
            json.dumps({"t": t, "topic": topic_name, "msg_type": msg_type, **entry}), encoding="utf-8"
        )

    with (tdir / "data.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _preprocess_mcap(bag_path: Path, out_dir: Path, meta: dict) -> None:
    global _process_status
    from mcap.reader import make_reader
    from mcap_ros2.decoder import DecoderFactory
    import re as _re

    def _slug(s): return _re.sub(r"[^a-zA-Z0-9]", "_", s).strip("_")

    # Topics too large/useless to deserialise every message
    SKIP_TYPES = {
        "std_msgs/msg/String",          # robot_description URDF — huge
        "moveit_msgs/msg/PlanningScene",
        "moveit_msgs/msg/DisplayTrajectory",
        "moveit_msgs/msg/AttachedCollisionObject",
        "moveit_msgs/msg/CollisionObject",
        "rcl_interfaces/msg/ParameterEvent",
        "rosgraph_msgs/msg/Clock",
        "tf2_msgs/msg/TFMessage",       # write timestamps only, no full decode
    }

    db_files = list(bag_path.glob("*.mcap"))
    if not db_files:
        raise FileNotFoundError("No .mcap files found")

    # --- Pass 1: count total messages for progress ---
    _process_status["progress"] = "Counting messages…"
    total_msgs = 0
    for db_file in db_files:
        with db_file.open("rb") as f:
            r = make_reader(f)
            for _ in r.iter_messages():
                total_msgs += 1

    topic_info: dict = {}
    t_start = t_end = None
    processed = 0
    last_pct = -1

    # --- Pass 2: decode and write ---
    for db_file in db_files:
        with db_file.open("rb") as f:
            reader = make_reader(f, decoder_factories=[DecoderFactory()])
            for schema, channel, message, decoded in reader.iter_decoded_messages():
                processed += 1
                pct = int(processed / max(1, total_msgs) * 100)
                if pct != last_pct:
                    last_pct = pct
                    _process_status["progress"] = f"Processing… {pct}% ({processed}/{total_msgs} msgs)"

                t         = message.log_time / 1e9
                topic     = channel.topic
                msg_type  = schema.name if schema else "unknown"

                if t_start is None or t < t_start: t_start = t
                if t_end   is None or t > t_end:   t_end   = t

                if topic not in topic_info:
                    topic_info[topic] = {
                        "msg_type": msg_type, "slug": _slug(topic), "count": 0,
                        "t_start": t, "t_end": t,
                        "is_image": "Image" in msg_type,
                        "is_num":   _is_num(msg_type),
                        "is_table": "JointState" in msg_type,
                    }
                ti = topic_info[topic]
                ti["count"] += 1
                ti["t_start"] = min(ti["t_start"], t)
                ti["t_end"]   = max(ti["t_end"],   t)

                # Skip expensive deserialisation for known-heavy types
                if msg_type in SKIP_TYPES:
                    # Still record the timestamp in JSONL for timeline coverage
                    tdir = out_dir / _slug(topic)
                    tdir.mkdir(parents=True, exist_ok=True)
                    with (tdir / "data.jsonl").open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"t": t}) + "\n")
                    continue

                raw = {}
                try:
                    raw = _decoded_to_dict(decoded)
                except Exception:
                    pass

                img_bytes = None
                if "Image" in msg_type:
                    img_bytes = _decode_image_bytes(decoded, msg_type)

                _write_topic_entry(out_dir, _slug(topic), topic, msg_type, t, raw,
                                   is_image="Image" in msg_type, img_bytes=img_bytes)

    _write_session_index(out_dir, topic_info, t_start or 0, t_end or 0)


def _preprocess_sqlite(bag_path: Path, out_dir: Path, meta: dict) -> None:
    global _process_status
    import sqlite3 as _sqlite3
    import re as _re
    import struct

    def _slug(s): return _re.sub(r"[^a-zA-Z0-9]", "_", s).strip("_")

    db_files = list(bag_path.glob("*.db3"))
    if not db_files:
        raise FileNotFoundError("No .db3 files found")

    topic_info: dict = {}
    t_start = t_end = None

    for db_file in db_files:
        _process_status["progress"] = f"Processing {db_file.name}…"
        conn = _sqlite3.connect(str(db_file))
        cur  = conn.cursor()

        # Build topic id -> (name, type) map
        cur.execute("SELECT id, name, type FROM topics")
        topics_map = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

        cur.execute("SELECT topic_id, timestamp, data FROM messages ORDER BY timestamp")
        for topic_id, ts_ns, data in cur.fetchall():
            if topic_id not in topics_map:
                continue
            topic_name, msg_type = topics_map[topic_id]
            t = ts_ns / 1e9

            if t_start is None or t < t_start: t_start = t
            if t_end   is None or t > t_end:   t_end   = t

            if topic_name not in topic_info:
                topic_info[topic_name] = {
                    "msg_type": msg_type, "slug": _slug(topic_name), "count": 0,
                    "t_start": t, "t_end": t,
                    "is_image": "Image" in msg_type,
                    "is_num": _is_num(msg_type),
                    "is_table": "JointState" in msg_type,
                }
            ti = topic_info[topic_name]
            ti["count"] += 1
            ti["t_start"] = min(ti["t_start"], t)
            ti["t_end"]   = max(ti["t_end"], t)

            # Deserialise using rclpy
            raw = {}
            img_bytes = None
            try:
                from rclpy.serialization import deserialize_message
                from rosidl_runtime_py.utilities import get_message
                msg_class = get_message(msg_type)
                msg = deserialize_message(bytes(data), msg_class)
                raw = _decoded_to_dict(msg)
                if "Image" in msg_type:
                    img_bytes = _decode_image_bytes(msg, msg_type)
            except Exception:
                pass

            _write_topic_entry(out_dir, _slug(topic_name), topic_name, msg_type, t, raw,
                               is_image="Image" in msg_type, img_bytes=img_bytes)

        conn.close()

    _write_session_index(out_dir, topic_info, t_start or 0, t_end or 0)


def _decoded_to_dict(msg: Any) -> dict:
    import json as _json
    def _conv(v, d=0):
        if d > 5: return str(v)
        if hasattr(v, "get_fields_and_field_types"):
            return {f: _conv(getattr(v, f), d+1) for f in v.get_fields_and_field_types()}
        if isinstance(v, (list, tuple)):
            return [_conv(x, d+1) for x in v][:256]
        if isinstance(v, bytes): return list(v[:64])
        try: _json.dumps(v); return v
        except: return str(v)
    result = _conv(msg)
    # Always return a dict, never a bare string
    if not isinstance(result, dict):
        return {"_value": result}
    return result


def _decode_image_bytes(msg: Any, msg_type: str) -> Optional[bytes]:
    try:
        import numpy as np
        bridge = CvBridge()
        if "Compressed" in msg_type:
            arr   = np.frombuffer(msg.data, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            #from cv_bridge import CvBridge
            frame = bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else None
    except Exception:
        return None


def _is_num(t: str) -> bool:
    NUMERIC = ["Float", "Int", "UInt", "Bool", "JointState", "Imu",
               "Wrench", "Twist", "Pose", "Vector3", "Odometry", "NavSatFix"]
    return any(n in t for n in NUMERIC)


def _write_session_index(out_dir: Path, topic_info: dict, t_start: float, t_end: float) -> None:
    topics = []
    for topic_name, ti in topic_info.items():
        topics.append({
            **ti,
            "topic":    topic_name,
            "active":   False,
            "last_msg": ti.get("t_end", t_end),
        })
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.json").write_text(json.dumps({
        "timestamp": time.time(),
        "t_start": t_start,
        "t_end":   t_end,
        "topics":  topics,
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class RecordRequest(BaseModel):
    output_name: str = "recording"
    topics: Optional[list[str]] = None


class PlayRequest(BaseModel):
    bag_path: str


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_ui():
    f = BASE_DIR / "index.html"
    if not f.exists(): raise HTTPException(404, "index.html not found")
    return HTMLResponse(f.read_text())


@app.get("/static/{filename}", include_in_schema=False)
def serve_static(filename: str):
    f = BASE_DIR / "static" / filename
    if not f.exists(): raise HTTPException(404, f"{filename} not found")
    mt = "text/javascript" if filename.endswith(".js") else \
         "text/css"        if filename.endswith(".css") else "application/octet-stream"
    from fastapi.responses import Response
    return Response(f.read_bytes(), media_type=mt)


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

@app.post("/record/start")
def record_start(req: RecordRequest):
    global _bag_proc, _node_proc
    _stop_all()
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    bag_out = str(UPLOAD_DIR / f"{req.output_name}_{ts}")
    cmd     = f"ros2 bag record -a -o {bag_out}" if not req.topics else \
              f"ros2 bag record -o {bag_out} {' '.join(req.topics)}"
    _bag_proc  = subprocess.Popen(_ros_cmd(cmd), shell=True, executable="/bin/bash")
    _node_proc = _start_node()
    return {"status": "recording", "bag_output": bag_out}


@app.post("/record/stop")
def record_stop():
    _stop_all()
    return {"status": "stopped"}


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

@app.post("/playback/upload")
async def playback_upload(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    global _process_status
    dest = UPLOAD_DIR / file.filename
    with dest.open("wb") as buf:
        shutil.copyfileobj(file.file, buf)

    extracted_path = str(dest)
    if file.filename.endswith(".zip"):
        import zipfile
        extract_to = UPLOAD_DIR / Path(file.filename).stem
        with zipfile.ZipFile(dest, "r") as zf:
            zf.extractall(extract_to)
        extracted_path = str(_find_bag_root(extract_to))
    elif file.filename.endswith((".tar.gz", ".tgz")):
        import tarfile
        extract_to = UPLOAD_DIR / Path(file.filename).stem
        with tarfile.open(dest, "r:gz") as tf:
            tf.extractall(extract_to)
        extracted_path = str(_find_bag_root(extract_to))

    _process_status = {"state": "processing", "progress": "Queued…", "error": ""}
    background_tasks.add_task(_preprocess_bag, Path(extracted_path), OUT_DIR)
    return {"status": "processing", "path": extracted_path}


@app.get("/playback/status")
def playback_status():
    return _process_status


@app.post("/record/start_live_playback")
def start_live_playback(req: PlayRequest):
    """Optional: play a bag through ROS for live DashboardNode capture."""
    global _bag_proc, _node_proc
    bag_path = Path(req.bag_path)
    if not bag_path.exists():
        raise HTTPException(404, f"Bag not found: {req.bag_path}")
    _stop_all()
    flag = _storage_flag(bag_path)
    _bag_proc  = subprocess.Popen(
        _ros_cmd(f"ros2 bag play {req.bag_path} {flag}"),
        shell=True, executable="/bin/bash"
    )
    _node_proc = _start_node()
    return {"status": "playing", "bag_path": req.bag_path}


@app.post("/playback/stop")
def playback_stop():
    _stop_all()
    return {"status": "stopped"}


# ---------------------------------------------------------------------------
# Topic endpoints
# ---------------------------------------------------------------------------

@app.get("/topics/index")
def topics_index():
    p = OUT_DIR / "index.json"
    if not p.exists():
        return JSONResponse({"timestamp": 0, "t_start": 0, "t_end": 0, "topics": []})
    return JSONResponse(json.loads(p.read_text()))


@app.get("/topics/data/{slug}")
def topic_data(slug: str, t: Optional[float] = None, window: float = 10.0):
    """
    If t is given: return entries from data.jsonl within [t-window, t+window/10].
    If no t: return latest.json snapshot.
    """
    tdir = OUT_DIR / slug
    if not tdir.exists():
        raise HTTPException(404, f"No data for slug: {slug}")

    if t is not None:
        jsonl = tdir / "data.jsonl"
        if not jsonl.exists():
            raise HTTPException(404, "No JSONL data")
        lo, hi = t - window, t + window / 10
        entries = []
        with jsonl.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    e = json.loads(line)
                    if lo <= e.get("t", 0) <= hi:
                        entries.append(e)
                except Exception:
                    continue
        return JSONResponse({"slug": slug, "t": t, "window": window, "entries": entries})

    latest = tdir / "latest.json"
    if not latest.exists():
        raise HTTPException(404, "No latest data")
    return JSONResponse(json.loads(latest.read_text()))


@app.get("/topics/image/{slug}")
def topic_image(slug: str, t: Optional[float] = None):
    tdir = OUT_DIR / slug
    if not tdir.exists():
        raise HTTPException(404, f"No image topic: {slug}")

    if t is not None:
        # Find closest frame
        frames = sorted(tdir.glob("*.jpg"))
        frames = [f for f in frames if f.stem not in ("latest",)]
        if frames:
            best = min(frames, key=lambda f: abs(float(f.stem) - t))
            return FileResponse(str(best), media_type="image/jpeg")

    latest = tdir / "latest.jpg"
    if not latest.exists():
        raise HTTPException(404, "No image")
    return FileResponse(str(latest), media_type="image/jpeg")


@app.get("/session/range")
def session_range():
    """Return the global time range of the current session."""
    p = OUT_DIR / "index.json"
    if not p.exists():
        return {"t_start": 0, "t_end": 0}
    d = json.loads(p.read_text())
    return {"t_start": d.get("t_start", 0), "t_end": d.get("t_end", 0)}


# ---------------------------------------------------------------------------
# Status / misc
# ---------------------------------------------------------------------------

@app.get("/status")
def status():
    return {
        "bag_process":    "running" if _bag_proc  and _bag_proc.poll()  is None else "stopped",
        "dashboard_node": "running" if _node_proc and _node_proc.poll() is None else "stopped",
        "process":        _process_status,
    }


@app.get("/bags")
def list_bags():
    return [{"name": p.name, "path": str(p), "is_dir": p.is_dir()}
            for p in sorted(UPLOAD_DIR.iterdir())]


@app.on_event("shutdown")
def on_shutdown():
    _stop_all()