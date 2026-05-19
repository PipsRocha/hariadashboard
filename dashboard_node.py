"""
HARIA Failure Dashboard — Dashboard Node
----------------------------------------
Subscribes to every discoverable ROS 2 topic, writes per-topic JSONL files
and latest-frame images to disk. Also maintains an in-memory ring buffer for
fast time-windowed queries during live recording.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from rosidl_runtime_py.utilities import get_message
from cv_bridge import CvBridge
import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RING_BUFFER_SECONDS = 300   # 5 min in-memory per topic
DISCOVERY_INTERVAL  = 2.0   # seconds between topic sweeps
SKIP_TOPICS = {"/parameter_events", "/rosout", "/clock"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slug(topic: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", topic).strip("_")


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def is_image_type(t: str) -> bool:
    return "sensor_msgs/msg/Image" in t or "sensor_msgs/msg/CompressedImage" in t


def is_numeric_type(t: str) -> bool:
    NUMERIC = [
        "std_msgs/msg/Float", "std_msgs/msg/Int", "std_msgs/msg/UInt", "std_msgs/msg/Bool",
        "sensor_msgs/msg/JointState", "sensor_msgs/msg/Imu", "sensor_msgs/msg/NavSatFix",
        "geometry_msgs/msg/Wrench", "geometry_msgs/msg/WrenchStamped",
        "geometry_msgs/msg/Twist", "geometry_msgs/msg/TwistStamped",
        "geometry_msgs/msg/Pose", "geometry_msgs/msg/PoseStamped",
        "geometry_msgs/msg/Vector3", "nav_msgs/msg/Odometry",
    ]
    return any(n in t for n in NUMERIC)


def is_table_type(t: str) -> bool:
    return "sensor_msgs/msg/JointState" in t or "diagnostic_msgs/msg/DiagnosticArray" in t


def msg_to_dict(msg: Any, depth: int = 0) -> Any:
    if depth > 5:
        return str(msg)
    if hasattr(msg, "get_fields_and_field_types"):
        return {f: msg_to_dict(getattr(msg, f), depth+1) for f in msg.get_fields_and_field_types()}
    if isinstance(msg, (list, tuple)):
        items = [msg_to_dict(v, depth+1) for v in msg]
        return items[:256] if len(items) > 256 else items
    if isinstance(msg, bytes):
        return list(msg[:64])
    try:
        json.dumps(msg)
        return msg
    except (TypeError, ValueError):
        return str(msg)


def extract_numeric(msg: Any, msg_type: str) -> Dict[str, List[float]]:
    result: Dict[str, List[float]] = {}
    if "JointState" in msg_type:
        if msg.name:
            result["__names"] = list(msg.name)
            if list(msg.position): result["position"] = list(msg.position)
            if list(msg.velocity): result["velocity"] = list(msg.velocity)
            if list(msg.effort):   result["effort"]   = list(msg.effort)
        return result
    if "Imu" in msg_type:
        result["angular_velocity"]    = [msg.angular_velocity.x,    msg.angular_velocity.y,    msg.angular_velocity.z]
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


# ---------------------------------------------------------------------------
# Per-topic handler
# ---------------------------------------------------------------------------

class TopicHandler:
    def __init__(self, topic: str, msg_type: str, out_dir: Path, bridge: CvBridge) -> None:
        self.topic    = topic
        self.msg_type = msg_type
        self.slug     = slug(topic)
        self.bridge   = bridge
        self.is_image = is_image_type(msg_type)
        self.is_num   = is_numeric_type(msg_type)
        self.is_table = is_table_type(msg_type)
        self.dir      = out_dir / self.slug
        self.dir.mkdir(parents=True, exist_ok=True)

        # In-memory ring buffer: list of {"t": float, ...fields}
        self._ring: Deque[dict] = deque()
        self._ring_max_seconds  = RING_BUFFER_SECONDS

        # JSONL file handle (append mode)
        self._jsonl_path = self.dir / "data.jsonl"
        self._jsonl      = self._jsonl_path.open("a", encoding="utf-8")

        self.last_stamp: float = 0.0
        self.t_start:    float = 0.0
        self.count:      int   = 0

    def handle(self, msg: Any) -> None:
        now = time.time()
        if self.t_start == 0.0:
            self.t_start = now
        self.last_stamp = now
        self.count += 1

        if self.is_image:
            self._handle_image(msg, now)
        else:
            self._handle_data(msg, now)

    def _handle_image(self, msg: Any, t: float) -> None:
        try:
            if "Compressed" in self.msg_type:
                arr   = np.frombuffer(msg.data, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            else:
                frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                # Write latest (always)
                atomic_write(self.dir / "latest.jpg", buf.tobytes())
                # Write timestamped frame for scrubbing
                frame_path = self.dir / f"{t:.3f}.jpg"
                frame_path.write_bytes(buf.tobytes())
        except Exception:
            pass

        # Record to ring and JSONL (just the timestamp, no pixel data)
        entry = {"t": t, "type": "image", "frame": f"{t:.3f}.jpg"}
        self._append(entry)

    def _handle_data(self, msg: Any, t: float) -> None:
        raw = msg_to_dict(msg)
        entry: dict = {"t": t}
        if self.is_num:
            entry.update(extract_numeric(msg, self.msg_type))
        entry["_raw"] = raw
        atomic_write_text(self.dir / "latest.json", json.dumps({"t": t, "topic": self.topic, "msg_type": self.msg_type, **entry}))
        self._append(entry)

    def _append(self, entry: dict) -> None:
        self._ring.append(entry)
        # Evict entries older than ring window
        cutoff = entry["t"] - self._ring_max_seconds
        while self._ring and self._ring[0]["t"] < cutoff:
            self._ring.popleft()
        # Write to JSONL
        self._jsonl.write(json.dumps(entry) + "\n")
        self._jsonl.flush()

    def query(self, t: float, window: float = 10.0) -> List[dict]:
        """Return ring entries within [t-window, t+window/10]."""
        lo, hi = t - window, t + window / 10
        return [e for e in self._ring if lo <= e["t"] <= hi]

    def closest_frame(self, t: float) -> Optional[Path]:
        """Return path to the image frame closest to t."""
        frames = sorted(self.dir.glob("*.jpg"))
        if not frames:
            return None
        # exclude latest.jpg
        frames = [f for f in frames if f.stem != "latest"]
        if not frames:
            return self.dir / "latest.jpg"
        best = min(frames, key=lambda f: abs(float(f.stem) - t))
        return best

    def close(self) -> None:
        self._jsonl.close()


# ---------------------------------------------------------------------------
# Dashboard Node
# ---------------------------------------------------------------------------

class DashboardNode(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("haria_dashboard")
        self.bridge   = CvBridge()
        self.out_dir  = Path(args.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.fps      = max(0.5, float(args.fps))
        self.handlers: Dict[str, TopicHandler] = {}
        self._subs:    dict = {}
        self._qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        self.create_timer(DISCOVERY_INTERVAL, self._discover)
        self.create_timer(1.0 / self.fps, self._write_index)
        self._discover()
        self.get_logger().info(f"HARIA Dashboard node started — writing to {self.out_dir}")

    def _discover(self) -> None:
        for topic, type_list in self.get_topic_names_and_types():
            if topic in self._subs or topic in SKIP_TOPICS:
                continue
            if not type_list:
                continue
            msg_type_str = type_list[0]
            try:
                msg_class = get_message(msg_type_str)
            except Exception:
                continue
            handler = TopicHandler(topic, msg_type_str, self.out_dir, self.bridge)
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
            if h.last_stamp > t_max:
                t_max = h.last_stamp
            topics.append({
                "topic":      h.topic,
                "msg_type":   h.msg_type,
                "slug":       h.slug,
                "is_image":   h.is_image,
                "is_num":     h.is_num,
                "is_table":   h.is_table,
                "last_msg":   h.last_stamp,
                "count":      h.count,
                "active":     (now - h.last_stamp) < 5.0 if h.last_stamp else False,
            })
        atomic_write_text(self.out_dir / "index.json", json.dumps({
            "timestamp": now,
            "t_start":   t_min if topics else now,
            "t_end":     t_max if t_max > 0 else now,
            "topics":    topics,
        }))

    def destroy_node(self) -> None:
        for h in self.handlers.values():
            try: h.close()
            except Exception: pass
        super().destroy_node()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="dashboard/out")
    parser.add_argument("--fps", type=float, default=5.0)
    args = parser.parse_args()
    rclpy.init()
    node = DashboardNode(args)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        try: rclpy.shutdown()
        except Exception: pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
