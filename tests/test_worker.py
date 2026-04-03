"""Tests for the background worker cleaning pipeline."""

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

# Add parent dir to path so imports work
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import create_job, get_job, get_segments, init_db, update_job
from worker import Worker


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace with all required directories."""
    db_path = str(tmp_path / "test.db")
    jobs_dir = tmp_path / "jobs"
    uploads_dir = tmp_path / "uploads"
    ref_dir = tmp_path / "ref"
    jobs_dir.mkdir()
    uploads_dir.mkdir()
    ref_dir.mkdir()
    init_db(db_path)
    return {
        "db_path": db_path,
        "jobs_dir": jobs_dir,
        "uploads_dir": uploads_dir,
        "ref_dir": ref_dir,
    }


def _create_test_job(workspace, filename, content):
    """Create a job and write the upload file. Returns job_id."""
    db_path = workspace["db_path"]
    uploads_dir = workspace["uploads_dir"]

    job_id = create_job(db_path, filename=filename)
    update_job(db_path, job_id, status="cleaning")

    upload_path = uploads_dir / f"{job_id}_{filename}"
    upload_path.write_text(content, encoding="utf-8")
    return job_id


def test_cleaning_basic_txt(workspace):
    """Worker should extract, clean, chunk, and create segments from a .txt upload."""
    content = "Test Title\n\nThis is a paragraph of text for testing. It should be processed by the worker."
    job_id = _create_test_job(workspace, "sample.txt", content)

    worker = Worker(
        workspace["db_path"],
        workspace["jobs_dir"],
        workspace["uploads_dir"],
        workspace["ref_dir"],
    )

    job = get_job(workspace["db_path"], job_id)
    worker._process_cleaning(job)

    # Check job status changed to review
    job = get_job(workspace["db_path"], job_id)
    assert job["status"] == "review", f"Expected 'review', got '{job['status']}'"
    assert job["segments_total"] > 0

    # Check segments were created
    segments = get_segments(workspace["db_path"], job_id)
    assert len(segments) > 0
    assert len(segments) == job["segments_total"]

    # Check cleaned files exist
    cleaned_dir = workspace["jobs_dir"] / job_id / "cleaned"
    assert cleaned_dir.exists()
    cleaned_files = list(cleaned_dir.glob("part*.txt"))
    assert len(cleaned_files) > 0


def test_cleaning_multi_paragraph(workspace):
    """Multiple paragraphs should produce segments with correct indexing."""
    content = (
        "Chapter One\n\n"
        "First paragraph with enough text to be meaningful.\n\n"
        "Second paragraph continues the story here.\n\n"
        "Third paragraph wraps things up nicely."
    )
    job_id = _create_test_job(workspace, "multi.txt", content)

    worker = Worker(
        workspace["db_path"],
        workspace["jobs_dir"],
        workspace["uploads_dir"],
        workspace["ref_dir"],
    )

    job = get_job(workspace["db_path"], job_id)
    worker._process_cleaning(job)

    job = get_job(workspace["db_path"], job_id)
    assert job["status"] == "review"

    segments = get_segments(workspace["db_path"], job_id)
    # Should have multiple segments with continuous indexing
    indices = [s["seg_index"] for s in segments]
    assert indices == list(range(len(segments)))

    # Title segment should be flagged
    title_segs = [s for s in segments if s["is_title"] == 1]
    assert len(title_segs) >= 1, "Expected at least one title segment"


def test_cleaning_missing_upload(workspace):
    """Worker should set status to 'failed' when the upload file is missing."""
    db_path = workspace["db_path"]
    job_id = create_job(db_path, filename="ghost.txt")
    update_job(db_path, job_id, status="cleaning")
    # Don't create the upload file

    worker = Worker(
        workspace["db_path"],
        workspace["jobs_dir"],
        workspace["uploads_dir"],
        workspace["ref_dir"],
    )

    job = get_job(db_path, job_id)
    worker._process_cleaning(job)

    job = get_job(db_path, job_id)
    assert job["status"] == "failed"
    assert job["error_msg"] is not None
    assert "not found" in job["error_msg"].lower()


def test_cleaning_empty_file(workspace):
    """Worker should fail gracefully on an empty file."""
    job_id = _create_test_job(workspace, "empty.txt", "")

    worker = Worker(
        workspace["db_path"],
        workspace["jobs_dir"],
        workspace["uploads_dir"],
        workspace["ref_dir"],
    )

    job = get_job(workspace["db_path"], job_id)
    worker._process_cleaning(job)

    job = get_job(workspace["db_path"], job_id)
    assert job["status"] == "failed"
    assert job["error_msg"] is not None


def test_cancel_job(workspace):
    """cancel_job should set the cancel event when job_id matches."""
    worker = Worker(
        workspace["db_path"],
        workspace["jobs_dir"],
        workspace["uploads_dir"],
        workspace["ref_dir"],
    )

    # When no job is running, cancel should be a no-op
    worker.cancel_job("nonexistent")
    assert not worker._cancel_event.is_set()

    # Simulate a running job
    worker._current_job_id = "abc123"
    worker.cancel_job("abc123")
    assert worker._cancel_event.is_set()

    # Wrong job id should not trigger
    worker._cancel_event.clear()
    worker._current_job_id = "abc123"
    worker.cancel_job("xyz789")
    assert not worker._cancel_event.is_set()


def test_friendly_error_messages():
    """Static method should map exceptions to user-friendly messages."""
    assert "memory" in Worker._friendly_error(RuntimeError("CUDA out of memory")).lower()
    assert "not found" in Worker._friendly_error(FileNotFoundError("No such file: test.txt")).lower()
    assert "extract" in Worker._friendly_error(ValueError("No extractor for .xyz")).lower()
    assert "something went wrong" in Worker._friendly_error(RuntimeError("unknown error")).lower()


def test_worker_poll_finds_cleaning_job(workspace):
    """_poll_for_work should find and process a job with status 'cleaning'."""
    content = "Poll test content paragraph."
    job_id = _create_test_job(workspace, "poll.txt", content)

    worker = Worker(
        workspace["db_path"],
        workspace["jobs_dir"],
        workspace["uploads_dir"],
        workspace["ref_dir"],
    )

    worker._poll_for_work()

    job = get_job(workspace["db_path"], job_id)
    assert job["status"] == "review"


def test_activity_logged(workspace):
    """Worker should log activity entries for cleaning start and completion."""
    content = "Activity log test paragraph."
    job_id = _create_test_job(workspace, "activity.txt", content)

    worker = Worker(
        workspace["db_path"],
        workspace["jobs_dir"],
        workspace["uploads_dir"],
        workspace["ref_dir"],
    )

    job = get_job(workspace["db_path"], job_id)
    worker._process_cleaning(job)

    from activity import read_activity
    activities = read_activity(workspace["jobs_dir"] / job_id)
    events = [a["event"] for a in activities]
    assert "clean_start" in events
    assert "clean_done" in events
