# HARIA Failure Dashboard

Web dashboard for ROS 2 (Jazzy) visualization and recording of HRI sessions,
with annotations saved as JSON next to each recording's `.mcap` and
`metadata.yaml`.

```
┌─────────────────────────────────────────────────────────┐
│              Frontend (React, served at /)              │
│  topic sidebar · widget panels · timeline scrubber ·    │
│  annotation track + sidebar                             │
└────────────────────────────┬────────────────────────────┘
                             │ REST (poll index / windowed data / images)
                             ▼
┌─────────────────────────────────────────────────────────┐
│           FastAPI backend (dashboard-backend)           │
│                                                         │
│  recorder.py      wraps `ros2 bag record -s mcap`       │
│  live_capture.py  rclpy node mirroring the live graph   │
│                   into the session archive              │
│  bag_indexer.py   pre-processes uploaded bags (.mcap /  │
│                   .db3) into a scrubbable archive       │
│  player.py        wraps `ros2 bag play` (optional)      │
│  bag_reader.py    WebSocket streaming from .mcap        │
│  ros_manager.py   rclpy lifecycle (shared executor)     │
└─────────────────────────────────────────────────────────┘
```

## Layout

```
dashboard-backend/
├── app/
│   ├── main.py              # FastAPI entry point, serves the frontend
│   ├── config.py            # paths (overridable via env vars)
│   ├── ros_manager.py       # rclpy lifecycle + shared executor
│   ├── schemas.py
│   ├── routers/
│   │   ├── session.py       # upload, topic archive, session annotations
│   │   ├── recordings.py    # record start/stop, list, per-bag annotations
│   │   ├── playback.py      # /player — replay a bag onto the ROS graph
│   │   ├── topics.py        # live topic list (+ WebSocket)
│   │   ├── ws_demo.py       # WebSocket bag streaming (seek/pause/speed)
│   │   └── health.py
│   └── services/
│       ├── recorder.py      # ros2 bag record subprocess (mcap)
│       ├── live_capture.py  # live topic capture during recording
│       ├── bag_indexer.py   # bag → JSONL/JPEG archive for scrubbing
│       ├── bag_reader.py    # mcap reading + streaming
│       ├── player.py        # ros2 bag play subprocess
│       └── session_state.py # current bag + annotations persistence
├── frontend/
│   ├── index.html
│   └── static/              # sidebar.js, panels.js, timeline.js,
│                            # annotations.js, style.css
├── recordings/              # bags land here (gitignored)
├── session_out/             # scrubbable archive of current session (gitignored)
├── requirements.txt
└── run.sh
```

## Running

```bash
cd dashboard-backend
source /opt/ros/jazzy/setup.bash
python3 -m venv --system-site-packages .venv   # first time only
source .venv/bin/activate
pip install -r requirements.txt               # first time only
./run.sh
```

Open http://localhost:8000 — the backend serves the frontend.

`--system-site-packages` matters: `rclpy`, `cv_bridge` and OpenCV come from
the ROS 2 installation, not pip.

Paths are configurable via environment variables:

- `HARIA_RECORDINGS_DIR` — where bags are recorded/uploaded
  (default `dashboard-backend/recordings/`)
- `HARIA_SESSION_DIR` — scratch dir for the current session's scrubbable
  archive (default `dashboard-backend/session_out/`)

## How a session works

**Record** — `POST /recordings/start` launches `ros2 bag record -s mcap`
(empty topic list = record everything) and an in-process live-capture node
that mirrors incoming messages into `session_out/` so the dashboard can
scrub the session while it records. `POST /recordings/stop` SIGINTs the
recorder so rosbag2 flushes `metadata.yaml`, and ensures `annotations.json`
exists next to the bag.

**Playback** — open an existing recording from the list
(`POST /playback/open {name}`) or upload a bag (`.mcap`/`.db3` directly, or a
zipped/tarred bag folder) via `POST /playback/upload`. Either way it's
indexed in the background into `session_out/` (progress via
`GET /playback/status`), then scrubbed/played from the timeline
(play/pause + drag).

**Annotations** — the frontend loads/saves the current session's annotations
via `GET`/`POST /session/annotations`; they're written to
`annotations.json` inside the bag folder, next to the `.mcap` and
`metadata.yaml`, so they travel with the recording. Per-recording access is
also available at `GET`/`POST /recordings/{name}/annotations`.

**Visualization panels** — click a topic in the sidebar to open a panel;
change its type from the panel's type button. Types: Image (nearest frame),
Video (smooth frame playback with preloading), Line Chart and 2D Plot
(numeric topics — the indexer flattens numeric fields to top-level keys like
`pose.position.x`), Table, JSON Inspector, Audio (waveform + playback synced
to the timeline, for `audio_common_msgs` topics — needs the bag's `AudioInfo`
for correct sample rate), and 3D / TF (frame tree from `/tf` + `/tf_static`,
rendered with three.js; TF is stored compactly rather than skipped).

**Annotations explorer** — the home screen's third card opens an explorer
over every `annotations.json` across recordings (`GET
/recordings/annotations`): group by name, occurrence counts across files,
filter by name / recording / minimum count, export the filtered set, and
jump straight into playback at an annotation's start time.
