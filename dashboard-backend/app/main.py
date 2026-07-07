from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ros_manager import ros_manager
from app.routers import health, topics, ws_demo
from app.routers import recordings
from app.services.recorder import recorder

from app.routers import recordings, playback
from app.services.recorder import recorder
from app.services.player import player


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    ros_manager.start()
    yield
    # Shutdown
    if recorder.state.is_active:
        try:
            await recorder.stop()
        except Exception:
            pass
    if player.state.is_active:
        try:
            await player.stop()
        except Exception:
            pass
    ros_manager.shutdown()



app = FastAPI(title="HARIA Dashboard API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(topics.router)
app.include_router(ws_demo.router)

app.include_router(recordings.router)
app.include_router(playback.router)



@app.get("/")
def root():
    return {"status": "ok", "service": "HARIAdashboard"}