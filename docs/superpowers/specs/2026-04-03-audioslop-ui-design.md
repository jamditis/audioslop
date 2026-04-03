# Audioslop web UI design spec

## Overview

A deployable web app that wraps the audioslop document-to-audiobook pipeline. Users upload documents, review cleaned text, generate audio with voice cloning, and listen with a synced transcript player that highlights words as they're spoken.

**Audience:** Joe + a few invited people. Basic auth, no public access.

**Processing model:** Queue-based. Jobs submitted via the web UI, processed on Legion's RTX 4080 Super using the existing pipeline scripts. The worker processes one job at a time, FIFO. Additional jobs remain in `pending` status until the current job finishes. Results available for playback/download when done.

**Stack:** Flask backend, vanilla JS frontend, Tailwind CSS via CDN, SQLite for job queue, no build step.

## Architecture

```
Browser (HTML/JS/Tailwind)
    |
Flask API (app.py - routes + job management)
    |
Background worker (worker.py - imports audioslop/synthesize/qa)
    |
Existing pipeline (audioslop.py -> synthesize.py -> qa.py)
```

### Data flow

1. User uploads file(s) via drag-and-drop or file picker
2. Flask saves to `uploads/`, creates a job row in SQLite (status: `pending`)
3. Background worker picks up the job, runs the pipeline:
   - **Cleaning step:** `audioslop.py` extracts and cleans text, saves to `jobs/{id}/cleaned/`. Worker then runs `split_into_segments()` on each chunk and creates segment rows in the database with `source_text`, `is_title`, and `pause_after`. Status changes to `review`.
   - **Review gate:** User can view and edit segment text on the review page. Edits update `source_text` and set `user_edited=1`. User clicks "Generate audio" to proceed.
   - **Synthesis step:** `synthesize.py` generates audio per segment with QA verification loop. Per-segment .wav files saved to `jobs/{id}/audio/`.
   - **Timing extraction step (mandatory):** After synthesis, Whisper runs with `word_timestamps=True` on every segment's final audio, regardless of QA settings. This produces `word_timings_json` for the player. This step shares the Whisper transcription with QA verification when both are enabled, avoiding a redundant pass.
   - **Concatenation:** All segment .wavs + silence gaps concatenated into a single `{job_id}_full.wav` for the player.
   - Status changes to `done`
4. Frontend polls `/api/job/{id}/status` every 2 seconds during processing
5. Player page loads word timings JSON and syncs transcript highlighting to audio playback

### Worker pipeline (detailed)

```
pending -> cleaning -> review (wait for user) -> synthesizing -> done
                                                      |
                                                   failed (retryable)
```

If synthesis fails partway through (GPU error, OOM), the job status changes to `failed` with `error_msg` set. The user can return to the review page and click "Generate audio" to retry. Completed segments are preserved; only failed/pending segments are re-synthesized.

### Database schema (SQLite)

```sql
CREATE TABLE jobs (
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

CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    seg_index INTEGER NOT NULL,
    chunk_file TEXT NOT NULL,
    source_text TEXT NOT NULL,
    user_edited INTEGER DEFAULT 0,
    audio_file TEXT,
    word_timings_json TEXT,
    accuracy REAL,
    duration_seconds REAL,
    pause_after REAL DEFAULT 0.0,
    is_title INTEGER DEFAULT 0
);
```

**Column notes:**
- `chunk_file`: which cleaned .txt file this segment originated from (e.g. `02-preface_part001.txt`)
- `user_edited`: set to 1 when user modifies segment text on the review page
- `pause_after`: seconds of silence inserted after this segment in the concatenated audio (stored so the player can compute global timeline offsets)
- `final_audio`: filename of the concatenated .wav on the jobs table (e.g. `{id}_full.wav`)
- `segments_done`/`segments_total`: updated during synthesis for progress reporting

### Status endpoint response shape

`GET /api/job/{id}/status` returns:

```json
{
    "status": "synthesizing",
    "progress_pct": 45,
    "segments_done": 5,
    "segments_total": 23,
    "current_segment_accuracy": 0.94,
    "error_msg": null
}
```

## Pages

### Upload page (`/`)

- Drag-and-drop zone accepting .docx, .pdf, .srt, .txt, .md
- Settings panel:
  - Speed slider (0.5x - 1.5x, default 0.85)
  - Voice reference dropdown (populated from `ref/` directory, option to upload new)
  - Title pause and paragraph pause sliders
- "Process" button submits the job
- Below: table of recent jobs with status badges, click to open review or player

### Review page (`/job/{id}`)

- Cleaned text displayed in an editable textarea per segment
- Each segment labeled as "title" or "paragraph" with its index and chunk file
- "Generate audio" button starts synthesis
- Progress bar and per-segment QA results appear as synthesis runs
- Accuracy percentage per segment, flagged segments highlighted
- Error state: if synthesis failed, show error message and "Retry" button
- Link to player page when synthesis completes

### Player page (`/job/{id}/player`)

