import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.services.recorder import RECORDINGS_DIR
from app.services.bag_reader import stream_messages

router = APIRouter(tags=["ws"])

@router.websocket("/ws/playback/{name}")
async def playback(ws: WebSocket, name: str):
    await ws.accept()

    bag_path = RECORDINGS_DIR / name
    if not bag_path.exists():
        await ws.send_json({"error": f"Recording {name!r} not found"})
        await ws.close()
        return

    stop_event = asyncio.Event()
    stream_task = None

    async def run_stream(start_ns: int, speed: float, topics: list[str]):
        async for msg in stream_messages(
            bag_path, topics, start_ns,
            speed=speed, stop_event=stop_event
        ):
            await ws.send_json(msg)

    try:
        # Wait for the frontend to send initial config:
        # { "topics": [...], "start_ns": 0, "speed": 1.0 }
        raw = await ws.receive_text()
        config = json.loads(raw)

        topics   = config.get("topics", [])
        start_ns = config.get("start_ns", 0)
        speed    = config.get("speed", 1.0)

        stream_task = asyncio.create_task(
            run_stream(start_ns, speed, topics)
        )

        # Listen for control messages while streaming
        while True:
            raw = await ws.receive_text()
            cmd = json.loads(raw)

            if cmd.get("action") == "seek":
                # Cancel current stream, restart from new position
                stop_event.set()
                if stream_task:
                    await stream_task
                stop_event.clear()
                start_ns = cmd["timestamp_ns"]
                speed    = cmd.get("speed", speed)
                topics   = cmd.get("topics", topics)
                stream_task = asyncio.create_task(
                    run_stream(start_ns, speed, topics)
                )

            elif cmd.get("action") == "pause":
                stop_event.set()
                if stream_task:
                    await stream_task

            elif cmd.get("action") == "resume":
                stop_event.clear()
                stream_task = asyncio.create_task(
                    run_stream(start_ns, speed, topics)
                )

            elif cmd.get("action") == "speed":
                stop_event.set()
                if stream_task:
                    await stream_task
                stop_event.clear()
                speed = cmd["value"]
                stream_task = asyncio.create_task(
                    run_stream(start_ns, speed, topics)
                )

    except WebSocketDisconnect:
        stop_event.set()
        if stream_task:
            stream_task.cancel()