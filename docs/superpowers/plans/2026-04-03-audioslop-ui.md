# Audioslop web UI implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Flask web app wrapping the audioslop document-to-audiobook pipeline with upload, text review, and a synced transcript audio player.

**Architecture:** Flask + vanilla JS + Tailwind CDN + SQLite. Background worker thread processes jobs using existing pipeline scripts. Player syncs word-level highlights to audio playback using Whisper timestamps.

**Tech Stack:** Python 3.10+, Flask, SQLite, vanilla JS, Tailwind CSS (CDN), existing audioslop/synthesize/qa pipeline.

**Spec:** `docs/superpowers/specs/2026-04-03-audioslop-ui-design.md`

---

### File map

| File | Responsibility | Task |
|------|---------------|------|
| `db.py` (create) | SQLite schema, CRUD helpers for jobs and segments | 1 |
| `activity.py` (create) | Per-job activity logging to JSONL files | 1 |
| `app.py` (create) | Flask app, all routes, auth | 2, 3, 5, 7 |
| `worker.py` (create) | Background thread, cleaning + synthesis + timing | 4, 6 |
| `templates/base.html` (create) | Layout shell, nav, Tailwind CDN | 2 |
| `templates/login.html` (create) | Login page | 2 |
| `templates/upload.html` (create) | Upload + job list page | 3 |
| `templates/review.html` (create) | Text review + synthesis progress | 5 |
| `templates/player.html` (create) | Audio player + transcript | 7 |
| `static/app.css` (create) | Custom styles | 3 |
| `static/player.js` (create) | Sync engine, audio controls | 7 |
| `synthesize.py` (modify) | Extract synthesis functions for worker import | 6 |
| `qa.py` (modify) | Extract timing functions for worker import | 6 |

---

### Task 1: Database and activity logging

**Files:**
- Create: `db.py`
- Create: `activity.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for database layer**

Create `tests/test_db.py`:

```python
import os
import tempfile
import pytest
from db import init_db, create_job, get_job, update_job, list_jobs, \
    create_segment, get_segments, update_segment, delete_job_cascade


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.unlink(path)


def test_create_and_get_job(db_path):
    job_id = create_job(db_path, filename="test.docx", speed=0.85,
                        voice_ref="voice.wav", title_pause=2.0, para_pause=0.75)
    job = get_job(db_path, job_id)
    assert job["filename"] == "test.docx"
    assert job["status"] == "pending"
    assert job["speed"] == 0.85


def test_update_job_status(db_path):
    job_id = create_job(db_path, filename="test.docx")
    update_job(db_path, job_id, status="cleaning", progress_pct=10)
    job = get_job(db_path, job_id)
    assert job["status"] == "cleaning"
    assert job["progress_pct"] == 10


def test_list_jobs_ordered(db_path):
    create_job(db_path, filename="first.docx")
    create_job(db_path, filename="second.docx")
    jobs = list_jobs(db_path)
    assert len(jobs) == 2
    assert jobs[0]["filename"] == "second.docx"  # newest first


def test_create_and_get_segments(db_path):
    job_id = create_job(db_path, filename="test.docx")
    create_segment(db_path, job_id=job_id, seg_index=0, chunk_file="ch1.txt",
                   source_text="Hello world", is_title=0, pause_after=0.75)
    create_segment(db_path, job_id=job_id, seg_index=1, chunk_file="ch1.txt",
                   source_text="Chapter One", is_title=1, pause_after=2.0)
    segs = get_segments(db_path, job_id)
    assert len(segs) == 2
    assert segs[0]["source_text"] == "Hello world"
    assert segs[1]["is_title"] == 1


def test_update_segment(db_path):
    job_id = create_job(db_path, filename="test.docx")
    create_segment(db_path, job_id=job_id, seg_index=0, chunk_file="ch1.txt",
                   source_text="Original text")
    update_segment(db_path, job_id, seg_index=0,
                   source_text="Edited text", user_edited=1)
    segs = get_segments(db_path, job_id)
    assert segs[0]["source_text"] == "Edited text"
    assert segs[0]["user_edited"] == 1


