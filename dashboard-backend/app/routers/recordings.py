from datetime import datetime
from typing import Optional
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import RECORDINGS_DIR, SESSION_OUT_DIR
from app.services.recorder import recorder, list_bags
from app.services.bag_reader import get_bag_info
from app.services.session_state import session
from app.services import live_capture

router = APIRouter(prefix="/recordings", tags=["recordings"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class StartRequest(BaseModel):
    topics: list[str] = Field(default_factory=list,
                              description="Topics to record; empty = record all",
                              examples=[["/chatter", "/tf"]])
    name: Optional[str] = Field(None, description="Optional bag folder name")


class StatusResponse(BaseModel):
    active: bool
    bag_path: Optional[str] = None
    topics: list[str] = []
    started_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Recorder control
# ---------------------------------------------------------------------------

@router.get("/status", response_model=StatusResponse)
async def status():
    s = recorder.state
    return StatusResponse(
        active=s.is_active,
        bag_path=str(s.bag_path) if s.bag_path else None,
        topics=s.topics,
        started_at=s.started_at,
    )


@router.post("/start", response_model=StatusResponse)
async def start(req: StartRequest):
    try:
        s = await recorder.start(req.topics, name=req.name)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Mirror the live graph into the session archive so the dashboard can
    # visualise the session while it's being recorded.
    session.set_bag(s.bag_path)
    live_capture.start(SESSION_OUT_DIR)
    return StatusResponse(
        active=s.is_active,
        bag_path=str(s.bag_path) if s.bag_path else None,
        topics=s.topics,
        started_at=s.started_at,
    )


@router.post("/stop", response_model=StatusResponse)
async def stop():
    live_capture.stop()
    try:
        s = await recorder.stop()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return StatusResponse(
        active=False,
        bag_path=str(s.bag_path) if s.bag_path else None,
        topics=s.topics,
        started_at=s.started_at,
    )


# ---------------------------------------------------------------------------
# Listing & metadata
# ---------------------------------------------------------------------------

@router.get("")
def list_recordings():
    return list_bags()


@router.get("/annotations")
def all_annotations():
    """Flat list of every annotation across all recordings, for the
    annotations explorer (filter/count by name across sessions)."""
    out = []
    for entry in sorted(RECORDINGS_DIR.iterdir()):
        ann_file = entry / "annotations.json"
        if not entry.is_dir() or not ann_file.exists():
            continue
        try:
            anns = json.loads(ann_file.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(anns, list):
            continue
        from app.services.recorder import _read_bag_times
        times = _read_bag_times(entry)
        for a in anns:
            if not isinstance(a, dict):
                continue
            out.append({
                "recording": entry.name,
                "recording_start_ns": times.get("start_time_ns"),
                "id":   a.get("id"),
                "name": a.get("name") or a.get("label") or a.get("category") or "unnamed",
                "t1":   a.get("t1"),
                "t2":   a.get("t2"),
            })
    return out


@router.get("/{name}/info")
def recording_info(name: str):
    bag_path = RECORDINGS_DIR / name
    if not bag_path.exists():
        raise HTTPException(status_code=404, detail=f"Recording {name!r} not found")
    info = get_bag_info(bag_path)
    return {
        "start_time_ns": info.start_time_ns,
        "end_time_ns":   info.end_time_ns,
        "duration_ns":   info.duration_ns,
        "topics": [
            {
                "name":          t.name,
                "msg_type":      t.msg_type,
                "message_count": t.message_count,
            }
            for t in info.topics
        ],
    }


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

@router.get("/{name}/annotations")
def get_annotations(name: str):
    ann_file = RECORDINGS_DIR / name / "annotations.json"
    if not ann_file.exists():
        return []
    return json.loads(ann_file.read_text())


@router.post("/{name}/annotations")
def save_annotations(name: str, annotations: list[dict]):
    bag_path = RECORDINGS_DIR / name
    if not bag_path.exists():
        raise HTTPException(status_code=404, detail=f"Recording {name!r} not found")
    ann_file = bag_path / "annotations.json"
    ann_file.write_text(json.dumps(annotations, indent=2))
    return {"ok": True}