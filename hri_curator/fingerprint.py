from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hri_curator.config import SCANNER_VERSION
from hri_curator.discovery import TrialCandidate


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(candidate: TrialCandidate, qc_profile_hash: str) -> tuple[dict[str, Any], str]:
    def stat(path: Path) -> dict[str, Any] | None:
        if not path.is_file(): return None
        value = path.stat()
        return {"name": path.name, "size": value.st_size, "mtime_ns": value.st_mtime_ns}

    payload = {
        "relative_trial_path": candidate.relative_path,
        "mcap": [stat(path) for path in candidate.mcap_files],
        "metadata": stat(candidate.metadata_file),
        "metadata_sha256": file_sha256(candidate.metadata_file),
        "session_metadata": stat(candidate.session_metadata_file),
        "session_metadata_sha256": file_sha256(candidate.session_metadata_file),
        "scanner_version": SCANNER_VERSION,
        "qc_profile_hash": qc_profile_hash,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return payload, hashlib.sha256(encoded.encode()).hexdigest()
