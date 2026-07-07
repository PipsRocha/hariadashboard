from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.player import player


router = APIRouter(prefix="/playback", tags=["playback"])


class PlayRequest(BaseModel):
    name: str = Field(..., description="Bag folder name under recordings/")
    rate: float = Field(1.0, gt=0, le=10.0)
    loop: bool = False


class PlaybackStatusResponse(BaseModel):
    active: bool
    bag_name: Optional[str] = None
    rate: float = 1.0
    loop: bool = False
    started_at: Optional[datetime] = None


def _to_response(s) -> PlaybackStatusResponse:
    return PlaybackStatusResponse(
        active=s.is_active,
        bag_name=s.bag_name,
        rate=s.rate,
        loop=s.loop,
        started_at=s.started_at,
    )


@router.get("/status", response_model=PlaybackStatusResponse)
async def status():
    return _to_response(player.state)


@router.post("/start", response_model=PlaybackStatusResponse)
async def start(req: PlayRequest):
    try:
        s = await player.start(req.name, rate=req.rate, loop=req.loop)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_response(s)


@router.post("/stop", response_model=PlaybackStatusResponse)
async def stop():
    try:
        s = await player.stop()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_response(s)