from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx

from hri_curator.scanner import scan


def test_web_workspace_replaces_empty_state():
    web = Path(__file__).parents[1] / "hri_curator" / "web"
    css = (web / "style.css").read_text(encoding="utf-8")
    javascript = (web / "app.js").read_text(encoding="utf-8")

    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;\s*\}", css)
    assert "showWorkspaceMessage('Loading trial...',true)" in javascript
    assert "$('empty').hidden=true;$('review').hidden=false" in javascript


def test_curator_api_hides_paths_and_saves_review(subject: Path, monkeypatch):
    scan(subject)
    monkeypatch.setenv("HRI_CURATOR_ROOT", str(subject))
    from hri_curator.webapp import app
    async def exercise():
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        summary = await client.get("/api/subject")
        assert summary.status_code == 200 and summary.json()["subject_id"] == "S001"
        trials = (await client.get("/api/trials?queue=all")).json()
        assert len(trials) == 1
        uid = trials[0]["trial_uid"]
        detail = (await client.get(f"/api/trials/{uid}")).json()
        assert "relative_trial_path" not in detail and "relative_mcap_path" not in detail
        review = (await client.get(f"/api/trials/{uid}/review")).json()
        review.update({"review_status": "reviewed", "condition_reviewed": "normal", "task_outcome_reviewed": "success", "semantic_validity": "valid", "anomaly_present": False, "usable_for_normal_training": True, "usable_for_anomaly_evaluation": False})
        saved = await client.put(f"/api/trials/{uid}/review", json=review)
        assert saved.status_code == 200 and saved.json()["review_status"] == "reviewed"
        assert (await client.get("/api/trials/not-a-real-trial")).status_code == 404
    asyncio.run(exercise())


def test_prepare_reports_background_failure_outside_container(subject: Path, monkeypatch):
    scan(subject)
    monkeypatch.setenv("HRI_CURATOR_ROOT", str(subject))
    from hri_curator.webapp import app
    async def exercise():
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        uid = (await client.get("/api/trials?queue=all")).json()[0]["trial_uid"]
        response = await client.post(f"/api/trials/{uid}/prepare")
        assert response.status_code == 202
        for _ in range(100):
            state = (await client.get(f"/api/trials/{uid}/preview")).json()
            if state["status"] == "failed": break
            await asyncio.sleep(0.01)
        assert state["status"] == "failed"
        assert "ROS 2 Jazzy container" in state["error"]
    asyncio.run(exercise())


def test_api_rejects_stale_review(subject: Path, monkeypatch):
    scan(subject); monkeypatch.setenv("HRI_CURATOR_ROOT", str(subject))
    from hri_curator.webapp import app
    async def exercise():
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        uid = (await client.get("/api/trials?queue=all")).json()[0]["trial_uid"]
        first = (await client.get(f"/api/trials/{uid}/review")).json()
        stale = dict(first)
        first["review_status"] = "in_progress"
        assert (await client.put(f"/api/trials/{uid}/review", json=first)).status_code == 200
        stale["review_status"] = "in_progress"
        assert (await client.put(f"/api/trials/{uid}/review", json=stale)).status_code == 409
    asyncio.run(exercise())
