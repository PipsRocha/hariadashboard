from __future__ import annotations

import csv
import json
from pathlib import Path

from hri_curator.config import SCHEMA_VERSION, layout, load_subject
from hri_curator.exporter import export_all
from hri_curator.paths import validate_relative


def merge_subjects(subject_roots: list[str], output: str) -> dict[str, int]:
    target = Path(output).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    tables = ["trials.csv", "topic_qc.csv", "annotations.csv", "phase_intervals.csv"]
    combined: dict[str, list[dict[str, str]]] = {name: [] for name in tables}
    fields: dict[str, list[str]] = {}
    subjects: set[str] = set(); trials: set[str] = set()
    for value in subject_roots:
        paths = layout(value); subject = load_subject(paths.root)
        if subject.schema_version != SCHEMA_VERSION: raise ValueError(f"Incompatible schema for {subject.subject_id}")
        if subject.subject_id in subjects: raise ValueError(f"Duplicate subject ID: {subject.subject_id}")
        subjects.add(subject.subject_id); export_all(paths.root)
        for name in tables:
            with (paths.exports / name).open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream); fields.setdefault(name, reader.fieldnames or [])
                for row in reader:
                    for key, item in row.items():
                        if key.startswith("relative_") and item: validate_relative(item)
                        if item and Path(item).is_absolute(): raise ValueError(f"Absolute path in {name}:{key}")
                    if name == "trials.csv":
                        uid = row["trial_uid"]
                        if uid in trials: raise ValueError(f"Duplicate trial ID: {uid}")
                        if not uid.startswith(subject.subject_id + "_"): raise ValueError(f"Invalid trial provenance: {uid}")
                        trials.add(uid)
                    combined[name].append(row)
    for name, rows in combined.items():
        with (target / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields[name]); writer.writeheader(); writer.writerows(rows)
    result = {"subjects": len(subjects), "trials": len(trials)}
    (target / "merge_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
