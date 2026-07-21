# HRI Dataset Curator — Complete Task Description

## 0. Supported product boundary

This specification describes an **offline-only**, browser-based curator. Live
recording, ROS graph introspection, bag upload, and replay onto a live ROS graph
are out of scope. The former live dashboard remains in the repository behind a
separate legacy entrypoint and is not imported by the curator.

The supported runtime is a Docker image pinned to ROS 2 Jazzy, Ubuntu 24.04,
and Python 3.12. The image contains the application code for production. During
development the repository is bind-mounted over that code. A subject root is
mounted read-only at `/data/subject`, with its `_curation` directory overlaid as
a separate read-write mount. Datasets and previews are never copied into the
image.

The review interface extends the existing FastAPI/browser architecture. PySide6
and container GUI forwarding are not part of the supported implementation.

## 1. Goal

Build a local dataset-curation tool for ROS 2 MCAP recordings collected during three human–robot interaction tasks:

- `handover` / unscrew
- `pour`
- `tray`

Each subject is curated independently. The tool is launched with the **subject directory as its root**, for example:

```bash
hri-curator scan --root ~/my_moveit_bags/joao_b
hri-curator review --root ~/my_moveit_bags/joao_b
```

The tool has two stages:

1. **Automated inventory and technical quality control**
2. **Manual synchronized playback, semantic review, and timeline annotation**

The original MCAP files and acquisition metadata are immutable source data. The curator must not edit `metadata.yaml`, overwrite MCAP files, or treat acquisition labels as verified ground truth.

## 2. Core design principles

### 2.1 Subject-root operation

Every invocation operates on one subject directory:

```text
<subject_root>/
├── handover/
│   └── <collection_session>/
│       └── <trial>/
│           ├── <trial>_0.mcap
│           ├── metadata.yaml
│           └── session_metadata.yaml
├── pour/
└── tray/
```

All paths stored in databases, CSV files, YAML sidecars, reports, and logs must be **relative to `<subject_root>`**.

Store:

```text
handover/fr3_20260715_161643_379685/20260715_162218_636972
```

Never store an absolute machine path.

### 2.2 Immutable acquisition data

Treat these as read-only:

```text
*.mcap
metadata.yaml
session_metadata.yaml
```

Manual corrections go into curator-owned sidecars.

### 2.3 Acquisition, reviewed, and effective values

Keep:

```text
condition_acquired
condition_reviewed
condition_effective
```

where:

```text
condition_effective =
    condition_reviewed, if manually reviewed
    otherwise condition_acquired
```

Apply the same pattern to task outcome, anomaly family, consequence, and semantic validity.

### 2.4 Technical and semantic validity are independent

A trial can be technically valid but anomalous, technically defective but behaviorally normal, or usable for RGB but not RGB-D. Do not reduce everything to one binary `valid` field.

## 3. Subject anonymization

Initialize each subject root with an explicit anonymized ID:

```bash
hri-curator init   --root ~/my_moveit_bags/joao_b   --subject-id S001   --reviewer reviewer_01
```

Create:

```text
<subject_root>/_curation/subject.yaml
```

```yaml
schema_version: 2
subject_id: S001
dataset_version: pilot_v1
reviewer_assignment: reviewer_01
```

The tool must never infer the public ID from the source-folder name.

Keep the private mapping outside all subject roots:

```csv
source_folder,subject_id
joao_b,S001
```

This file must never be copied into the public dataset.

Create stable trial IDs:

```text
<subject_id>_<task>_<collection_session_id>_<trial_id>
```

Example:

```text
S001_unscrew_fr3_20260715_161643_379685_20260715_162218_636972
```

## 4. Generated output structure

```text
<subject_root>/
├── handover/
├── pour/
├── tray/
└── _curation/
    ├── subject.yaml
    ├── curator.sqlite
    ├── config/
    │   └── qc_profile.yaml
    ├── reviews/
    │   └── <trial_uid>.review.yaml
    ├── exports/
    │   ├── trials.csv
    │   ├── topic_qc.csv
    │   ├── annotations.csv
    │   └── phase_intervals.csv
    ├── reports/
    │   ├── latest_scan_summary.json
    │   ├── latest_scan_summary.md
    │   └── qc_failures.csv
    ├── cache/
    └── logs/
        └── curator.log
```

The cache may contain temporary previews but must be safely deletable.

## 5. Stage 1 — automated scan and QC

### 5.1 Trial discovery

