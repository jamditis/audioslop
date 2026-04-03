"""Tests for worker API endpoints."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Set the worker key before importing app so WORKER_API_KEY is captured at module load.
os.environ["AUDIOSLOP_WORKER_KEY"] = "test-worker-key"

import app as app_module
from db import create_job, create_segment, init_db, update_job

AUTH = {"Authorization": "Bearer test-worker-key"}
WRONG_AUTH = {"Authorization": "Bearer wrong-key"}


@pytest.fixture()
def client(tmp_path):
    """Flask test client with a temp DB and patched DB_PATH / JOBS_DIR."""
    db_path = str(tmp_path / "test.db")
    jobs_dir = tmp_path / "jobs"
    ref_dir = tmp_path / "ref"
    jobs_dir.mkdir()
    ref_dir.mkdir()
    init_db(db_path)

    with patch.object(app_module, "DB_PATH", db_path), \
         patch.object(app_module, "JOBS_DIR", jobs_dir), \
         patch.object(app_module, "REF_DIR", ref_dir):
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, db_path, jobs_dir, ref_dir


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

class TestWorkerAuth:
    def test_no_auth_returns_401(self, client):
        c, db_path, *_ = client
        resp = c.get("/api/worker/jobs")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, client):
        c, db_path, *_ = client
        resp = c.get("/api/worker/jobs", headers=WRONG_AUTH)
        assert resp.status_code == 401

    def test_correct_key_passes(self, client):
        c, db_path, *_ = client
        resp = c.get("/api/worker/jobs", headers=AUTH)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/worker/jobs
# ---------------------------------------------------------------------------

class TestWorkerListJobs:
    def test_returns_empty_list_when_no_jobs(self, client):
        c, db_path, *_ = client
        resp = c.get("/api/worker/jobs?status=synthesizing", headers=AUTH)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_filters_by_status(self, client):
        c, db_path, *_ = client
        j1 = create_job(db_path, "a.txt")
        j2 = create_job(db_path, "b.txt")
        update_job(db_path, j1, status="synthesizing")
        update_job(db_path, j2, status="done")

        resp = c.get("/api/worker/jobs?status=synthesizing", headers=AUTH)
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["id"] == j1

    def test_returns_expected_fields(self, client):
        c, db_path, *_ = client
        j1 = create_job(db_path, "book.txt", speed=0.9, voice_ref="voice.wav",
                         title_pause=2.0, para_pause=0.75)
        update_job(db_path, j1, status="synthesizing", segments_total=5)

        resp = c.get("/api/worker/jobs?status=synthesizing", headers=AUTH)
        job = resp.get_json()[0]

        for field in ("id", "filename", "speed", "voice_ref", "title_pause",
                      "para_pause", "segments_total"):
            assert field in job, f"Missing field: {field}"

    def test_oldest_first_ordering(self, client):
        c, db_path, *_ = client
        j1 = create_job(db_path, "first.txt")
        j2 = create_job(db_path, "second.txt")
        update_job(db_path, j1, status="synthesizing")
        update_job(db_path, j2, status="synthesizing")

        resp = c.get("/api/worker/jobs?status=synthesizing", headers=AUTH)
        data = resp.get_json()
        assert data[0]["id"] == j1
        assert data[1]["id"] == j2

    def test_no_status_param_returns_all(self, client):
        c, db_path, *_ = client
        j1 = create_job(db_path, "a.txt")
        j2 = create_job(db_path, "b.txt")
        update_job(db_path, j1, status="synthesizing")
        update_job(db_path, j2, status="done")

        resp = c.get("/api/worker/jobs", headers=AUTH)
        data = resp.get_json()
        assert len(data) == 2


# ---------------------------------------------------------------------------
# GET /api/worker/job/<id>/segments
# ---------------------------------------------------------------------------

class TestWorkerGetSegments:
    def test_returns_segments(self, client):
        c, db_path, *_ = client
        job_id = create_job(db_path, "book.txt")
        create_segment(db_path, job_id, 0, source_text="Hello world.", is_title=1, pause_after=2.0)
        create_segment(db_path, job_id, 1, source_text="Some text.", pause_after=0.5)

        resp = c.get(f"/api/worker/job/{job_id}/segments", headers=AUTH)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2

    def test_returns_expected_fields(self, client):
        c, db_path, *_ = client
        job_id = create_job(db_path, "book.txt")
        create_segment(db_path, job_id, 0, source_text="Hello.", is_title=0, pause_after=0.75)

        resp = c.get(f"/api/worker/job/{job_id}/segments", headers=AUTH)
        seg = resp.get_json()[0]

        for field in ("seg_index", "source_text", "is_title", "pause_after", "audio_file"):
            assert field in seg, f"Missing field: {field}"

    def test_404_for_unknown_job(self, client):
        c, *_ = client
        resp = c.get("/api/worker/job/doesnotexist/segments", headers=AUTH)
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        c, db_path, *_ = client
        job_id = create_job(db_path, "book.txt")
        resp = c.get(f"/api/worker/job/{job_id}/segments")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/worker/ref/<filename>
# ---------------------------------------------------------------------------

class TestWorkerRefFile:
    def test_serves_ref_file(self, client):
        c, db_path, jobs_dir, ref_dir = client
        ref_file = ref_dir / "voice.wav"
        ref_file.write_bytes(b"RIFF fake wav data")

        resp = c.get("/api/worker/ref/voice.wav", headers=AUTH)
        assert resp.status_code == 200
        assert resp.data == b"RIFF fake wav data"

    def test_404_for_missing_file(self, client):
        c, *_ = client
        resp = c.get("/api/worker/ref/missing.wav", headers=AUTH)
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        c, db_path, jobs_dir, ref_dir = client
        ref_file = ref_dir / "voice.wav"
        ref_file.write_bytes(b"data")
        resp = c.get("/api/worker/ref/voice.wav")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/worker/job/<id>/segment/<idx>/complete
# ---------------------------------------------------------------------------

class TestWorkerSegmentComplete:
    def test_updates_segment(self, client):
        c, db_path, jobs_dir, *_ = client
        job_id = create_job(db_path, "book.txt")
        update_job(db_path, job_id, segments_total=2, status="synthesizing")
        create_segment(db_path, job_id, 0, source_text="Hello.")
        create_segment(db_path, job_id, 1, source_text="World.")

        (jobs_dir / job_id).mkdir(parents=True, exist_ok=True)

        payload = {
            "audio_r2_key": "jobs/abc/audio/seg_000.wav",
            "accuracy": 0.98,
            "duration_seconds": 3.5,
            "word_timings": [{"word": "Hello", "start": 0.0, "end": 0.5}],
        }
        resp = c.post(
            f"/api/worker/job/{job_id}/segment/0/complete",
            data=json.dumps(payload),
            content_type="application/json",
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_updates_job_progress(self, client):
        from db import get_job, get_segments
        c, db_path, jobs_dir, *_ = client
        job_id = create_job(db_path, "book.txt")
        update_job(db_path, job_id, segments_total=4, status="synthesizing")
        create_segment(db_path, job_id, 0, source_text="Seg 0.")

        (jobs_dir / job_id).mkdir(parents=True, exist_ok=True)

        payload = {
            "audio_r2_key": "jobs/abc/audio/seg_000.wav",
            "accuracy": 0.95,
            "duration_seconds": 2.0,
            "word_timings": [],
        }
        c.post(
            f"/api/worker/job/{job_id}/segment/0/complete",
            data=json.dumps(payload),
            content_type="application/json",
            headers=AUTH,
        )

        job = get_job(db_path, job_id)
        assert job["segments_done"] == 1
        assert job["progress_pct"] == 25  # 1/4

    def test_404_for_unknown_job(self, client):
        c, *_ = client
        resp = c.post(
            "/api/worker/job/doesnotexist/segment/0/complete",
            data=json.dumps({}),
            content_type="application/json",
            headers=AUTH,
        )
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        c, db_path, jobs_dir, *_ = client
        job_id = create_job(db_path, "book.txt")
        resp = c.post(
            f"/api/worker/job/{job_id}/segment/0/complete",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/worker/job/<id>/complete
# ---------------------------------------------------------------------------

class TestWorkerJobComplete:
    def test_marks_job_done(self, client):
        from db import get_job
        c, db_path, jobs_dir, *_ = client
        job_id = create_job(db_path, "book.txt")
        update_job(db_path, job_id, status="synthesizing")
        (jobs_dir / job_id).mkdir(parents=True, exist_ok=True)

        payload = {"final_audio": "jobs/abc/audio/full.wav"}
        resp = c.post(
            f"/api/worker/job/{job_id}/complete",
            data=json.dumps(payload),
            content_type="application/json",
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        job = get_job(db_path, job_id)
        assert job["status"] == "done"
        assert job["progress_pct"] == 100
        assert job["final_audio"] == "jobs/abc/audio/full.wav"

    def test_404_for_unknown_job(self, client):
        c, *_ = client
        resp = c.post(
            "/api/worker/job/doesnotexist/complete",
            data=json.dumps({}),
            content_type="application/json",
            headers=AUTH,
        )
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        c, db_path, *_ = client
        job_id = create_job(db_path, "book.txt")
        resp = c.post(
            f"/api/worker/job/{job_id}/complete",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/worker/job/<id>/fail
# ---------------------------------------------------------------------------

class TestWorkerJobFail:
    def test_marks_job_failed(self, client):
        from db import get_job
        c, db_path, jobs_dir, *_ = client
        job_id = create_job(db_path, "book.txt")
        update_job(db_path, job_id, status="synthesizing")
        (jobs_dir / job_id).mkdir(parents=True, exist_ok=True)

        payload = {
            "error_msg": "CUDA out of memory",
            "error_detail": "RuntimeError: CUDA out of memory at line 42",
        }
        resp = c.post(
            f"/api/worker/job/{job_id}/fail",
            data=json.dumps(payload),
            content_type="application/json",
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        job = get_job(db_path, job_id)
        assert job["status"] == "failed"
        assert job["error_msg"] == "CUDA out of memory"
        assert job["error_detail"] == "RuntimeError: CUDA out of memory at line 42"

    def test_404_for_unknown_job(self, client):
        c, *_ = client
        resp = c.post(
            "/api/worker/job/doesnotexist/fail",
            data=json.dumps({}),
            content_type="application/json",
            headers=AUTH,
        )
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        c, db_path, *_ = client
        job_id = create_job(db_path, "book.txt")
        resp = c.post(
            f"/api/worker/job/{job_id}/fail",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/worker/heartbeat
# ---------------------------------------------------------------------------

class TestWorkerHeartbeat:
    def test_heartbeat_updates_status(self, client):
        c, *_ = client
        payload = {"gpu": "RTX 4080 Super", "job_id": "abc123", "progress": 45}
        resp = c.post(
            "/api/worker/heartbeat",
            data=json.dumps(payload),
            content_type="application/json",
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_heartbeat_stores_data_with_last_seen(self, client):
        c, *_ = client
        payload = {"gpu": "RTX 4080 Super", "job_id": "abc123"}
        c.post(
            "/api/worker/heartbeat",
            data=json.dumps(payload),
            content_type="application/json",
            headers=AUTH,
        )
        # Check via admin endpoint (need admin session)
        # Instead, verify via the module-level dict directly
        assert "gpu" in app_module._worker_status
        assert "last_seen" in app_module._worker_status

    def test_requires_auth(self, client):
        c, *_ = client
        resp = c.post(
            "/api/worker/heartbeat",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 401
