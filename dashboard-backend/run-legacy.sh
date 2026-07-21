#!/usr/bin/env bash
set -euo pipefail
source /opt/ros/jazzy/setup.bash
source .venv/bin/activate
exec uvicorn app.legacy_main:app --reload --host 0.0.0.0 --port "${HARIA_LEGACY_PORT:-8001}"