Recursively discover candidate trial folders. Register malformed candidates instead of silently ignoring them:

- missing MCAP;
- multiple MCAPs;
- missing YAML;
- unreadable YAML;
- unreadable MCAP.

### 5.2 Incremental processing

Skip trials that were already scanned and have not changed.

Store a fingerprint containing at least:

```text
relative trial path
MCAP filename
MCAP size
MCAP modification time
metadata.yaml size and modification time
session_metadata.yaml size and modification time
scanner version
QC-profile version
```

Prefer hashes for the two YAML files.

Skip when:

```text
scan_status == complete
AND stored fingerprint == current fingerprint
AND scanner version unchanged
AND QC-profile version unchanged
```

Required commands:

```bash
# Default resume behavior
hri-curator scan --root <subject_root>

# Recheck warnings and failures
hri-curator scan --root <subject_root> --recheck warnings,failures

# Force all trials
hri-curator scan --root <subject_root> --force

# Preview work
hri-curator scan --root <subject_root> --dry-run
```

Example progress:

```text
[12/48] SKIP  S001_unscrew_... unchanged
[13/48] SCAN  S001_unscrew_...
[14/48] WARN  S001_unscrew_... missing camera2 depth
```

Manual reviews must never be removed during rescanning.

### 5.3 Fast metadata pass

Extract:

```text
subject_id
task_raw
task_normalized
collection_session_id
trial_directory_id
trial_uid
relative_trial_path
relative_mcap_path
mcap_filename
bag_size_bytes
duration_sec
starting_time
message_count
ros_distro
storage_identifier

condition_acquired
primary_anomaly_acquired
secondary_consequence_acquired
controller_terminal_phase
controller_completion_reason
session_id_acquired
episode_id_acquired
experiment_config_sha256
```

Absolute paths may be used transiently in memory but must not be exported.

### 5.4 Topic-level QC

Create one row per trial-topic pair:

```text
trial_uid
topic_name
message_type
message_count
mean_hz
first_message_offset_sec
last_message_offset_sec
coverage_ratio
median_dt_ms
p95_dt_ms
max_gap_ms
required
qc_status
qc_reason
```

The fast pass can use rosbag metadata for counts. The deep pass reads MCAP timestamps without decoding image pixels.

### 5.5 Expected sensor profile

Both cameras are expected to provide RGB and depth. RGB is required for this
profile; depth is optional-but-expected and therefore affects modality flags
and warning reports without alone failing a trial:

```yaml
schema_version: 2
profile_id: dual_rgbd_fr3_v1

required_topics:
  camera1_rgb:
    topic: /camera1/camera1/color/image_raw/compressed
    required_for_trial: true
    expected_hz: 30
    minimum_mean_hz: 27
    maximum_gap_ms: 250
    minimum_coverage_ratio: 0.95

  camera1_depth:
    topic: /camera1/camera1/depth/image_rect_raw/compressedDepth
    required_for_trial: false
    expected_hz: 30
    minimum_mean_hz: 27
    maximum_gap_ms: 250
    minimum_coverage_ratio: 0.95

  camera2_rgb:
    topic: /camera2/camera2/color/image_raw/compressed
    required_for_trial: true
    expected_hz: 30
    minimum_mean_hz: 27
    maximum_gap_ms: 250
    minimum_coverage_ratio: 0.95

  camera2_depth:
    topic: /camera2/camera2/depth/image_rect_raw/compressedDepth
    required_for_trial: false
    expected_hz: 30
    minimum_mean_hz: 27
    maximum_gap_ms: 250
    minimum_coverage_ratio: 0.95

  robot_pose:
    topic: /franka_robot_state_broadcaster/current_pose
    expected_hz: 1000
    minimum_mean_hz: 900
    maximum_gap_ms: 50
    minimum_coverage_ratio: 0.98

  external_joint_torques:
    topic: /franka_robot_state_broadcaster/external_joint_torques
    expected_hz: 1000
    minimum_mean_hz: 900
    maximum_gap_ms: 50
    minimum_coverage_ratio: 0.98

  external_wrench:
    topic: /franka_robot_state_broadcaster/external_wrench_in_base_frame
    expected_hz: 1000
    minimum_mean_hz: 900
    maximum_gap_ms: 50
    minimum_coverage_ratio: 0.98

  measured_joint_states:
    topic: /franka_robot_state_broadcaster/measured_joint_states
    expected_hz: 1000
    minimum_mean_hz: 900
    maximum_gap_ms: 50
    minimum_coverage_ratio: 0.98

  task_phase:
    topic: /task_state/phase
    expected_hz: 20
    minimum_mean_hz: 15
    maximum_gap_ms: 500
    minimum_coverage_ratio: 0.90
```

