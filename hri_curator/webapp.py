from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from hri_curator.config import layout, load_profile, load_subject, persist_current_config
from hri_curator.database import Database
from hri_curator.exporter import export_all
from hri_curator.preview import nearest_frame, preview_state, start_preview
from hri_curator.reviews import Review, ReviewConflict, load_review, reconcile_sidecars, save_review


def _root() -> Path:
    value = os.environ.get("HRI_CURATOR_ROOT")
    if not value: raise RuntimeError("HRI_CURATOR_ROOT is not set")
    return layout(value).root


WEB_DIR = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    paths = layout(_root())
    subject = load_subject(paths.root)
    persist_current_config(paths.root, subject, load_profile(paths.root))
    Database(paths.database).initialize(subject)
    reconcile_sidecars(_root())
    yield


app = FastAPI(title="HRI Dataset Curator", version="0.1.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/subject")
async def get_subject():
    root = _root(); subject = load_subject(root); db = Database(layout(root).database)
    counts = db.row("SELECT COUNT(*) total, SUM(technical_qc_status='PASS') pass, SUM(technical_qc_status='PASS_WITH_WARNINGS') warnings, SUM(technical_qc_status='FAIL') fail FROM trials") or {}
    reviews = db.row("SELECT SUM(review_status='reviewed') reviewed, SUM(review_status='needs_second_review') needs_second_review FROM reviews") or {}
    return {**subject.model_dump(), "counts": counts, "reviews": reviews}


@app.get("/api/trials")
async def get_trials(queue: str = Query("all"), task: str | None = None,
               collection_session: str | None = None, qc_status: str | None = None,
               review_status: str | None = None, reviewed_condition: str | None = None,
               task_outcome: str | None = None, anomaly_family: str | None = None):
    db = Database(layout(_root()).database)
    clauses: list[str] = []; params: list[str] = []
    queue_map = {
        "unreviewed": "COALESCE(r.review_status,'unreviewed')='unreviewed'",
        "qc_warnings": "t.technical_qc_status='PASS_WITH_WARNINGS'", "qc_failures": "t.technical_qc_status='FAIL'",
        "needs_second_review": "r.review_status='needs_second_review'", "reviewed_anomalies": "r.condition_reviewed='anomaly'",
        "ambiguous": "r.condition_reviewed='ambiguous'",
    }
    if queue != "all" and queue in queue_map: clauses.append(queue_map[queue])
    if task: clauses.append("t.task_normalized=?"); params.append(task)
    if collection_session: clauses.append("t.collection_session_id=?"); params.append(collection_session)
    if qc_status: clauses.append("t.technical_qc_status=?"); params.append(qc_status)
    if review_status: clauses.append("COALESCE(r.review_status,'unreviewed')=?"); params.append(review_status)
    if reviewed_condition: clauses.append("r.condition_reviewed=?"); params.append(reviewed_condition)
    if task_outcome: clauses.append("r.task_outcome_reviewed=?"); params.append(task_outcome)
    if anomaly_family: clauses.append("r.primary_anomaly_reviewed=?"); params.append(anomaly_family)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return db.rows("SELECT t.trial_uid,t.task_normalized,t.collection_session_id,t.trial_directory_id,t.duration_sec,t.condition_acquired,t.task_outcome_acquired,t.technical_qc_status,t.technical_qc_reasons,t.usable_dual_view,t.usable_dual_rgbd,COALESCE(r.review_status,'unreviewed') review_status,r.condition_reviewed,r.task_outcome_reviewed FROM trials t LEFT JOIN reviews r USING(trial_uid)" + where + " ORDER BY t.task_normalized,t.collection_session_id,t.trial_directory_id", tuple(params))


@app.get("/api/trials/{trial_uid}")
async def get_trial(trial_uid: str):
    db = Database(layout(_root()).database)
    trial = db.row("SELECT * FROM trials WHERE trial_uid=?", (trial_uid,))
    if not trial: raise HTTPException(404, "Unknown trial")
    trial["topics"] = db.rows("SELECT * FROM topic_qc WHERE trial_uid=? ORDER BY topic_key", (trial_uid,))
    trial["phase_intervals"] = db.rows("SELECT phase,start_ns,end_ns FROM phase_intervals WHERE trial_uid=? ORDER BY start_ns", (trial_uid,))
    review = db.row("SELECT condition_reviewed,task_outcome_reviewed,primary_anomaly_reviewed,"
                    "secondary_consequence_reviewed FROM reviews WHERE trial_uid=?", (trial_uid,)) or {}
    trial["effective_values"] = {
        "condition": review.get("condition_reviewed") or trial.get("condition_acquired"),
        "task_outcome": review.get("task_outcome_reviewed") or trial.get("task_outcome_acquired"),
        "primary_anomaly": review.get("primary_anomaly_reviewed") or trial.get("primary_anomaly_acquired"),
        "secondary_consequence": review.get("secondary_consequence_reviewed") or trial.get("secondary_consequence_acquired"),
    }
    trial.pop("relative_mcap_path", None); trial.pop("relative_trial_path", None)
    return trial


@app.post("/api/trials/{trial_uid}/prepare", status_code=202)
async def prepare(trial_uid: str):
    try: return start_preview(_root(), trial_uid)
    except KeyError: raise HTTPException(404, "Unknown trial")
    except (RuntimeError, ValueError) as exc: raise HTTPException(422, str(exc))


@app.get("/api/trials/{trial_uid}/preview")
async def preview(trial_uid: str):
    try: return preview_state(_root(), trial_uid)
    except KeyError: raise HTTPException(404, "Unknown trial")


@app.get("/api/trials/{trial_uid}/frame")
async def frame(trial_uid: str, topic_key: Literal["camera1_rgb", "camera2_rgb"], t_ns: int = Query(ge=0)):
    try: path = nearest_frame(_root(), trial_uid, topic_key, t_ns)
    except (KeyError, FileNotFoundError): raise HTTPException(404, "Frame not available")
    media = "image/png" if path.suffix == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media)


@app.get("/api/trials/{trial_uid}/review")
async def get_review(trial_uid: str):
    try: return load_review(_root(), trial_uid)
    except KeyError: raise HTTPException(404, "Unknown trial")


@app.put("/api/trials/{trial_uid}/review")
async def put_review(trial_uid: str, review: Review):
    if review.trial_uid != trial_uid: raise HTTPException(400, "trial_uid does not match URL")
    try: return save_review(_root(), review)
    except KeyError: raise HTTPException(404, "Unknown trial")
    except ReviewConflict as exc: raise HTTPException(409, str(exc))
    except ValueError as exc: raise HTTPException(422, str(exc))


@app.post("/api/export")
async def export():
    return export_all(_root())


@app.get("/api/health")
async def health():
    return {"status": "ok"}
