# HRI Dataset Curator

Offline subject-level curation for ROS 2 MCAP recordings from the HARIA human-robot interaction dataset. It inventories trials, performs repeatable technical QC, supports synchronized dual-camera review and timeline annotation, and writes portable review sidecars and exports without modifying acquisition data.

The supported runtime is a digest-pinned ROS 2 Jazzy Docker image. The browser runs on the host; the application and ROS bag tooling run in the container. Large datasets stay on the host and are bind-mounted, never copied into the image.

This release is the single-reviewer MVP. Dual RGB playback, technical QC, manual review, annotations, sidecars, and exports are supported. Colorized depth playback, robot-summary tracks, and multi-subject merge are deferred. Depth is still inventoried and checked, but is optional for trial-level validity.

## Data contract

Each command operates on one subject root. Source files are immutable:

```text
<subject_root>/
├── handover|pour|tray/<collection_session>/<trial>/
│   ├── *.mcap
│   ├── metadata.yaml
│   └── session_metadata.yaml
└── _curation/
    ├── subject.yaml
    ├── curator.sqlite
    ├── config/qc_profile.yaml
    ├── reviews/*.review.yaml
    ├── exports/
    ├── reports/
    └── cache/
```

Only root-relative paths and the configured anonymized subject ID are persisted. Absolute paths, acquisition `subject_id`, launch arguments, robot addresses, and source-folder identity are not copied into curator outputs.

## Build and run

Build the image once:

```bash
docker build -t hri-curator:jazzy .
```

The Dockerfile pins the ROS base-image digest and exact Python versions. Each
build also records the resolved ROS/Ubuntu package versions in
`/app/ros-packages.lock` inside the image for release auditing.

If Docker reports `permission denied while trying to connect to the docker API`,
refresh your shell membership and verify access before using the launcher:

```bash
newgrp docker
id
docker version
```

If `id` still does not list `docker`, log out and back in, or reboot. For Snap
Docker, `sudo snap restart docker` may also be needed after group changes. Do
not `chmod 666 /var/run/docker.sock`.

If `id` lists `docker` but Docker still denies access, check the socket:

```bash
ls -l /var/run/docker.sock
```

It should be owned by group `docker`. If it is not, repair the socket group:

```bash
sudo chgrp docker /var/run/docker.sock
sudo chmod 660 /var/run/docker.sock
docker version
```

If Snap Docker recreates the socket with the wrong group after every restart,
the least annoying long-term fix is to replace the Snap package with Docker
Engine.

Use the host launcher for commands. It mounts the subject root read-only and overlays only `_curation` as writable:

```bash
scripts/hri-curator-docker /path/to/subject init --subject-id S001 --reviewer reviewer_01
scripts/hri-curator-docker /path/to/subject scan --deep
scripts/hri-curator-docker /path/to/subject review --queue unreviewed
scripts/hri-curator-docker /path/to/subject export
scripts/hri-curator-docker /path/to/subject clean-cache
```

The launcher runs as your host UID/GID in the host user namespace. This is
needed for Snap Docker installations that otherwise remap the requested UID.
Curator directories created by an older image may need a one-time ownership repair:

```bash
sudo chown -R "$(id -u):$(id -g)" /path/to/subject/_curation
```

Open `http://localhost:8000` after starting `review`. The server is published only on host loopback. Opening an uncached trial starts RGB preview preparation automatically and shows progress in the workspace. The source MCAP remains on the host; previews are written atomically under `_curation/cache` and can be deleted at any time. The cache uses least-recently-used trial eviction with a 20 GB default limit; override it with `HRI_CURATOR_CACHE_MAX_GB`.

The fast scan reads both YAML files and bag metadata. `--deep` additionally uses `rosbag2_py` and the Jazzy MCAP plugin for timestamp coverage, frequency, gaps, and phase intervals. A fast result automatically upgrades when deep QC is requested, and a later fast scan never downgrades an unchanged deep result. Scans are incremental by default; use `--force`, `--dry-run`, or `--recheck warnings,failures` as needed.

Scan errors and technical QC outcomes are separate. Missing expected depth produces `PASS_WITH_WARNINGS` and updates modality flags; it does not make an otherwise healthy trial fail.

## Development

Create the writable overlay once, then launch the development profile. Source edits on the host are reloaded inside the container:

```bash
export HRI_SUBJECT_ROOT=/path/to/subject
export HRI_CURATOR_UID=$(id -u)
export HRI_CURATOR_GID=$(id -g)
mkdir -p "$HRI_SUBJECT_ROOT/_curation"
docker compose --profile dev up --build curator-dev
```

The production profile uses the code baked into the image:

```bash
export HRI_SUBJECT_ROOT=/path/to/subject
export HRI_CURATOR_UID=$(id -u)
export HRI_CURATOR_GID=$(id -g)
mkdir -p "$HRI_SUBJECT_ROOT/_curation"
docker compose --profile prod up --build curator
```

Shipping the image does not ship any dataset, cache, database, or private identity mapping. At the destination, mount the desired subject root using the same runtime contract.

## Local tests

The non-ROS tests can run on the host:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

Deep scan and preview integration require the Jazzy container. The optional real-trial smoke test is read-only. Set `HRI_CURATOR_REAL_TRIAL` to a representative trial directory; otherwise it skips automatically.

Run the full Docker acceptance against a subject without touching its existing `_curation` outputs:

```bash
scripts/acceptance-docker /path/to/subject
```

The script uses a temporary curation overlay, deep-scans the subject, prepares one RGB preview, exports all tables, checks optional-depth semantics, and compares source file size/mtime snapshots before and after.

## Legacy dashboard

The former live recording and ROS graph dashboard is retained under `dashboard-backend/` for posterity. It is not part of the curator image or supported curator workflow. In a separately prepared legacy environment it can be launched with `dashboard-backend/run-legacy.sh` on port 8001.

## Main modules

```text
hri_curator/
├── cli.py             # init, scan, review, export, clean-cache
├── scanner.py         # incremental inventory and scan orchestration
├── qc.py              # topic coverage, timing, status, usability
├── database.py        # SQLite schema and repository helpers
├── reviews.py         # validated atomic YAML sidecars
├── preview.py         # lazy dual-RGB cache through rosbag2_py
├── exporter.py        # stable portable CSV exports
├── webapp.py          # curator API
└── web/               # self-contained browser review client
```
