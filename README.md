# HARIA Failure Dashboard

Web dashboard for ROS 2 (Jazzy) visualization and recording of HRI sessions,
with annotations saved as JSON next to each recording's `.mcap` and
`metadata.yaml`.


## Layout

```
dashboard-backend/
├── app/
│   ├── main.py              # FastAPI entry point, serves the frontend
│   ├── config.py            # paths (override via env vars)
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
│       ├── session_cache.py # ros2 bag play subprocess
│       └── session_state.py # current bag + annotations persistence
├── frontend/
│   ├── index.html
│   └── static/              # sidebar.js, panels.js, timeline.js, explorer.js
│                            # annotations.js, style.css
├── recordings/              # bags land here (gitignore)
├── session_out/             # scrubbable archive of current session (gitignore)
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

Open http://localhost:8000

Paths are configurable via environment variables:

- `HARIA_RECORDINGS_DIR` — where bags are recorded/uploaded
  (default `dashboard-backend/recordings/`)
- `HARIA_SESSION_DIR` — scratch dir for the current session's scrubbable
  archive (default `dashboard-backend/session_out/`)