#!/usr/bin/env bash
set -euo pipefail
: "${HRI_CURATOR_ROOT:?Set HRI_CURATOR_ROOT to an initialized subject root}"
exec hri-curator review --root "$HRI_CURATOR_ROOT" --host 0.0.0.0 --port "${HRI_CURATOR_PORT:-8000}"