def test_delete_job_cascade(db_path):
    job_id = create_job(db_path, filename="test.docx")
    create_segment(db_path, job_id=job_id, seg_index=0, chunk_file="ch1.txt",
                   source_text="text")
    delete_job_cascade(db_path, job_id)
    assert get_job(db_path, job_id) is None
    assert get_segments(db_path, job_id) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\amdit\OneDrive\Desktop\Crimes\playground\audioslop && python -m pytest tests/test_db.py -v`

Expected: ModuleNotFoundError for `db`

- [ ] **Step 3: Implement db.py**

Create `db.py`:

```python
import sqlite3
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    speed REAL DEFAULT 0.85,
    voice_ref TEXT DEFAULT 'amditis.wav',
    title_pause REAL DEFAULT 2.0,
    para_pause REAL DEFAULT 0.75,
    progress_pct INTEGER DEFAULT 0,
    segments_done INTEGER DEFAULT 0,
    segments_total INTEGER DEFAULT 0,
    final_audio TEXT,
    error_msg TEXT,
    error_detail TEXT
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seg_index INTEGER NOT NULL,
    chunk_file TEXT NOT NULL DEFAULT '',
    source_text TEXT NOT NULL,
    user_edited INTEGER DEFAULT 0,
    audio_file TEXT,
    word_timings_json TEXT,
    accuracy REAL,
    duration_seconds REAL,
    pause_after REAL DEFAULT 0.0,
    is_title INTEGER DEFAULT 0
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


def create_job(db_path: str, filename: str, speed: float = 0.85,
               voice_ref: str = "amditis.wav", title_pause: float = 2.0,
               para_pause: float = 0.75) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (id, filename, speed, voice_ref, title_pause, para_pause) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, filename, speed, voice_ref, title_pause, para_pause),
        )
    return job_id


def get_job(db_path: str, job_id: str) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def update_job(db_path: str, job_id: str, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)


def list_jobs(db_path: str, limit: int = 50) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def create_segment(db_path: str, job_id: str, seg_index: int,
                   chunk_file: str = "", source_text: str = "",
                   is_title: int = 0, pause_after: float = 0.0) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO segments (job_id, seg_index, chunk_file, source_text, "
            "is_title, pause_after) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, seg_index, chunk_file, source_text, is_title, pause_after),
        )


def get_segments(db_path: str, job_id: str) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM segments WHERE job_id = ? ORDER BY seg_index",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_segment(db_path: str, job_id: str, seg_index: int, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id, seg_index]
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE segments SET {set_clause} WHERE job_id = ? AND seg_index = ?",
            values,
        )


def delete_job_cascade(db_path: str, job_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_db.py -v`

Expected: All 6 tests PASS

- [ ] **Step 5: Implement activity.py**

Create `activity.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path


def log_activity(job_dir: Path, event: str, msg: str, **extra) -> None:
    """Append a timestamped event to the job's activity log."""
    job_dir.mkdir(parents=True, exist_ok=True)
    log_file = job_dir / "activity.jsonl"
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "event": event,
        "msg": msg,
        **extra,
    }
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_activity(job_dir: Path) -> list[dict]:
    """Read all activity entries for a job."""
    log_file = job_dir / "activity.jsonl"
    if not log_file.exists():
        return []
    entries = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries
```

- [ ] **Step 6: Commit**

```bash
git add db.py activity.py tests/test_db.py
git commit -m "feat: add database layer and activity logging"
```

---

### Task 2: Flask app skeleton + auth

**Files:**
- Create: `app.py`
- Create: `templates/base.html`
- Create: `templates/login.html`

- [ ] **Step 1: Create base.html template**

Create `templates/base.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}audioslop{% endblock %}</title>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>a</text></svg>">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
    {% block head %}{% endblock %}
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen">
    <nav class="border-b border-gray-800 px-6 py-3 flex items-center justify-between">
        <a href="/" class="text-lg font-semibold tracking-tight text-gray-100">audioslop</a>
        {% if session.get('authed') %}
        <span class="text-sm text-gray-500">
            <a href="/logout" class="hover:text-gray-300">log out</a>
        </span>
        {% endif %}
    </nav>
    <main class="max-w-5xl mx-auto px-6 py-8">
        {% block content %}{% endblock %}
    </main>
    {% block scripts %}{% endblock %}
</body>
</html>
```

- [ ] **Step 2: Create login.html template**

Create `templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}Log in - audioslop{% endblock %}
{% block content %}
<div class="max-w-sm mx-auto mt-20">
    <h1 class="text-2xl font-semibold mb-6">Log in</h1>
    {% if error %}
    <p class="text-red-400 text-sm mb-4">{{ error }}</p>
    {% endif %}
    <form method="POST" action="/login">
        <input type="password" name="password" placeholder="Password"
               class="w-full px-4 py-2 bg-gray-900 border border-gray-700 rounded-lg
                      text-gray-100 placeholder-gray-500 focus:outline-none
                      focus:border-gray-500 mb-4"
               autofocus>
        <button type="submit"
                class="w-full px-4 py-2 bg-gray-100 text-gray-900 rounded-lg
                       font-medium hover:bg-gray-200 transition-colors">
            Log in
        </button>
    </form>
