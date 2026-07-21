from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hri_curator.config import layout


def write_event(root: str | Path, event: str, **fields: Any) -> None:
    paths = layout(root)
    paths.logs.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": datetime.now(UTC).isoformat(), "event": event, **fields}
    text = json.dumps(payload, sort_keys=True, default=str)
    text = text.replace(str(paths.root), "<subject_root>")
    if paths.root.name: text = text.replace(paths.root.name, "<private_subject_folder>")
    with (paths.logs / "curator.log").open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")