**Audio controls (top bar):**
- Play/pause button
- Seek bar with current time / total time
- Skip back 10s / skip forward 10s buttons
- Speed control (0.5x, 0.75x, 1.0x, 1.25x, 1.5x, 2.0x)
- Volume slider
- Download .wav button

**Transcript panel (below player):**
- Full text displayed with word-level spans
- Current word highlighted (gold background)
- Already-spoken words dimmed
- Clicking any word seeks audio to that word's start time
- Auto-scrolls to keep current word in viewport (scrolls only when the highlighted word exits the visible area, not continuously)
- Segment dividers between paragraphs
- Chapter/section titles styled distinctly

**Sync engine (`player.js`):**
- Loads all segments from `/api/job/{id}/segments` (includes `word_timings_json`, `duration_seconds`, `pause_after`)
- Builds a flat timeline array. Each entry: `{word, globalStart, globalEnd, segIndex, wordIndex}`
- Global timestamps computed at load time: `globalOffset[i] = sum(duration[0..i-1] + pause_after[0..i-1])`. Each word's `globalStart = segmentOffset + whisperStart`, `globalEnd = segmentOffset + whisperEnd`.
- Uses `requestAnimationFrame` loop checking `audio.currentTime`
- Binary search on the timeline for the current word (O(log n) per frame)
- Updates DOM: adds/removes highlight class on word spans
- Playback speed changes handled automatically since sync is driven by `audio.currentTime`
- Click-to-seek: each word span has `data-start` attribute, click handler calls `audio.currentTime = parseFloat(el.dataset.start)`

**Audio source:** The player loads the single concatenated file `jobs/{id}/audio/{final_audio}`. Per-segment .wav files are retained for potential re-synthesis of individual segments but are not used by the player.

## File structure

```
audioslop/
├── app.py                  # Flask app, routes, job queue
├── worker.py               # Background worker, imports pipeline scripts
├── audioslop.py             # (existing) text extraction + cleaning
├── synthesize.py            # (existing) TTS synthesis + QA
├── qa.py                    # (existing) quality assurance
├── static/
│   ├── app.css              # Custom styles beyond Tailwind
│   └── player.js            # Audio player + transcript sync engine
├── templates/
│   ├── base.html            # Layout shell (nav, Tailwind CDN, shared scripts)
│   ├── login.html           # Login page
│   ├── upload.html          # Upload page + job list
│   ├── review.html          # Text review + synthesis progress
│   └── player.html          # Audio player + synced transcript
├── uploads/                 # Raw uploaded files
├── jobs/                    # Per-job working directories
│   └── {id}/
│       ├── cleaned/         # Cleaned .txt chunks from audioslop.py
│       ├── audio/           # Per-segment .wav files + concatenated full .wav
│       └── meta.json        # Job config snapshot
├── ref/                     # Reference voice clips
├── audioslop.db             # SQLite database
└── CLAUDE.md
```

## API routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/login` | Login page |
| POST | `/login` | Authenticate with shared password |
| GET | `/` | Upload page (requires auth) |
| POST | `/api/upload` | Accept file, create job, start cleaning |
| GET | `/job/{id}` | Review page |
| POST | `/api/job/{id}/segments/{index}/text` | Save edited segment text |
| POST | `/api/job/{id}/synthesize` | Start audio generation |
| GET | `/api/job/{id}/status` | Poll job status + progress (see response shape above) |
| DELETE | `/api/job/{id}` | Delete job and all associated files |
| POST | `/api/job/{id}/cancel` | Cancel a running job |
| GET | `/job/{id}/player` | Player page |
| GET | `/api/job/{id}/segments` | All segments with word timings |
| GET | `/api/job/{id}/audio/{filename}` | Serve audio file (per-segment or concatenated) |
| GET | `/api/voices` | List available reference voices |
| POST | `/api/voices/upload` | Upload new reference voice |

All routes except `/login` require an active session.

## Error handling and user-facing status

### Design principle

Every state the system can be in must have a plain-language explanation visible in the UI. Users are non-technical — they need to know what's happening, what went wrong, and what to do next without reading logs.

### Status messages

Each job status maps to a user-facing message displayed on the review page and job list:

| Status | User sees | Detail |
|--------|-----------|--------|
| `pending` | "Waiting in line -- your file will be processed shortly." | Shown when another job is ahead in the queue |
| `cleaning` | "Reading and preparing your document for audio..." | During text extraction and cleaning |
| `review` | "Ready for review. Check the text below and click Generate when you're ready." | Waiting for user action |
| `synthesizing` | "Generating audio -- segment 5 of 23 complete (about 2 minutes remaining)." | Progress bar + ETA based on average segment time |
| `done` | "Your audiobook is ready. Click Play to listen." | Link to player |
| `failed` | "Something went wrong while generating audio. [See details] [Try again]" | Expandable error detail + retry button |
| `cancelled` | "This job was cancelled." | Option to restart |

### Error detail levels

When a job fails, the system stores two versions of the error:

1. **`error_msg`** (user-facing): plain language, no stack traces. Examples:
   - "The voice model ran out of memory on segment 12. Try again -- this sometimes works on retry, or try a shorter document."
   - "Couldn't read the uploaded file. It might be corrupted or in an unsupported format."
   - "The audio quality check failed for 3 segments after multiple retries. The audio was saved but may have issues in those sections."
2. **`error_detail`** (technical): full exception + traceback, stored in the database for bug reports.

The UI shows `error_msg` by default with a "Show technical details" toggle that reveals `error_detail` in a copyable code block. Users can copy the technical details to paste into a bug report.

### Error categories and recovery

| Error type | User message | Recovery |
|------------|-------------|----------|
| File format not supported | "This file type isn't supported. Try .docx, .pdf, .txt, .md, or .srt." | Upload a different file |
| File too large | "This file is too large to process. Try splitting it into smaller parts." | Upload smaller file |
| Text extraction failed | "Couldn't extract text from this file. It might be corrupted or password-protected." | Upload a different version |
| GPU out of memory | "The voice model ran out of memory. This can happen with long segments. Try again -- it sometimes works on retry." | Retry button |
| F5-TTS generation error | "The voice model had trouble generating audio for segment N. It will retry automatically." | Auto-retry (up to max_retries), then flag segment |
| QA threshold not met | "Some segments didn't pass the quality check. The audio was saved but those sections may sound off." | Show flagged segments, option to regenerate individual segments |
| Whisper transcription error | "Couldn't verify the audio quality for segment N. The audio was saved but hasn't been checked." | Non-blocking -- audio still usable |
| Worker crash | "The audio generator stopped unexpectedly. Click Try Again to restart." | Retry from last incomplete segment |

### Activity log

Each job has a timestamped activity log stored in `jobs/{id}/activity.jsonl` (one JSON object per line). Every significant event is logged:

```json
{"ts": "2026-04-03T19:45:12", "event": "job_created", "msg": "Uploaded cathedral-bazaar.docx"}
{"ts": "2026-04-03T19:45:13", "event": "cleaning_started", "msg": "Extracting text from .docx"}
{"ts": "2026-04-03T19:45:14", "event": "cleaning_done", "msg": "Found 8 paragraphs, 1 title"}
{"ts": "2026-04-03T19:45:30", "event": "synthesis_started", "msg": "Generating audio at 0.85x speed"}
{"ts": "2026-04-03T19:45:32", "event": "segment_done", "seg": 1, "msg": "Title 'Foreword' -- 2.1s audio, skipped verification"}
{"ts": "2026-04-03T19:45:35", "event": "segment_done", "seg": 2, "msg": "Paragraph 1 -- 18.2s audio, 100% accuracy"}
{"ts": "2026-04-03T19:45:38", "event": "segment_retry", "seg": 4, "msg": "Accuracy 87% below threshold, retrying (attempt 2/2)"}
{"ts": "2026-04-03T19:46:10", "event": "synthesis_done", "msg": "8 segments, 195s total audio, 98.4% average accuracy"}
{"ts": "2026-04-03T19:46:10", "event": "error", "msg": "GPU OOM on segment 5", "detail": "RuntimeError: CUDA out of memory..."}
```

The review page shows a collapsible "Activity log" section at the bottom with these events rendered as a timeline. Each entry shows the timestamp, a plain-language description, and an optional "details" expansion for technical info.

### Backend logging

`worker.py` uses Python's `logging` module at INFO level for normal operations, DEBUG for detailed pipeline output. Log format:

```
2026-04-03 19:45:32 [job:abc123] [seg:2/8] INFO: Synthesis complete, 18.2s audio
2026-04-03 19:45:32 [job:abc123] [seg:2/8] INFO: Whisper verification: 100% accuracy
2026-04-03 19:45:38 [job:abc123] [seg:4/8] WARN: Accuracy 87% below threshold, retrying
2026-04-03 19:46:10 [job:abc123] ERROR: GPU OOM on segment 5 -- RuntimeError: CUDA out of memory
```

Logs written to `audioslop.log` (rotating, 10MB max, 3 backups). The log file path and level are configurable via environment variables `AUDIOSLOP_LOG_FILE` and `AUDIOSLOP_LOG_LEVEL`.

## Auth

Simple shared password via environment variable `AUDIOSLOP_PASSWORD`. Flask session cookie after login. No user accounts, no registration.

## Deployment

- Runs on Legion (localhost:5000 for dev)
- For remote access: Cloudflare Tunnel or Tailscale funnel
- Requirements: Python 3.10+, Flask, existing pipeline dependencies (f5-tts, whisper, python-docx, pdfplumber)
- Single process: Flask dev server + background worker thread (adequate for a few users)

## Out of scope for v1

- Multiple voice selection per segment (all segments use same voice ref)
- Real-time streaming synthesis (wait for full job to complete)
- Mobile-optimized layout
- Audio post-processing (EQ, normalization, compression)
- Compressed audio formats (MP3/OGG) -- v1 serves .wav only
- User accounts and multi-tenancy