</div>
{% endblock %}
```

- [ ] **Step 3: Create app.py with auth routes and all API endpoints**

Create `app.py` with the full route set. See spec for all routes. The app includes:
- Auth routes (`/login`, `/logout`)
- Page routes (`/`, `/job/<id>`, `/job/<id>/player`)
- API routes (upload, status, segments, audio serving, voice management, delete, cancel)
- Worker startup on `__main__`

Key implementation details:
- `require_auth` decorator checks `session["authed"]`
- `AUDIOSLOP_PASSWORD` env var, defaults to `"audioslop"`
- `use_reloader=False` to prevent double worker startup in debug mode
- All file paths relative to `BASE_DIR = Path(__file__).parent`

- [ ] **Step 4: Create static/app.css**

Create `static/app.css` with badge styles, word highlighting styles, transcript scroll container, drop zone styles, and segment dividers. See plan Task 2 Step 4 from the original draft above for the full CSS.

- [ ] **Step 5: Verify Flask app starts**

Run: `cd C:\Users\amdit\OneDrive\Desktop\Crimes\playground\audioslop && python app.py`

Expected: Flask dev server starts on port 5000. Visit `http://localhost:5000/` -- should redirect to `/login`. Enter "audioslop" -- should redirect to `/`.

- [ ] **Step 6: Commit**

```bash
git add app.py static/app.css templates/base.html templates/login.html
git commit -m "feat: flask app skeleton with auth"
```

---

### Task 3: Upload page

**Files:**
- Create: `templates/upload.html`

- [ ] **Step 1: Create upload.html**

Create `templates/upload.html` with:
- Drag-and-drop zone (click to browse or drag files)
- Settings panel: speed slider (0.5-1.5, default 0.85), voice dropdown, title/paragraph pause sliders
- Each slider shows its current value
- "Process" button (disabled until file selected)
- Job list table below with status badges, Play/Review/Delete links
- Upload via `fetch('/api/upload', { method: 'POST', body: formData })`, redirect to review page on success
- Delete via `fetch('/api/job/' + id, { method: 'DELETE' })`, reload page

- [ ] **Step 2: Verify upload page loads and accepts files**

Run: `python app.py`

Visit `http://localhost:5000/`. Upload a small .txt file. Verify it redirects to `/job/{id}`.

- [ ] **Step 3: Commit**

```bash
git add templates/upload.html
git commit -m "feat: upload page with drag-and-drop and settings"
```

---

### Task 4: Worker - cleaning step

**Files:**
- Create: `worker.py`
- Modify: `app.py` (start worker thread on startup)

- [ ] **Step 1: Create worker.py with cleaning pipeline**

Create `worker.py` with a `Worker` class that:
- Runs a daemon thread polling every 2 seconds for jobs with status `cleaning` or `synthesizing`
- `_process_cleaning(job)`: finds uploaded file, runs `EXTRACTORS[ext]()` + `clean_for_tts()` + `chunk_text()` from `audioslop.py`, then `split_into_segments()` from `synthesize.py` to create segment rows in the database. Sets status to `review`.
- `_process_synthesis(job)`: placeholder (implemented in Task 6)
- `cancel_job(job_id)`: sets a threading.Event to interrupt current job
- `_friendly_error(e)`: maps exception types to plain-language messages
- Uses `logging.handlers.RotatingFileHandler` for `audioslop.log`
- Calls `log_activity()` for every significant event

- [ ] **Step 2: Wire worker into app.py**

Add `get_worker()` function and call it from `__main__`. Add `/api/job/{id}/synthesize` and `/api/job/{id}/cancel` routes.

- [ ] **Step 3: Test cleaning step**

Upload a document, verify status changes from `cleaning` to `review`, verify segment rows appear in the database, verify `activity.jsonl` has entries.

- [ ] **Step 4: Commit**

```bash
git add worker.py app.py
git commit -m "feat: background worker with cleaning step"
```

---

### Task 5: Review page

**Files:**
- Create: `templates/review.html`

- [ ] **Step 1: Create review.html**

Create `templates/review.html` with:
- Job header (filename + status badge)
- Status message bar (plain language, color-coded per status, error detail toggle for failed jobs)
- Progress bar (visible during synthesis, shows segment N of M)
- Segment list: each segment in an editable textarea, labeled title/paragraph, shows accuracy if available
- Textareas disabled when not in `review` or `failed` status
- "Generate audio" button (or "Try again" for failed jobs)
- "Cancel" button during synthesis
- Collapsible activity log timeline at bottom
- JS: `saveSegment()` calls `POST /api/job/{id}/segments/{index}/text`
- JS: polls `/api/job/{id}/status` every 2s during cleaning/synthesizing, reloads on state change

- [ ] **Step 2: Verify review page**

