"""
Live capture node — the dashboard's eyes during a recording.

Subscribes to every discoverable ROS 2 topic and mirrors the on-disk archive
format produced by `bag_indexer` (per-topic data.jsonl / latest.json / JPEG
frames + a periodically refreshed index.json), so the frontend can scrub a
session while it is still being recorded.

Runs inside the backend process on the shared `ros_manager` executor —
no subprocess to babysit.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rosidl_runtime_py.utilities import get_message

from app.ros_manager import ros_manager
from app.services.bag_indexer import is_numeric_type, slug, wipe_dir

RING_BUFFER_SECONDS = 300   # 5 min in-memory per topic
DISCOVERY_INTERVAL  = 2.0   # seconds between topic sweeps
INDEX_INTERVAL      = 0.5   # seconds between index.json refreshes
SKIP_TOPICS = {"/parameter_events", "/rosout", "/clock"}


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _is_image_type(t: str) -> bool:
    return "sensor_msgs/msg/Image" in t or "sensor_msgs/msg/CompressedImage" in t


def _msg_to_dict(msg: Any, depth: int = 0) -> Any:
    if depth > 5:
        return str(msg)
    if hasattr(msg, "get_fields_and_field_types"):
        return {f: _msg_to_dict(getattr(msg, f), depth + 1)
                for f in msg.get_fields_and_field_types()}
    if isinstance(msg, (list, tuple)):
        items = [_msg_to_dict(v, depth + 1) for v in msg]
        return items[:256]
    if isinstance(msg, bytes):
        return list(msg[:64])
    try:
        json.dumps(msg)
        return msg
    except (TypeError, ValueError):
        return str(msg)


def _extract_numeric(msg: Any, msg_type: str) -> Dict[str, List[float]]:
    result: Dict[str, List[float]] = {}
    if "JointState" in msg_type:
        if msg.name:
            result["__names"] = list(msg.name)
            if list(msg.position): result["position"] = list(msg.position)
            if list(msg.velocity): result["velocity"] = list(msg.velocity)
            if list(msg.effort):   result["effort"]   = list(msg.effort)
        return result
    if "Imu" in msg_type:
        result["angular_velocity"]    = [msg.angular_velocity.x, msg.angular_velocity.y, msg.angular_velocity.z]
        result["linear_acceleration"] = [msg.linear_acceleration.x, msg.linear_acceleration.y, msg.linear_acceleration.z]
        return result
    if "Wrench" in msg_type:
        w = msg.wrench if hasattr(msg, "wrench") else msg
        result["force"]  = [w.force.x,  w.force.y,  w.force.z]
        result["torque"] = [w.torque.x, w.torque.y, w.torque.z]
        return result
    if "Twist" in msg_type:
        tw = msg.twist if hasattr(msg, "twist") and hasattr(msg.twist, "linear") else msg
        result["linear"]  = [tw.linear.x,  tw.linear.y,  tw.linear.z]
        result["angular"] = [tw.angular.x, tw.angular.y, tw.angular.z]
        return result
    if "Odometry" in msg_type:
        result["position"] = [msg.pose.pose.position.x, msg.pose.pose.position.y, msg.pose.pose.position.z]
        result["velocity"] = [msg.twist.twist.linear.x, msg.twist.twist.linear.y, msg.twist.twist.linear.z]
        return result
    if hasattr(msg, "data"):
        d = msg.data
        if isinstance(d, (int, float, bool)):
            result["value"] = [float(d)]
        elif hasattr(d, "__iter__"):
            result["value"] = list(d)[:64]
    return result


def _encode_jpeg(msg: Any, msg_type: str) -> Optional[bytes]:
    try:
        import cv2
        import numpy as np
        if "Compressed" in msg_type:
            arr = np.frombuffer(msg.data, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            from cv_bridge import CvBridge
            frame = CvBridge().imgmsg_to_cv2(msg, desired_encoding="bgr8")
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else None
    except Exception:
        return None


class _TopicHandler:
    def __init__(self, topic: str, msg_type: str, out_dir: Path) -> None:
        self.closed   = False
        self.topic    = topic
        self.msg_type = msg_type
        self.slug     = slug(topic)
        self.is_image = _is_image_type(msg_type)
        self.is_num   = is_numeric_type(msg_type)
        self.is_table = "JointState" in msg_type
        self.dir      = out_dir / self.slug
        self.dir.mkdir(parents=True, exist_ok=True)

        self._ring: Deque[dict] = deque()
        self._jsonl = (self.dir / "data.jsonl").open("a", encoding="utf-8")

        self.last_stamp: float = 0.0
        self.t_start:    float = 0.0
        self.count:      int   = 0

    def handle(self, msg: Any) -> None:
        # A subscription can outlive stop() if node teardown partially fails;
        # never write to the session archive after close().
        if self.closed:
            return
        now = time.time()
        if self.t_start == 0.0:
            self.t_start = now
        self.last_stamp = now
        self.count += 1

        if self.is_image:
            jpeg = _encode_jpeg(msg, self.msg_type)
            if jpeg:
                _atomic_write(self.dir / "latest.jpg", jpeg)
                (self.dir / f"{now:.3f}.jpg").write_bytes(jpeg)
            entry = {"t": now, "type": "image", "frame": f"{now:.3f}.jpg"}
        else:
            entry = {"t": now}
            if self.is_num:
                entry.update(_extract_numeric(msg, self.msg_type))
            entry["_raw"] = _msg_to_dict(msg)
            _atomic_write_text(self.dir / "latest.json", json.dumps(
                {"t": now, "topic": self.topic, "msg_type": self.msg_type, **entry}))

        self._ring.append(entry)
        cutoff = now - RING_BUFFER_SECONDS
        while self._ring and self._ring[0]["t"] < cutoff:
            self._ring.popleft()
        self._jsonl.write(json.dumps(entry) + "\n")
        self._jsonl.flush()

    def close(self) -> None:
        self.closed = True
        try:
            self._jsonl.close()
        except Exception:
            pass


class LiveCaptureNode(Node):
    def __init__(self, out_dir: Path) -> None:
        super().__init__("haria_live_capture")
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.handlers: Dict[str, _TopicHandler] = {}
        self._subs: dict = {}
        self._qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        self.create_timer(DISCOVERY_INTERVAL, self._discover)
        self.create_timer(INDEX_INTERVAL, self._write_index)
        self._discover()
        self.get_logger().info(f"Live capture started — writing to {self.out_dir}")

    def _discover(self) -> None:
        for topic, type_list in self.get_topic_names_and_types():
            if topic in self._subs or topic in SKIP_TOPICS or not type_list:
                continue
            msg_type_str = type_list[0]
            try:
                msg_class = get_message(msg_type_str)
            except Exception:
                continue
            handler = _TopicHandler(topic, msg_type_str, self.out_dir)
            self.handlers[topic] = handler
            self._subs[topic] = self.create_subscription(
                msg_class, topic,
                lambda msg, h=handler: h.handle(msg),
                self._qos,
            )
            self.get_logger().info(f"  + {topic}  [{msg_type_str}]")

    def _write_index(self) -> None:
        now = time.time()
        topics = []
        t_min, t_max = now, 0.0
        for h in self.handlers.values():
            if h.t_start > 0:
                t_min = min(t_min, h.t_start)
            t_max = max(t_max, h.last_stamp)
            topics.append({
                "topic":    h.topic,
                "msg_type": h.msg_type,
                "slug":     h.slug,
                "is_image": h.is_image,
                "is_num":   h.is_num,
                "is_table": h.is_table,
                "last_msg": h.last_stamp,
                "count":    h.count,
                "active":   (now - h.last_stamp) < 5.0 if h.last_stamp else False,
            })
        _atomic_write_text(self.out_dir / "index.json", json.dumps({
            "timestamp": now,
            "t_start":   t_min if topics else now,
            "t_end":     t_max if t_max > 0 else now,
            "topics":    topics,
        }))

    def close(self) -> None:
        # Tear down piecewise with per-step guards: destroy_node() can throw
        # (e.g. InvalidHandle racing the spinning executor), and a single
        # failure must not leave subscriptions or timers firing.
        for h in self.handlers.values():
            h.close()
        for sub in list(self._subs.values()):
            try:
                self.destroy_subscription(sub)
            except Exception:
                pass
        self._subs.clear()
        for timer in list(self.timers):
            try:
                self.destroy_timer(timer)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Module-level lifecycle (one live capture at a time)
# ---------------------------------------------------------------------------

_node: Optional[LiveCaptureNode] = None
_lock = threading.Lock()


def start(out_dir: Path) -> None:
    global _node
    with _lock:
        if _node is not None:
            return
        wipe_dir(out_dir)
        _node = LiveCaptureNode(out_dir)
        ros_manager.add_node(_node)


def stop() -> None:
    global _node
    with _lock:
        if _node is None:
            return
        node, _node = _node, None
        node.close()          # silence handlers + destroy subs/timers first
        ros_manager.remove_node(node)
        try:
            node.destroy_node()
        except Exception:
            pass


def is_active() -> bool:
    return _node is not None
