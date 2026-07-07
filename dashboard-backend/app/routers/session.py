"""
Endpoints backing the dashboard UI itself: the pre-processed topic archive
(index / time-windowed data / image frames), bag upload, and the annotations
of the current session.

Annotations are persisted as `annotations.json` next to the bag's .mcap and
metadata.yaml, so they always travel with the recording.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.config import RECORDINGS_DIR, SESSION_OUT_DIR
from app.services import bag_indexer
from app.services.session_state import session

router = APIRouter(tags=["session"])


# ---------------------------------------------------------------------------
# Bag upload → background pre-processing
# ---------------------------------------------------------------------------

@router.post("/playback/upload")
async def playback_upload(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    stem = Path(file.filename).name
    for ext in (".zip", ".tar.gz", ".tgz", ".mcap", ".db3"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    if not stem:
        raise HTTPException(400, "Invalid file name")

    bag_dir = RECORDINGS_DIR / stem
    bag_dir.mkdir(parents=True, exist_ok=True)
    dest = bag_dir / Path(file.filename).name
    with dest.open("wb") as buf:
        shutil.copyfileobj(file.file, buf)

    if dest.name.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(dest, "r") as zf:
            zf.extractall(bag_dir)
        dest.unlink()
        bag_dir = bag_indexer.find_bag_root(bag_dir)
    elif dest.name.endswith((".tar.gz", ".tgz")):
        import tarfile
        with tarfile.open(dest, "r:gz") as tf:
            tf.extractall(bag_dir)
        dest.unlink()
        bag_dir = bag_indexer.find_bag_root(bag_dir)

    session.set_bag(bag_dir)
    session.set_status("processing", "Queued…")
    background_tasks.add_task(bag_indexer.preprocess_bag, bag_dir, SESSION_OUT_DIR)
    return {"status": "processing", "path": str(bag_dir)}


@router.get("/playback/status")
def playback_status():
    return session.status


@router.post("/playback/stop")
async def playback_stop():
    """Close the playback session (stops a live `ros2 bag play` if one runs)."""
    from app.services.player import player
    if player.state.is_active:
        try:
            await player.stop()
        except RuntimeError:
            pass
    session.set_status("idle")
    return {"status": "stopped"}


# ---------------------------------------------------------------------------
# Pre-processed topic archive (served from SESSION_OUT_DIR)
# ---------------------------------------------------------------------------

@router.get("/topics/index")
def topics_index():
    p = SESSION_OUT_DIR / "index.json"
    if not p.exists():
        return JSONResponse({"timestamp": 0, "t_start": 0, "t_end": 0, "topics": []})
    return JSONResponse(json.loads(p.read_text()))


@router.get("/topics/data/{slug}")
def topic_data(slug: str, t: Optional[float] = None, window: float = 10.0):
    """
    With t: entries from data.jsonl within [t - window, t + window/10].
    Without t: the latest.json snapshot.
    """
    tdir = _topic_dir(slug)

    if t is not None:
        jsonl = tdir / "data.jsonl"
        if not jsonl.exists():
            raise HTTPException(404, "No JSONL data")
        lo, hi = t - window, t + window / 10
        entries = []
        with jsonl.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if lo <= e.get("t", 0) <= hi:
                    entries.append(e)
        return JSONResponse({"slug": slug, "t": t, "window": window, "entries": entries})

    latest = tdir / "latest.json"
    if not latest.exists():
        raise HTTPException(404, "No latest data")
    return JSONResponse(json.loads(latest.read_text()))


@router.get("/topics/image/{slug}")
def topic_image(slug: str, t: Optional[float] = None):
    tdir = _topic_dir(slug)

    if t is not None:
        frames = [f for f in tdir.glob("*.jpg") if f.stem != "latest"]
        if frames:
            best = min(frames, key=lambda f: abs(float(f.stem) - t))
            return FileResponse(str(best), media_type="image/jpeg")

    latest = tdir / "latest.jpg"
    if not latest.exists():
        raise HTTPException(404, "No image")
    return FileResponse(str(latest), media_type="image/jpeg")


@router.get("/session/range")
def session_range():
    p = SESSION_OUT_DIR / "index.json"
    if not p.exists():
        return {"t_start": 0, "t_end": 0}
    d = json.loads(p.read_text())
    return {"t_start": d.get("t_start", 0), "t_end": d.get("t_end", 0)}


def _topic_dir(slug: str) -> Path:
    tdir = (SESSION_OUT_DIR / slug).resolve()
    if SESSION_OUT_DIR.resolve() not in tdir.parents or not tdir.exists():
        raise HTTPException(404, f"No data for topic: {slug}")
    return tdir


# ---------------------------------------------------------------------------
# Annotations of the current session
# ---------------------------------------------------------------------------

@router.get("/session/annotations")
def get_annotations():
    return session.load_annotations()


@router.post("/session/annotations")
def save_annotations(annotations: list[dict]):
    try:
        path = session.save_annotations(annotations)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "path": str(path)}
