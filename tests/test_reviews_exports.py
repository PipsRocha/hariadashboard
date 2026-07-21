from __future__ import annotations

from pathlib import Path

from hri_curator.config import layout
from hri_curator.exporter import export_all
from hri_curator.reviews import Annotation, Review, load_review, save_review
from hri_curator.scanner import scan


def test_review_survives_rescan_and_exports_effective_values(subject: Path):
    scan(subject)
    uid = __import__("hri_curator.database", fromlist=["Database"]).Database(layout(subject).database).row("SELECT trial_uid FROM trials")["trial_uid"]
    review = Review(
        trial_uid=uid, review_status="reviewed", condition_reviewed="anomaly",
        task_outcome_reviewed="failure", semantic_validity="valid", reviewer_id="reviewer_01",
        anomaly_present=True, usable_for_normal_training=False,
        usable_for_anomaly_evaluation=True, primary_anomaly_reviewed="handover",
        annotations=[Annotation(annotation_type="anomaly", family="handover", onset_ns=1_000_000_000, offset_ns=2_000_000_000)],
    )
    save_review(subject, review)
    scan(subject, force=True)
    assert load_review(subject, uid).condition_reviewed == "anomaly"
    counts = export_all(subject)
    assert counts["trials"] == 1 and counts["annotations"] == 1
    text = (layout(subject).exports / "trials.csv").read_text()
    assert ",anomaly,anomaly," in text
    all_output = "".join(path.read_text() for path in layout(subject).exports.glob("*.csv"))
    assert str(subject) not in all_output
    assert "private_person_folder" not in all_output


def test_annotation_rejects_reverse_interval():
    import pytest
    with pytest.raises(ValueError):
        Annotation(annotation_type="anomaly", onset_ns=2, offset_ns=1)


def test_review_completion_and_stale_save_validation(subject: Path):
    import pytest
    from hri_curator.database import Database
    from hri_curator.reviews import ReviewConflict
    scan(subject)
    uid = Database(layout(subject).database).row("SELECT trial_uid FROM trials")["trial_uid"]
    with pytest.raises(ValueError, match="reviewed trials require"):
        Review(trial_uid=uid, review_status="reviewed", reviewer_id="reviewer_01")
    first = load_review(subject, uid)
    stale = first.model_copy(deep=True)
    first.review_status = "in_progress"
    save_review(subject, first)
    stale.review_status = "in_progress"
    with pytest.raises(ReviewConflict): save_review(subject, stale)


def test_annotation_must_fit_trial(subject: Path):
    import pytest
    from hri_curator.database import Database
    scan(subject)
    uid = Database(layout(subject).database).row("SELECT trial_uid FROM trials")["trial_uid"]
    review = load_review(subject, uid)
    review.annotations = [Annotation(annotation_type="anomaly", onset_ns=0, offset_ns=11_000_000_000)]
    with pytest.raises(ValueError, match="duration"): save_review(subject, review)


def test_review_rejects_private_paths(subject: Path):
    import pytest
    from hri_curator.database import Database
    scan(subject)
    uid = Database(layout(subject).database).row("SELECT trial_uid FROM trials")["trial_uid"]
    review = load_review(subject, uid)
    review.notes = "/tmp/private-review.txt"
    with pytest.raises(ValueError, match="absolute filesystem path"): save_review(subject, review)
