"""
Wraps `ros2 bag play` as a managed subprocess.

Used to replay a recorded bag onto the live ROS graph (e.g. so other nodes,
or the live-capture view, can consume it). Mirrors the Recorder's design:
single module-level instance, process group signalling on stop.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import RECORDINGS_DIR


@dataclass
class PlaybackState:
    process: Optional[asyncio.subprocess.Process] = None
    bag_name: Optional[str] = None
    rate: float = 1.0
    loop: bool = False
    started_at: Optional[datetime] = None

    @property
    def is_active(self) -> bool:
        return self.process is not None and self.process.returncode is None


class Player:
    def __init__(self) -> None:
        self._state = PlaybackState()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> PlaybackState:
        return self._state

    async def start(self, name: str, rate: float = 1.0, loop: bool = False) -> PlaybackState:
        async with self._lock:
            if self._state.is_active:
                raise RuntimeError("A playback is already in progress.")

            if rate <= 0:
                raise ValueError("Playback rate must be positive.")

            bag_path = (RECORDINGS_DIR / name).resolve()
            # Refuse names that escape the recordings dir (e.g. "../../etc")
            if RECORDINGS_DIR.resolve() not in bag_path.parents:
                raise ValueError("Invalid bag name.")
            if not bag_path.exists():
                raise FileNotFoundError(f"Recording {name!r} not found.")

            if shutil.which("ros2") is None:
                raise RuntimeError(
                    "`ros2` not found on PATH. Did you source /opt/ros/jazzy/setup.bash "
                    "before launching uvicorn?"
                )

            cmd = ["ros2", "bag", "play", str(bag_path), "--rate", str(rate)]
            if loop:
                cmd.append("--loop")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )

            self._state = PlaybackState(
                process=proc,
                bag_name=name,
                rate=rate,
                loop=loop,
                started_at=datetime.now(),
            )
            return self._state

    async def stop(self, timeout: float = 5.0) -> PlaybackState:
        async with self._lock:
            if not self._state.is_active:
                raise RuntimeError("No playback in progress.")

            proc = self._state.process
            assert proc is not None

            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            except ProcessLookupError:
                pass

            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                await proc.wait()

            finished = self._state
            self._state = PlaybackState()
            return finished


player = Player()
