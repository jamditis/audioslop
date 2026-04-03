# audioslop deployment design

**Date:** 2026-04-03
**Status:** Draft

## Goal

Deploy audioslop from a local dev server to a publicly accessible tool at `audioslop.amditis.tech` that a small group of friends and family can use via invite links.

## Architecture overview

Two-node split across the Tailscale mesh:

- **landofjawn** (Intel N95, Doylestown, `100.123.224.40`) -- always-on web server. Runs Flask, serves UI via Cloudflare Tunnel, handles auth, uploads, text cleaning, and job management. Stores uploads and SQLite DB locally. Serves audio via Cloudflare R2 presigned URLs.
- **Legion** (RTX 4080 Super, Bloomfield, `100.108.24.67`) -- GPU worker. Runs a standalone polling script that checks landofjawn for pending synthesis jobs over Tailscale. Downloads segment texts, synthesizes audio with F5-TTS, uploads .wav files to R2, reports completion back to landofjawn.

### Data flow

```
User browser
  |
  | HTTPS (audioslop.amditis.tech)
  v
Cloudflare Tunnel
  |
  v
landofjawn:5000 (Flask)
  |
  |-- Upload: save file to disk, extract text, clean, chunk, create segments in SQLite
  |-- Review: serve cleaned text for user editing
  |-- Synthesize: set job status to "synthesizing" (queued)
  |-- Player: generate R2 presigned URLs, serve player page
  |
  |   Tailscale (100.123.224.40)
  |   <----- polling every 30s ----->
  |
Legion (worker_remote.py)
  |-- Poll: GET /api/worker/jobs?status=synthesizing
  |-- Process: download segment texts, run F5-TTS on GPU
  |-- Upload: push .wav files to R2 bucket "audioslop"
  |-- Report: POST completion + R2 URLs back to landofjawn
```

## Auth system

### Tables

```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,       -- uuid hex[:12]
    name TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    invite_id TEXT REFERENCES invites(id)
);

CREATE TABLE invites (
    id TEXT PRIMARY KEY,       -- uuid hex[:12]
    token TEXT NOT NULL UNIQUE, -- url-safe random token
    created_by TEXT NOT NULL REFERENCES users(id),
    expires_at TIMESTAMP,      -- NULL = never expires
    used_by TEXT REFERENCES users(id),
    used_at TIMESTAMP
);
```

### Flows

**Initial setup (first run):**
- If no users exist, the first visitor sees a setup page to create the admin account (name + password).
- No invite required for the first user. They automatically get `is_admin = 1`.

**Invite flow:**
1. Admin generates an invite link from `/admin/invites` (or CLI: `python manage.py create-invite`)
2. Link format: `https://audioslop.amditis.tech/invite/<token>`
3. Recipient clicks link, sees signup form (display name + password)
4. On submit: create user, mark invite as used, log them in
5. Expired or already-used invites show an error page

**Login flow:**
- `/login` page with name + password fields
- `werkzeug.security.check_password_hash` for verification
- Flask session cookie (same as current, just backed by user accounts)

**Admin capabilities:**
- View all users at `/admin/users`
- Generate/revoke invite links at `/admin/invites`
- Delete user accounts
- View all jobs (not just their own)

**Regular user capabilities:**
- Upload documents, manage their own jobs
- Access the player for their completed audiobooks
- Jobs are scoped to the user who created them (`user_id` column on jobs table)

### Password hashing

Use `werkzeug.security.generate_password_hash` / `check_password_hash` (pbkdf2:sha256 by default). No additional dependencies needed -- werkzeug ships with Flask.

## Database changes

### Modified tables

```sql
-- Add user_id to jobs table
ALTER TABLE jobs ADD COLUMN user_id TEXT REFERENCES users(id);
```

### New tables

`users` and `invites` tables as described above.

### Migration strategy

The DB is fresh on landofjawn (no existing data to migrate). The `init_db()` function gets updated to create all tables including users and invites. No migration scripts needed.

## Worker API

New endpoints under `/api/worker/`, authenticated with a bearer token (shared secret).

### Authentication

```
Authorization: Bearer <WORKER_API_KEY>
```

The key is stored in `pass` on houseofjawn at `claude/services/audioslop-worker-key`. Both landofjawn and Legion fetch it at startup.

### Endpoints

**`GET /api/worker/jobs?status=synthesizing`**

Returns jobs awaiting synthesis, oldest first.

```json
[
  {
    "id": "c32f171c0633",
    "filename": "01-foreword.txt",
    "speed": 0.85,
    "voice_ref": "amditis.wav",
    "title_pause": 2.0,
    "para_pause": 0.75,
    "segments_total": 15
  }
]
```

**`GET /api/worker/job/<id>/segments`**

Returns all segments for a job.

```json
[
  {
    "seg_index": 0,
    "source_text": "The cathedral and the bazaar.",
    "is_title": 1,
    "pause_after": 2.0,
    "audio_file": null
  }
]
```

