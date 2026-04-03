import tempfile
import os
import time
import pytest
from pathlib import Path

# Import will fail until db.py is implemented — that's expected at first
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from db import (
    init_db,
    create_job,
    get_job,
    update_job,
    list_jobs,
    create_segment,
    get_segments,
    update_segment,
    delete_job_cascade,
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def test_create_and_get_job(db_path):
    job_id = create_job(db_path, "test.pdf")
    assert job_id is not None
    assert len(job_id) == 12  # uuid hex[:12]

    job = get_job(db_path, job_id)
    assert job is not None
    assert job["id"] == job_id
    assert job["filename"] == "test.pdf"
    assert job["status"] == "pending"
    assert job["speed"] == pytest.approx(0.85)
    assert job["voice_ref"] == "amditis.wav"
    assert job["title_pause"] == pytest.approx(2.0)
    assert job["para_pause"] == pytest.approx(0.75)
    assert job["progress_pct"] == 0
    assert job["segments_done"] == 0
    assert job["segments_total"] == 0
    assert job["final_audio"] is None
    assert job["error_msg"] is None


def test_get_job_returns_none_for_missing(db_path):
    result = get_job(db_path, "doesnotexist")
    assert result is None


def test_create_job_with_custom_params(db_path):
    job_id = create_job(db_path, "book.docx", speed=1.0, voice_ref="other.wav", title_pause=3.0, para_pause=1.0)
    job = get_job(db_path, job_id)
    assert job["speed"] == pytest.approx(1.0)
    assert job["voice_ref"] == "other.wav"
    assert job["title_pause"] == pytest.approx(3.0)
    assert job["para_pause"] == pytest.approx(1.0)


def test_update_job_status(db_path):
    job_id = create_job(db_path, "file.pdf")
    update_job(db_path, job_id, status="running")
    job = get_job(db_path, job_id)
    assert job["status"] == "running"


def test_update_job_multiple_fields(db_path):
    job_id = create_job(db_path, "file.pdf")
    update_job(db_path, job_id, status="done", progress_pct=100, segments_done=5, segments_total=5, final_audio="output.wav")
    job = get_job(db_path, job_id)
    assert job["status"] == "done"
    assert job["progress_pct"] == 100
    assert job["segments_done"] == 5
    assert job["segments_total"] == 5
    assert job["final_audio"] == "output.wav"


def test_update_job_error_fields(db_path):
    job_id = create_job(db_path, "file.pdf")
    update_job(db_path, job_id, status="error", error_msg="synthesis failed", error_detail="traceback here")
    job = get_job(db_path, job_id)
    assert job["status"] == "error"
    assert job["error_msg"] == "synthesis failed"
    assert job["error_detail"] == "traceback here"


def test_list_jobs_newest_first(db_path):
    id1 = create_job(db_path, "first.pdf")
    # Small sleep to ensure distinct timestamps
    time.sleep(0.01)
    id2 = create_job(db_path, "second.pdf")
    time.sleep(0.01)
    id3 = create_job(db_path, "third.pdf")

    jobs = list_jobs(db_path)
    assert len(jobs) == 3
    # Newest first
    assert jobs[0]["id"] == id3
    assert jobs[1]["id"] == id2
    assert jobs[2]["id"] == id1


def test_list_jobs_limit(db_path):
    for i in range(5):
        create_job(db_path, f"file{i}.pdf")
    jobs = list_jobs(db_path, limit=3)
    assert len(jobs) == 3


def test_list_jobs_empty(db_path):
    jobs = list_jobs(db_path)
    assert jobs == []


def test_create_and_get_segments(db_path):
    job_id = create_job(db_path, "file.pdf")
    create_segment(db_path, job_id, seg_index=0, source_text="First paragraph.", is_title=0, pause_after=0.75)
    create_segment(db_path, job_id, seg_index=1, source_text="Chapter One", is_title=1, pause_after=2.0)

    segments = get_segments(db_path, job_id)
    assert len(segments) == 2

    assert segments[0]["seg_index"] == 0
    assert segments[0]["source_text"] == "First paragraph."
    assert segments[0]["is_title"] == 0
    assert segments[0]["pause_after"] == pytest.approx(0.75)
    assert segments[0]["audio_file"] is None

    assert segments[1]["seg_index"] == 1
    assert segments[1]["source_text"] == "Chapter One"
    assert segments[1]["is_title"] == 1
    assert segments[1]["pause_after"] == pytest.approx(2.0)


def test_get_segments_ordered_by_index(db_path):
    job_id = create_job(db_path, "file.pdf")
    # Insert out of order
    create_segment(db_path, job_id, seg_index=2, source_text="Third")
    create_segment(db_path, job_id, seg_index=0, source_text="First")
    create_segment(db_path, job_id, seg_index=1, source_text="Second")

    segments = get_segments(db_path, job_id)
    assert [s["seg_index"] for s in segments] == [0, 1, 2]


def test_get_segments_empty(db_path):
    job_id = create_job(db_path, "file.pdf")
    segments = get_segments(db_path, job_id)
    assert segments == []


def test_update_segment(db_path):
    job_id = create_job(db_path, "file.pdf")
    create_segment(db_path, job_id, seg_index=0, source_text="Hello world.")

    update_segment(db_path, job_id, 0, audio_file="seg_000.wav", accuracy=0.97, duration_seconds=3.5, user_edited=1)

    segments = get_segments(db_path, job_id)
    seg = segments[0]
    assert seg["audio_file"] == "seg_000.wav"
    assert seg["accuracy"] == pytest.approx(0.97)
    assert seg["duration_seconds"] == pytest.approx(3.5)
    assert seg["user_edited"] == 1


def test_update_segment_word_timings(db_path):
    job_id = create_job(db_path, "file.pdf")
    create_segment(db_path, job_id, seg_index=0, source_text="Hello.")
    timings = '[{"word": "Hello", "start": 0.0, "end": 0.5}]'
    update_segment(db_path, job_id, 0, word_timings_json=timings)

    segments = get_segments(db_path, job_id)
    assert segments[0]["word_timings_json"] == timings


def test_delete_job_cascade(db_path):
    job_id = create_job(db_path, "file.pdf")
    create_segment(db_path, job_id, seg_index=0, source_text="Para one.")
    create_segment(db_path, job_id, seg_index=1, source_text="Para two.")

    # Confirm they exist
    assert get_job(db_path, job_id) is not None
    assert len(get_segments(db_path, job_id)) == 2

    delete_job_cascade(db_path, job_id)

    # Both job and segments should be gone
    assert get_job(db_path, job_id) is None
    assert get_segments(db_path, job_id) == []


def test_delete_job_only_removes_own_segments(db_path):
    job1 = create_job(db_path, "file1.pdf")
    job2 = create_job(db_path, "file2.pdf")
    create_segment(db_path, job1, seg_index=0, source_text="Job 1 seg.")
    create_segment(db_path, job2, seg_index=0, source_text="Job 2 seg.")

    delete_job_cascade(db_path, job1)

    assert get_job(db_path, job1) is None
    assert get_segments(db_path, job1) == []
    assert get_job(db_path, job2) is not None
    assert len(get_segments(db_path, job2)) == 1


def test_segment_chunk_file_default(db_path):
    job_id = create_job(db_path, "file.pdf")
    create_segment(db_path, job_id, seg_index=0, source_text="Hello.")
    segments = get_segments(db_path, job_id)
    assert segments[0]["chunk_file"] == ""


def test_segment_chunk_file_custom(db_path):
    job_id = create_job(db_path, "file.pdf")
    create_segment(db_path, job_id, seg_index=0, chunk_file="chunk_000.txt", source_text="Hello.")
    segments = get_segments(db_path, job_id)
    assert segments[0]["chunk_file"] == "chunk_000.txt"
