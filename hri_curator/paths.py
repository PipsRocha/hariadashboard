from __future__ import annotations

from pathlib import Path, PurePosixPath


def subject_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Subject root does not exist: {root}")
    return root


def relative_path(root: Path, path: Path) -> str:
    try:
        value = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("Path is outside the subject root") from exc
    return validate_relative(value)


def validate_relative(value: str) -> str:
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or not value or ".." in candidate.parts:
        raise ValueError(f"Expected a root-relative path, got {value!r}")
    return candidate.as_posix()


def safe_join(root: Path, value: str) -> Path:
    clean = validate_relative(value)
    result = (root / clean).resolve()
    if result != root.resolve() and root.resolve() not in result.parents:
        raise ValueError("Path escapes the subject root")
    return result