**`GET /api/worker/ref/<filename>`**

Downloads a reference voice .wav file. Legion caches these locally.

**`POST /api/worker/job/<id>/segment/<idx>/complete`**

Reports a segment synthesis result.

```json
{
  "audio_r2_key": "audioslop/c32f171c0633/seg_0000.wav",
  "accuracy": 0.95,
  "duration_seconds": 12.5,
  "word_timings": [{"word": "The", "start": 0.0, "end": 0.15, "gap_before": 0.0}]
}
```

**`POST /api/worker/job/<id>/complete`**

Marks the job as done after all segments are synthesized and concatenated.

```json
{
  "final_audio_r2_key": "audioslop/c32f171c0633/full.wav"
}
```

**`POST /api/worker/job/<id>/fail`**

Reports a job failure.

```json
{
  "error_msg": "CUDA out of memory",
  "error_detail": "Full traceback..."
}
```

**`POST /api/worker/heartbeat`**

Legion sends a heartbeat every poll cycle. landofjawn tracks worker status for the admin UI.

```json
{
  "hostname": "legion2025",
  "gpu_name": "NVIDIA RTX 4080 Super",
  "gpu_memory_used_mb": 4096,
  "gpu_memory_total_mb": 16384,
  "current_job_id": "c32f171c0633"  // null if idle
}
```

## R2 integration

### Bucket

New Cloudflare R2 bucket: `audioslop`
Region: ENAM (same as pi-transfer)
Created and managed from the Cloudflare dashboard.

### Key structure

```
audioslop/
  {job_id}/
    seg_0000.wav
    seg_0001.wav
    ...
    full.wav
```

### Upload (Legion -> R2)

Legion uses `boto3` with R2-compatible S3 endpoint. Credentials fetched from houseofjawn `pass` at startup.

```python
import boto3

s3 = boto3.client(
    "s3",
    endpoint_url="https://<account_id>.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
)
s3.upload_file(local_path, "audioslop", r2_key)
```

### Serving (landofjawn -> user)

landofjawn generates presigned URLs when the player page loads. URLs expire after 1 hour.

```python
url = s3.generate_presigned_url(
    "get_object",
    Params={"Bucket": "audioslop", "Key": r2_key},
    ExpiresIn=3600,
)
```

The player page and audio API endpoints switch from serving local files to returning/embedding presigned R2 URLs.

## Legion GPU worker (`worker_remote.py`)

A standalone Python script that runs on Legion. Not part of the Flask app.

### Behavior

1. **Startup:** load F5-TTS and Whisper models into GPU memory. Fetch worker API key and R2 credentials from houseofjawn. Cache reference voices locally in `ref/`.
2. **Poll loop:** every 30 seconds, `GET /api/worker/jobs?status=synthesizing` from landofjawn over Tailscale.
3. **Process job:** for each pending job:
   - Fetch segments via worker API
   - Download reference voice if not cached
   - Synthesize each segment with F5-TTS
   - Run Whisper QA verification
   - Upload per-segment .wav to R2
   - Report segment completion back to landofjawn
   - After all segments: concatenate, upload full.wav, report job complete
4. **Error handling:** CUDA OOM or other failures -> report via fail endpoint, move to next job. The job goes to "failed" status and the user can retry from the UI.
5. **Heartbeat:** send GPU status every poll cycle for admin visibility.
6. **Cancellation:** check job status before each segment. If status changed to "cancelled", stop processing.

### Running

```bash
# Manual start
python worker_remote.py --api-url http://100.123.224.40:5000

# Or as a Windows scheduled task that runs at login
```

### Dependencies

Same as current: `f5-tts`, `whisper`, `torchaudio`, `torch`. Plus `boto3` for R2 and `requests` for the worker API.

## Web server changes (landofjawn)

### What stays the same

- Flask app structure, templates, static files, design system
- `audioslop.py` text extraction and cleaning logic
- `db.py` pattern (SQLite with direct connections)
- `activity.py` logging
- All existing page routes and user-facing API routes

### What changes

| Component | Current | New |
|-----------|---------|-----|
| Auth | Single shared password | Individual accounts + invite links |
| Worker | In-process daemon thread | Remote worker via API |
| Audio serving | Local filesystem via `send_from_directory` | R2 presigned URLs |
| Audio storage | `jobs/{id}/audio/` on disk | R2 bucket `audioslop/{job_id}/` |
| Text cleaning | Worker thread (async) | Synchronous in upload handler (< 1s for typical docs) |
| DB | `audioslop.db` local | Same, but on landofjawn's disk |
| Hosting | `python app.py` on Legion | systemd service on landofjawn behind Cloudflare Tunnel |

### New files