Thresholds must be configurable.

### 5.6 Missing portions of streams

Distinguish:

```text
stream_complete
stream_missing
stream_partial
stream_low_frequency
stream_large_gap
stream_starts_late
stream_ends_early
```

Calculate:

```text
first_message_offset
last_message_offset
coverage_duration
coverage_ratio
median gap
p95 gap
maximum gap
```

A zero-count depth stream is `missing` and produces a trial warning. A stream covering only part of the episode is `partial`. A stream spanning the episode but containing a long interruption is `large_gap`. Missing depth always makes the corresponding depth usability flag false, even though the topic-level severity is a warning.

### 5.7 Task-state QC

Decode phase strings, derive phase intervals, record the terminal phase, and detect invalid phase sequences or missing completion. Keep controller completion separate from manually reviewed task outcome.

### 5.8 QC statuses

Use:

```text
PASS
PASS_WITH_WARNINGS
FAIL
NOT_CHECKED
```

Reason codes include:

```text
missing_mcap
multiple_mcap_files
metadata_missing
session_metadata_missing
metadata_parse_error
session_metadata_parse_error
mcap_unreadable
duration_too_short
duration_outlier
required_topic_missing
required_topic_zero_messages
required_stream_partial
low_topic_frequency
large_timestamp_gap
stream_starts_late
stream_ends_early
camera_sync_warning
missing_task_phase
invalid_phase_sequence
terminal_phase_not_reached
```

### 5.9 Modality-specific usability

Generate:

```text
usable_rgb
usable_depth
usable_dual_view
usable_dual_rgbd
usable_proprio
usable_task_state
technical_qc_status
```

## 6. End-of-scan statistics

Every scan prints and saves a complete subject report.

### 6.1 Processing summary

```text
Subject: S001
Trials discovered: 48
New trials processed: 12
Changed trials reprocessed: 2
Unchanged trials skipped: 34
Scan failures: 0
Elapsed time: 00:04:31
```

Do not print full absolute paths outside verbose mode.

### 6.2 Task distribution

```text
Task             Trials    Pass    Warning    Fail
unscrew              27      24          3       0
pour                  0       0          0       0
tray                  21      19          2       0
```

### 6.3 Sensor completeness

Print counts and percentages:

```text
Required stream           Complete    Partial    Missing    Low-rate
camera1_rgb                48 (100%)    0 (0%)     0 (0%)     0 (0%)
camera1_depth              48 (100%)    0 (0%)     0 (0%)     0 (0%)
camera2_rgb                48 (100%)    0 (0%)     0 (0%)     0 (0%)
camera2_depth              12 (25%)     5 (10%)   31 (65%)    0 (0%)
robot_pose                 48 (100%)    0 (0%)     0 (0%)     0 (0%)
external_joint_torques     48 (100%)    0 (0%)     0 (0%)     0 (0%)
external_wrench            48 (100%)    0 (0%)     0 (0%)     0 (0%)
task_phase                 48 (100%)    0 (0%)     0 (0%)     0 (0%)
```

### 6.4 Missing-duration statistics

For affected streams:

```text
camera2_depth:
  affected trials: 36/48
  total missing duration: 412.8 s
  median missing proportion: 74.2%
  worst trial: S001_tray_... (100% missing)
```

### 6.5 Frequency and gap statistics

```text
Stream          Median Hz    Min Hz    Median max-gap    Worst max-gap
camera1_rgb        29.94      29.71          42 ms             95 ms
camera1_depth      29.94      29.70          43 ms            101 ms
camera2_rgb        29.93      29.66          44 ms            113 ms
camera2_depth      29.91       0.00          48 ms         26,607 ms
robot_pose        998.80     941.20           6 ms             31 ms
```

### 6.6 Review progress

```text
Manual review:
  reviewed: 18/48
  unreviewed: 28/48
  needs second review: 2/48

Reviewed condition:
  normal: 14
  anomaly: 3
  ambiguous: 1
```

### 6.7 Affected-trial lists

Group concise terminal output by reason and write the full list to:

```text
_curation/reports/qc_failures.csv
```

Persist:

```text
_curation/reports/latest_scan_summary.json
_curation/reports/latest_scan_summary.md
_curation/reports/qc_failures.csv
```

