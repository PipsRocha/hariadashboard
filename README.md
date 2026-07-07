HARIA Failure Dashboard

┌─────────────────────────────────────────────────────────┐
│                     React Frontend                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Topic       │  │  Widget Grid │  │  Timeline    │  │
│  │  Selector    │  │  (charts,    │  │  (scrubber)  │  │
│  │              │  │   images...) │  │              │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└────────────┬──────────────────────────┬─────────────────┘
             │ WebSocket (live data)    │ REST (control)
             ▼                          ▼
┌─────────────────────────────────────────────────────────┐
│                  Python FastAPI Backend                 │
│  ┌────────────────┐  ┌────────────────────────────────┐ │
│  │ REST endpoints │  │  WebSocket manager             │ │
│  │ (upload, list, │  │  (streams messages by time)    │ │
│  │  start/stop)   │  │                                │ │
│  └────────────────┘  └────────────────────────────────┘ │
│  ┌────────────────┐  ┌────────────────────────────────┐ │
│  │ rosbag2_py     │  │  rclpy node (record / live)    │ │
│  │ (read bags)    │  │                                │ │
│  └────────────────┘  └────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘


backend/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry point
│   ├── ros_manager.py       # rclpy lifecycle + node access
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── topics.py
│   │   ├── recordings.py
│   │   ├── playback.py
│   │   ├── ws_demo.py
│   │   └── health.py
│   └── schemas.py           # Pydantic models
|   ├── services/
|   |   |── player.py
│   │   ├── recorder.py
├── recordings/
└── run.sh
frontend/
|   |── node_modules
|   |── public/ #image assets
│   ├── src/
│   │   ├── assets/
│   │   |   |── App.css
│   │   |   |── App.tsx
│   │   |   |── index.css
│   │   |   |── main.tsx
│   ├── eslint.config.js
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.app.json
│   ├── tsconfig.json
│   ├── tsconfig.node.json
│   └── vite.config.ts
static/
dashboard_node.py
index.html
main.py