- `worker_remote.py` -- standalone GPU worker for Legion
- `manage.py` -- CLI for admin tasks (create first user, generate invites, etc.)
- `r2.py` -- R2 client helper (upload, presigned URL generation)

### Modified files

- `app.py` -- add auth routes, worker API, admin pages, R2 integration for audio serving
- `db.py` -- add users/invites tables, user_id on jobs
- `worker.py` -- remove (replaced by worker_remote.py)
- `templates/login.html` -- update for username + password
- `templates/upload.html` -- minor (jobs now scoped to user)
- `templates/player.html` -- audio src from R2 presigned URLs instead of local paths

### New templates

- `templates/signup.html` -- invite link signup form
- `templates/admin_users.html` -- user management
- `templates/admin_invites.html` -- invite link management
- `templates/setup.html` -- first-run admin account creation

## Cloudflare Tunnel on landofjawn

### Config addition

In landofjawn's `~/.cloudflared/config.yml` (or equivalent), add:

```yaml
- hostname: audioslop.amditis.tech
  service: http://localhost:5000
```

### DNS

CNAME record: `audioslop.amditis.tech` -> `<tunnel-id>.cfargotunnel.com`
(Cloudflare manages this automatically when configured via the dashboard or `cloudflared tunnel route dns`)

## systemd service on landofjawn

```ini
[Unit]
Description=audioslop web server
After=network.target

[Service]
Type=simple
User=jamditis
WorkingDirectory=/home/jamditis/projects/audioslop
ExecStart=/home/jamditis/projects/audioslop/venv/bin/python app.py
Restart=always
RestartSec=5
Environment=AUDIOSLOP_SECRET=<generated>
Environment=AUDIOSLOP_WORKER_KEY=<from-pass>
Environment=R2_ACCESS_KEY=<from-pass>
Environment=R2_SECRET_KEY=<from-pass>
Environment=R2_ACCOUNT_ID=<from-cloudflare>

[Install]
WantedBy=multi-user.target
```

## Deployment sequence

1. Create R2 bucket `audioslop` in Cloudflare dashboard
2. Generate R2 API tokens, store in `pass` on houseofjawn
3. Generate a worker API key, store in `pass` on houseofjawn
4. Implement auth system (users/invites tables, login/signup/admin routes)
5. Implement worker API endpoints
6. Implement R2 integration (upload helper, presigned URL serving)
7. Refactor text cleaning to run inline (not in worker thread)
8. Build `worker_remote.py` for Legion
9. Update player template to use R2 URLs
10. Deploy Flask app to landofjawn (git clone, venv, systemd)
11. Configure Cloudflare Tunnel route on landofjawn
12. Test end-to-end: upload on audioslop.amditis.tech -> clean -> synthesize from Legion -> play
13. Create admin account, generate first invite links

## Testing strategy

- **Unit tests:** auth flows (login, signup, invite), worker API endpoints, R2 URL generation
- **Integration test:** full pipeline on Legion (upload doc via API, wait for cleaning, trigger synthesis, verify audio in R2)
- **Manual smoke test:** real browser at audioslop.amditis.tech, full flow including invite link signup

## Security considerations

- Worker API key required for all `/api/worker/` endpoints. Prevents unauthorized synthesis requests.
- User passwords hashed with werkzeug (pbkdf2:sha256)
- Invite tokens are url-safe random (128-bit entropy)
- R2 presigned URLs expire after 1 hour
- Flask session secret generated per deployment (not the dev default)
- Cloudflare Tunnel provides TLS termination -- no self-managed certs
- Tailscale provides encrypted worker<->server communication -- no public exposure of the worker API

## CJS2026 isolation

This deployment must not interfere with the CJS2026 pipeline (Cloud Function -> houseofjawn notify-service -> Legion Remotion render -> Firebase Storage).

**No conflicts:**
- Web server runs on landofjawn, not houseofjawn. CJS services on houseofjawn are unaffected.
- Separate R2 bucket (`audioslop` vs `pi-transfer`).
- Separate Cloudflare Tunnel config (landofjawn vs houseofjawn).
- Separate SQLite database.
- No shared code or dependencies between the two pipelines.

**GPU contention on Legion:**
Both audioslop synthesis (F5-TTS, ~4-6GB VRAM) and CJS2026 rendering (Remotion/Chromium, ~2-4GB VRAM) use the RTX 4080 Super. If both run simultaneously, CUDA OOM is likely.

Mitigation: `worker_remote.py` checks available VRAM before starting a synthesis job. If VRAM is below a threshold (e.g., < 8GB free), it skips the poll cycle and tries again in 30 seconds. This lets CJS render jobs finish without interference. Audioslop jobs are queue-and-wait anyway, so a short delay is acceptable.

## Out of scope

- User-uploaded reference voices (keep admin-managed for now)
- Email notifications when audiobooks are ready
- Multiple concurrent workers
- Auto-WoL for Legion
- Mobile-specific UI changes (current responsive design should work)