## 7. Stage 2 — manual review and annotation

Launch:

```bash
hri-curator review --root <subject_root>
```

Queues:

```text
unreviewed
qc_warnings
qc_failures
needs_second_review
reviewed_anomalies
ambiguous
all
```

Filters:

```text
task
collection session
technical QC status
review status
reviewed condition
task outcome
anomaly family
```

### 7.1 Playback

The MVP displays synchronized:

- camera 1 RGB;
- camera 2 RGB;
- common timeline;
- frame stepping;
- playback speed;
- missing-stream indicators.

Synchronize using MCAP timestamps, not frame indices. Temporary preview files are allowed only in the disposable cache.

Colorized depth playback and robot-summary tracks are post-MVP work. Depth QC
and availability remain part of the MVP catalogue and reports.

### 7.2 Timeline tracks

Show:

```text
task phase
internal phase
motion requests
camera availability
external wrench summary
joint torque summary
end-effector pose summary
manual annotations
```

### 7.3 Review form

Required fields:

```text
review_status:
  unreviewed
  in_progress
  reviewed
  needs_second_review

condition_reviewed:
  normal
  anomaly
  ambiguous
  exclude

task_outcome_reviewed:
  success
  recovered
  incomplete
  failure
  operator_abort
  robot_abort
  unknown

semantic_validity:
  valid
  questionable
  invalid

primary_anomaly_reviewed
secondary_consequence_reviewed
anomaly_present
usable_for_normal_training
usable_for_anomaly_evaluation
manual_exclusion_reason
reviewer_id
reviewed_at
notes
```

### 7.4 Timeline annotations

Support point and interval annotations:

```text
annotation_id
trial_uid
annotation_type
family
subtype
onset_ns
offset_ns
onset_sec
offset_sec
phase_at_onset
internal_phase_at_onset
primary
severity
confidence
reviewer_id
notes
```

Suggested types:

```text
anomaly
human_action
robot_event
task_failure
recovery
consequence
uncertain_region
```

Store time internally as integer nanoseconds relative to bag start.

### 7.5 Review sidecars

Save:

```text
_curation/reviews/<trial_uid>.review.yaml
```

Use atomic writes and update SQLite transactionally. Never modify source YAML.

## 8. Database and exports

Use:

```text
_curation/curator.sqlite
```

Suggested tables:

```text
subjects
trials
trial_fingerprints
topics
topic_qc
phase_intervals
reviews
annotations
qc_reason_codes
scan_runs
```

### 8.1 `trials.csv`

One row per trial with anonymized subject ID and root-relative paths:

```text
subject_id
trial_uid
task_raw
task_normalized
collection_session_id
trial_directory_id
relative_trial_path
relative_mcap_path
condition_acquired
condition_reviewed
condition_effective
controller_terminal_phase
controller_completion_reason
task_outcome_reviewed
task_outcome_effective
primary_anomaly_acquired
primary_anomaly_reviewed
primary_anomaly_effective
secondary_consequence_reviewed
anomaly_count
first_anomaly_onset_sec
last_anomaly_offset_sec
duration_sec
bag_size_bytes
message_count
technical_qc_status
technical_qc_reasons
usable_rgb
usable_depth
usable_dual_view
usable_dual_rgbd
usable_proprio
usable_task_state
semantic_validity
usable_for_normal_training
usable_for_anomaly_evaluation
review_status
reviewer_id
reviewed_at
manual_notes
```

### 8.2 `topic_qc.csv`

```text
subject_id
trial_uid
relative_trial_path
topic_name
message_type
message_count
expected_hz
mean_hz
first_message_offset_sec
last_message_offset_sec
coverage_ratio
median_dt_ms
p95_dt_ms
max_gap_ms
required
qc_status
qc_reason
```

### 8.3 `annotations.csv`

```text
subject_id
trial_uid
annotation_id
annotation_type
family
subtype
onset_ns
offset_ns
onset_sec
offset_sec
phase_at_onset
primary
severity
confidence
reviewer_id
notes
```

### 8.4 `phase_intervals.csv`

```text
subject_id
trial_uid
phase
start_ns
end_ns
start_sec
end_sec
duration_sec
```

CSV files are regenerated exports, not the canonical editable source.

## 9. Multi-reviewer workflow

This section is post-MVP. The single-subject export workflow is supported in
the MVP; the `merge` command is not exposed until this workflow receives its
own acceptance coverage.

Assign complete subject roots to reviewers:

```text
Reviewer A: S001, S002
Reviewer B: S003, S004
Reviewer C: S005, S006
```

Every subject root has its own database, sidecars, exports, and reports.

Because every trial ID starts with the anonymized subject ID, central aggregation is straightforward:

```bash
hri-curator merge   --subjects /dataset/S001 /dataset/S002 /dataset/S003   --output /dataset/combined_catalog
```

The merge should verify:

- globally unique trial IDs;
- compatible schema versions;
- valid subject configuration;
- no accidental absolute paths;
- reviewer provenance.

It must not require the private identity mapping.

## 10. CLI

```bash
hri-curator init   --root <subject_root>   --subject-id S001   --reviewer reviewer_01

hri-curator scan   --root <subject_root>   --deep

hri-curator validate   --root <subject_root>   --only new,warnings

hri-curator review   --root <subject_root>   --queue unreviewed

hri-curator export   --root <subject_root>

hri-curator clean-cache   --root <subject_root>
```

`clean-cache` must never delete MCAPs or review sidecars.

## 11. Implementation stack

```text
Python 3.12
ROS 2 Jazzy
rosbag2_py
FastAPI
Browser-native HTML, CSS, and JavaScript
OpenCV
SQLite
PyYAML
Pydantic
NumPy
```

Suggested modules:

```text
hri_curator/
├── cli.py
├── config.py
├── discovery.py
├── fingerprint.py
├── metadata_parser.py
├── mcap_reader.py
├── anonymization.py
├── sidecars.py
├── export.py
├── merge.py
├── qc/
│   ├── topics.py
│   ├── coverage.py
│   ├── timing.py
│   ├── phase_sequence.py
│   └── report.py
├── database/
│   ├── schema.py
│   └── repository.py
├── reviews.py
├── preview.py
├── webapp.py
└── web/
    ├── index.html
    ├── app.js
    └── style.css
```

`rosbag2_py.SequentialReader` with the Jazzy MCAP storage plugin is the
canonical reader. The fast pass validates ROS metadata YAML. The deep pass
streams serialized messages for timestamps and deserializes only selected
semantic and preview topics. MCAP is the only supported MVP storage format.

## 12. Minimum viable product

The MVP must include:

1. subject-root initialization with anonymized ID;
2. recursive trial discovery;
3. root-relative-only storage;
4. incremental fingerprint-based skipping;
5. parsing of both YAML files;
6. MCAP duration and topic counts;
7. expected-topic QC for dual RGB-D and robot state, with depth optional for trial validity;
8. timestamp coverage and maximum-gap checks;
9. end-of-scan statistics and reports;
10. SQLite catalogue;
11. CSV export;
12. synchronized dual-camera RGB playback;
13. manual condition and task-outcome correction;
14. point and interval annotations;
15. per-trial review sidecars;
16. save-and-next workflow.

## 13. Acceptance tests

### Incremental scan

- First scan processes all trials.
- Second unchanged scan skips all trials.
- Modifying one source YAML reprocesses only that trial.
- Changing the QC profile re-evaluates affected trials.
- Manual reviews survive every rescan.

### Relative paths

No export or sidecar may contain absolute paths or the original subject-folder name.

### Anonymization

For source root `joao_b` initialized as `S001`:

```text
subject_id == S001
trial_uid starts with S001_
```

### Missing depth

For camera 2 depth with zero messages:

```text
camera2_depth status == missing
technical_qc_status == PASS_WITH_WARNINGS unless another failure exists
usable_dual_rgbd == false
```

The report must include affected count, percentage, trial ID, and missing duration.

### Partial depth

For 60% stream coverage:

```text
status == partial
coverage_ratio approximately 0.60
```

### Manual correction

A trial acquired as normal but reviewed as anomalous exports:

```text
condition_acquired = normal
condition_reviewed = anomaly
condition_effective = anomaly
```

Rescanning preserves the correction.

### Portability

Copy the subject root to another absolute location. The tool must open it without migration or database editing.

## 14. Final workflow

```bash
hri-curator init   --root ~/my_moveit_bags/joao_b   --subject-id S001   --reviewer reviewer_01

hri-curator scan   --root ~/my_moveit_bags/joao_b   --deep

hri-curator review   --root ~/my_moveit_bags/joao_b   --queue unreviewed

hri-curator export   --root ~/my_moveit_bags/joao_b
```

The completed subject root contains immutable source recordings, automated QC, manually verified labels, timeline annotations, anonymized root-relative exports, and no permanent duplicate frame dataset.