Upload a document, wait for cleaning, verify review page shows segments. Edit a segment, verify it saves (check database). Click "Generate audio" (will fail until Task 6 is done, that's expected).

- [ ] **Step 3: Commit**

```bash
git add templates/review.html
git commit -m "feat: review page with editable segments and progress"
```

---

### Task 6: Worker - synthesis + timing extraction

**Files:**
- Modify: `worker.py` (implement `_process_synthesis`)

- [ ] **Step 1: Verify pipeline functions are importable**

Run: `python -c "from synthesize import split_into_segments, is_title_line, prepare_reference_audio, transcribe_reference, synthesize_segment, generate_silence, concatenate_wav_data, _patch_sequential; print('OK')"`

- [ ] **Step 2: Implement _process_synthesis**

The method:
1. Prepares reference audio (`prepare_reference_audio` + `transcribe_reference`)
2. Loads F5-TTS and Whisper models
3. Iterates over segments from the database
4. Skips segments that already have `audio_file` and `word_timings_json` (retry support)
5. For each segment: synthesize with `synthesize_segment()`, verify with `verify_segment()` (which produces word timings), retry up to `max_retries` if accuracy below threshold
6. Saves per-segment .wav files, updates segment rows with `audio_file`, `word_timings_json`, `accuracy`, `duration_seconds`
7. Updates job `progress_pct` and `segments_done` after each segment
8. Concatenates all segments + silence gaps into `{job_id}_full.wav`
9. Sets job status to `done` with `final_audio` filename
10. On error: sets status to `failed` with `error_msg` (friendly) and `error_detail` (traceback)

- [ ] **Step 3: Test full pipeline end-to-end**

Upload a short text file, review, click "Generate audio", watch progress bar, verify audio plays.

- [ ] **Step 4: Commit**

```bash
git add worker.py
git commit -m "feat: synthesis worker with QA verification and timing extraction"
```

---

### Task 7: Player page + sync engine

**Files:**
- Create: `templates/player.html`
- Create: `static/player.js`

- [ ] **Step 1: Create player.html**

Create `templates/player.html` with:
- Audio element sourcing `final_audio` from the job
- Seek bar with current/total time display
- Play/pause button (SVG icons toggle)
- Skip back/forward 10s buttons
- Speed select dropdown (0.5x - 2x)
- Volume slider
- Download link
- Transcript container div (`#transcript`, styled with `transcript-scroll` class)
- Loads `player.js` with `JOB_ID` set from template

- [ ] **Step 2: Create player.js sync engine**

Create `static/player.js` implementing:
- `loadTranscript()`: fetches `/api/job/{JOB_ID}/segments`, builds transcript DOM with word-level `<span>` elements. Each span has `class="word"` and `data-start`/`data-end` attributes with global timestamps. Titles get `segment-title` class. Segments separated by `segment-divider` divs.
- Global offset computation: accumulates `duration_seconds + pause_after` per segment to offset each segment's Whisper timestamps into the concatenated audio's timeline.
- `syncLoop()`: `requestAnimationFrame` callback. Reads `audio.currentTime`, binary searches the timeline for the current word, updates highlight classes (`word-current`, `word-spoken`). Auto-scrolls when current word exits viewport using `scrollIntoView({behavior: 'smooth', block: 'center'})`.
- `binarySearch(time)`: O(log n) search on the flat timeline array.
- Seek handler: on `audio.seeked` event, resets all highlight classes and re-marks words before current time as `word-spoken`.
- Click-to-seek: each word span click handler sets `audio.currentTime` and calls `audio.play()`.
- Controls: play/pause toggle, skip +/-10s, speed select, volume slider, seek bar.

- [ ] **Step 3: Test player end-to-end**

Process a document fully, open the player. Verify:
- Audio plays with word highlighting
- Clicking a word seeks audio
- Speed control works (highlight stays in sync)
- Skip buttons work
- Auto-scroll works when current word exits viewport
- Seeking backwards resets highlight state correctly

- [ ] **Step 4: Commit**

```bash
git add templates/player.html static/player.js
git commit -m "feat: audio player with synced transcript highlighting"
```

---

### Task 8: Voice upload + CLAUDE.md update

**Files:**
- Modify: `app.py` (voice upload route if not already added)
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add voice upload route**

Add `POST /api/voices/upload` to `app.py` that accepts a .wav file and saves to `ref/`.

- [ ] **Step 2: Full end-to-end walkthrough**

1. Log in
2. Upload a .docx file
3. Wait for cleaning
4. Review and edit a segment
5. Generate audio
6. Watch progress
7. Play with synced transcript
8. Download .wav
9. Delete job
10. Upload a new voice reference

- [ ] **Step 3: Update CLAUDE.md**

Add web UI section documenting: how to run, default password, environment variables, and the three-page flow.

- [ ] **Step 4: Commit**

```bash
git add app.py CLAUDE.md
git commit -m "feat: voice upload, docs update"
```
