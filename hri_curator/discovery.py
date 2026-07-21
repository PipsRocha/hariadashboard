from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hri_curator.paths import relative_path


@dataclass(frozen=True)
class TrialCandidate:
    path: Path
    relative_path: str
    task_raw: str
    collection_session_id: str
    trial_directory_id: str
    mcap_files: tuple[Path, ...]
    metadata_file: Path
    session_metadata_file: Path

    @property
    def discovery_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self.mcap_files: reasons.append("missing_mcap")
        if len(self.mcap_files) > 1: reasons.append("multiple_mcap_files")
        if not self.metadata_file.is_file(): reasons.append("metadata_missing")
        if not self.session_metadata_file.is_file(): reasons.append("session_metadata_missing")
        return reasons


def discover(root: Path) -> list[TrialCandidate]:
    candidates: list[TrialCandidate] = []
    seen: set[Path] = set()
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "_curation"):
        for session_dir in sorted(p for p in task_dir.iterdir() if p.is_dir()):
            for trial_dir in sorted(p for p in session_dir.iterdir() if p.is_dir()):
                resolved = trial_dir.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidates.append(TrialCandidate(
                    path=trial_dir,
                    relative_path=relative_path(root, trial_dir),
                    task_raw=task_dir.name,
                    collection_session_id=session_dir.name,
                    trial_directory_id=trial_dir.name,
                    mcap_files=tuple(sorted(trial_dir.glob("*.mcap"))),
                    metadata_file=trial_dir / "metadata.yaml",
                    session_metadata_file=trial_dir / "session_metadata.yaml",
                ))
    return candidates
