"""Legacy live-recording dashboard, retained outside the curator runtime."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import FRONTEND_DIR
from app.ros_manager import ros_manager
from app.routers import health, playback, recordings, session, topics, ws_demo
from app.services import live_capture
from app.services.player import player
from app.services.recorder import recorder


@asynccontextmanager
async def lifespan(app: FastAPI):
    ros_manager.start()
    yield
    live_capture.stop()
    if recorder.state.is_active:
        try: await recorder.stop()
        except Exception: pass
    if player.state.is_active:
        try: await player.stop()
        except Exception: pass
    ros_manager.shutdown()


app = FastAPI(title="HARIA Legacy Dashboard API", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(health.router)
app.include_router(topics.router)
app.include_router(ws_demo.router)
app.include_router(recordings.router)
app.include_router(playback.router)
app.include_router(session.router)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def serve_ui():
    return FileResponse(FRONTEND_DIR / "index.html")
