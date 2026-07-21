from __future__ import annotations

import argparse
import json
from pathlib import Path

from hri_curator.config import initialize, layout, load_subject
from hri_curator.exporter import export_all
from hri_curator.scanner import scan


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hri-curator")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--root", required=True); init.add_argument("--subject-id", required=True); init.add_argument("--reviewer", required=True)
    scan_cmd = commands.add_parser("scan")
    scan_cmd.add_argument("--root", required=True); scan_cmd.add_argument("--deep", action="store_true")
    scan_cmd.add_argument("--force", action="store_true"); scan_cmd.add_argument("--dry-run", action="store_true")
    scan_cmd.add_argument("--recheck", default="")
    review = commands.add_parser("review")
    review.add_argument("--root", required=True); review.add_argument("--queue", default="unreviewed")
    review.add_argument("--host", default="127.0.0.1"); review.add_argument("--port", type=int, default=8000)
    export = commands.add_parser("export"); export.add_argument("--root", required=True)
    clean = commands.add_parser("clean-cache"); clean.add_argument("--root", required=True)
    return root


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.command == "init":
        paths = initialize(args.root, args.subject_id, args.reviewer)
        print(f"Initialized {args.subject_id} at {paths.curation}")
    elif args.command == "scan":
        result = scan(args.root, deep=args.deep, force=args.force, dry_run=args.dry_run,
                      recheck={x.strip() for x in args.recheck.split(",") if x.strip()})
        print(json.dumps({key: value for key, value in result.items() if key != "trials"}, indent=2))
    elif args.command == "review":
        import os
        import uvicorn
        os.environ["HRI_CURATOR_ROOT"] = str(Path(args.root).expanduser().resolve())
        os.environ["HRI_CURATOR_QUEUE"] = args.queue
        load_subject(args.root)
        print(f"Review UI: http://localhost:{args.port}")
        uvicorn.run("hri_curator.webapp:app", host=args.host, port=args.port)
    elif args.command == "export":
        print(json.dumps(export_all(args.root), indent=2))
    elif args.command == "clean-cache":
        from hri_curator.preview import clean_cache
        paths = layout(args.root); clean_cache(paths.root)
        print(f"Cleared {paths.cache}")


if __name__ == "__main__":
    main()
