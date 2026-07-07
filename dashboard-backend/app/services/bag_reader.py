"""
Reads .mcap bags for the HARIA dashboard.

Responsibilities:
  - get_topics()       → topic list for the frontend topic selector
  - get_bag_info()     → time range + topic list for timeline initialisation
  - stream_messages()  → async generator consumed by the WebSocket manager

Image handling:
  - sensor_msgs/CompressedImage  → data passed through as-is (already JPEG/PNG)
  - sensor_msgs/Image            → raw pixels JPEG-encoded before sending

All other message types are serialised to JSON via their __slots__ recursively.
The MCAP schema stored in the file drives deserialisation — no per-type hardcoding.
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from mcap_ros2.reader import read_bag          # mcap-ros2-support
from mcap.reader import make_reader            # mcap

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

IMAGE_RAW_TYPE       = "sensor_msgs/msg/Image"
IMAGE_COMPRESSED_TYPE = "sensor_msgs/msg/CompressedImage"


@dataclass
class BagInfo:
    start_time_ns: int
    end_time_ns:   int
    duration_ns:   int
    topics: list[TopicInfo]


@dataclass
class TopicInfo:
    name:          str
    msg_type:      str
    message_count: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_bag_info(bag_path: Path) -> BagInfo:
    """
    Return time range and topic list without streaming any messages.
    Called once when the user opens a recording — populates the timeline
    and the topic selector.
    """
    with open(bag_path / _find_mcap(bag_path), "rb") as f:
        reader = make_reader(f)
        stats    = reader.get_summary().statistics
        channels = reader.get_summary().channels
        schemas  = {s.id: s for s in reader.get_summary().schemas.values()}

        start_ns = stats.message_start_time
        end_ns   = stats.message_end_time

        topics: list[TopicInfo] = []
        for ch in channels.values():
            schema   = schemas.get(ch.schema_id)
            msg_type = schema.name if schema else "unknown"
            count    = stats.channel_message_counts.get(ch.id, 0)
            topics.append(TopicInfo(
                name=ch.topic,
                msg_type=msg_type,
                message_count=count,
            ))

    return BagInfo(
        start_time_ns=start_ns,
        end_time_ns=end_ns,
        duration_ns=end_ns - start_ns,
        topics=topics,
    )


def get_topics(bag_path: Path) -> list[TopicInfo]:
    """Convenience wrapper — just the topic list."""
    return get_bag_info(bag_path).topics


async def stream_messages(
    bag_path:  Path,
    topics:    list[str],
    start_ns:  int,
    speed:     float = 1.0,
    *,
    stop_event: Optional[asyncio.Event] = None,
) -> AsyncGenerator[dict, None]:
    """
    Async generator that yields messages in timestamp order.

    Each yielded dict:
        {
            "topic":        str,
            "timestamp_ns": int,
            "msg_type":     str,
            "data":         dict | str          # JSON-serialisable
            # images only:
            "encoding":     "jpeg",
            "data":         str                 # base64-encoded JPEG bytes
        }

    Timing: real-time gaps between messages are reproduced at `speed` ×.
    Seeking: just call stream_messages() again with a new start_ns —
             the WebSocket manager cancels the current task and starts a new one.

    stop_event: set this asyncio.Event to halt streaming cleanly mid-bag.
    """
    if speed <= 0:
        raise ValueError("speed must be positive")

    mcap_file = bag_path / _find_mcap(bag_path)
    last_msg_ns:   Optional[int] = None
    last_wall_ns:  Optional[int] = None

    # read_bag yields (schema, channel, message) in timestamp order
    for schema, channel, message in read_bag(str(mcap_file), topics=topics):
        if stop_event and stop_event.is_set():
            return

        ts_ns = message.publish_time  # nanoseconds

        # Skip messages before the requested start position (seek support)
        if ts_ns < start_ns:
            continue

        # --- Timing: sleep to reproduce real-time gaps at requested speed ---
        if last_msg_ns is not None and last_wall_ns is not None:
            gap_ns    = ts_ns - last_msg_ns          # gap in bag time
            elapsed   = asyncio.get_event_loop().time() * 1e9 - last_wall_ns
            sleep_ns  = (gap_ns / speed) - elapsed
            if sleep_ns > 0:
                await asyncio.sleep(sleep_ns / 1e9)

        last_msg_ns  = ts_ns
        last_wall_ns = int(asyncio.get_event_loop().time() * 1e9)

        # --- Serialise ---
        msg_type = schema.name
        try:
            payload = _serialise(message.ros_msg, msg_type)
        except Exception as exc:
            log.warning("Failed to serialise %s on %s: %s", msg_type, channel.topic, exc)
            continue

        yield {
            "topic":        channel.topic,
            "timestamp_ns": ts_ns,
            "msg_type":     msg_type,
            **payload,
        }

    # Natural end of bag
    yield {"topic": "__status__", "timestamp_ns": 0, "msg_type": "__status__", "data": "end"}


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialise(ros_msg: Any, msg_type: str) -> dict:
    """
    Returns a dict with keys  "data"  (and "encoding" for images).
    """
    if msg_type == IMAGE_COMPRESSED_TYPE:
        return _serialise_compressed_image(ros_msg)
    if msg_type == IMAGE_RAW_TYPE:
        return _serialise_raw_image(ros_msg)
    return {"data": _ros_to_dict(ros_msg)}


def _serialise_compressed_image(msg: Any) -> dict:
    """
    sensor_msgs/CompressedImage — the robot already encoded it (JPEG/PNG/etc.).
    Just base64 the bytes and tell the frontend the format.
    """
    import base64
    return {
        "encoding": msg.format,                          # e.g. "jpeg", "png"
        "data":     base64.b64encode(bytes(msg.data)).decode(),
    }


def _serialise_raw_image(msg: Any) -> dict:
    """
    sensor_msgs/Image — raw pixel buffer.
    Encode to JPEG before sending to keep WebSocket traffic manageable.
    """
    import base64
    import numpy as np
    from PIL import Image as PILImage

    encoding = msg.encoding.lower()   # e.g. "rgb8", "bgr8", "mono8", "16uc1"
    height, width = msg.height, msg.width
    data = bytes(msg.data)

    # Map ROS encoding → numpy dtype + PIL mode
    _ENC_MAP = {
        "rgb8":   (np.uint8,  "RGB"),
        "bgr8":   (np.uint8,  "RGB"),   # channels swapped below
        "rgba8":  (np.uint8,  "RGBA"),
        "bgra8":  (np.uint8,  "RGBA"),  # channels swapped below
        "mono8":  (np.uint8,  "L"),
        "mono16": (np.uint16, "I;16"),
        "16uc1":  (np.uint16, "I;16"),
        "32fc1":  (np.float32, None),   # normalised below
    }

    dtype, pil_mode = _ENC_MAP.get(encoding, (np.uint8, "RGB"))
    arr = np.frombuffer(data, dtype=dtype).reshape(height, width, -1) \
          if pil_mode not in ("L", "I;16", None) \
          else np.frombuffer(data, dtype=dtype).reshape(height, width)

    # Swap BGR → RGB
    if encoding in ("bgr8", "bgra8"):
        arr = arr[..., ::-1]

    # Normalise float images to 0-255
    if encoding == "32fc1":
        mn, mx = arr.min(), arr.max()
        arr = ((arr - mn) / (mx - mn + 1e-9) * 255).astype(np.uint8)
        pil_mode = "L"

    img = PILImage.fromarray(arr, mode=pil_mode if pil_mode != "I;16" else "I")
    if img.mode in ("I", "I;16"):
        img = img.point(lambda x: x >> 8, "L")   # 16-bit → 8-bit for JPEG

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return {
        "encoding": "jpeg",
        "data":     base64.b64encode(buf.getvalue()).decode(),
    }


def _ros_to_dict(msg: Any) -> Any:
    """
    Recursively convert a ROS2 message to a JSON-serialisable structure.
    Works via __slots__ (all generated ROS2 Python classes expose these).
    Falls back gracefully for primitives and lists.
    """
    if msg is None:
        return None

    # Primitive types
    if isinstance(msg, (bool, int, float, str)):
        return msg

    # numpy scalars
    try:
        import numpy as np
        if isinstance(msg, np.generic):
            return msg.item()
        if isinstance(msg, np.ndarray):
            return msg.tolist()
    except ImportError:
        pass

    # Lists / tuples / arrays
    if isinstance(msg, (list, tuple)):
        return [_ros_to_dict(v) for v in msg]

    # bytes / bytearray — base64 encode (e.g. PointCloud2 data field)
    if isinstance(msg, (bytes, bytearray)):
        import base64
        return base64.b64encode(bytes(msg)).decode()

    # ROS2 message object — walk __slots__
    if hasattr(msg, "__slots__"):
        return {
            slot.lstrip("_"): _ros_to_dict(getattr(msg, slot))
            for slot in msg.__slots__
        }

    # Last resort
    return str(msg)


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _find_mcap(bag_path: Path) -> str:
    """
    Return the name of the .mcap file inside the bag directory.
    Raises if none or more than one found (ambiguous).
    """
    mcap_files = list(bag_path.glob("*.mcap"))
    if not mcap_files:
        raise FileNotFoundError(f"No .mcap file found in {bag_path}")
    if len(mcap_files) > 1:
        # ros2 bag record splits files over a size threshold — use the first shard
        mcap_files.sort()
        log.warning(
            "%d .mcap shards found in %s — streaming from first shard only. "
            "Multi-shard support is not yet implemented.",
            len(mcap_files), bag_path,
        )
    return mcap_files[0].name