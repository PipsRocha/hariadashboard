import os
from pathlib import Path

import pytest

from hri_curator.config import QCProfile, DEFAULT_TOPICS
from hri_curator.metadata import load_bag_metadata, load_acquisition_metadata
from hri_curator.qc import fast_topic_qc, usability

SAMPLE = Path(os.environ.get("HRI_CURATOR_REAL_TRIAL", "/nonexistent/hri-curator-real-trial"))


@pytest.mark.skipif(not SAMPLE.exists(), reason="local read-only sample is unavailable")
def test_supplied_trial_fast_qc_without_writes():
    before = {path.name: path.stat().st_mtime_ns for path in SAMPLE.iterdir()}
    bag = load_bag_metadata(SAMPLE / "metadata.yaml")
    acquisition = load_acquisition_metadata(SAMPLE / "session_metadata.yaml")
    rows = fast_topic_qc(QCProfile(required_topics=DEFAULT_TOPICS), bag.topics_with_message_count, bag.duration.nanoseconds / 1e9)
    depth = next(row for row in rows if row.topic_key == "camera2_depth")
    assert depth.message_count == 0 and depth.stream_status == "stream_missing"
    assert depth.qc_status == "PASS_WITH_WARNINGS"
    assert usability(rows)["usable_dual_rgbd"] is False
    assert not hasattr(acquisition, "bag_path") and not hasattr(acquisition, "subject_id")
    assert before == {path.name: path.stat().st_mtime_ns for path in SAMPLE.iterdir()}
