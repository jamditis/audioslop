"""Flask web UI for audioslop document-to-audiobook pipeline."""

import json
import os
import shutil
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from activity import log_activity, read_activity
from db import (
    count_users,
    create_invite,
    create_job,
    create_segment,
    create_user,
    delete_invite,
    delete_job_cascade,
    delete_user,
    get_invite_by_token,
    get_job,
    get_segments,
    get_user_by_id,
    get_user_by_name,
    init_db,
    list_invites,
    list_jobs,
    list_users,
    update_job,
    update_segment,
    use_invite,
)

BASE_DIR = Path(__file__).parent
DB_PATH = str(BASE_DIR / "audioslop.db")
UPLOAD_DIR = BASE_DIR / "uploads"
JOBS_DIR = BASE_DIR / "jobs"
REF_DIR = BASE_DIR / "ref"
ALLOWED_EXTENSIONS = {".docx", ".pdf", ".srt", ".txt", ".md"}

app = Flask(__name__)
app.secret_key = os.environ.get("AUDIOSLOP_SECRET", "dev-secret-change-me")

WORKER_API_KEY = os.environ.get("AUDIOSLOP_WORKER_KEY", "dev-worker-key")


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if count_users(DB_PATH) == 0:
                return redirect(url_for("setup"))
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if not session.get("is_admin"):
            return "Forbidden.", 403
        return f(*args, **kwargs)
    return decorated


def require_worker_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth != "Bearer " + WORKER_API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def check_job_access(job):
    """Return True if the current user can access this job."""
    if not job:
        return False
    if session.get("is_admin"):
        return True
    return job.get("user_id") == session.get("user_id")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if count_users(DB_PATH) > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not username or not password:
            return render_template("setup.html", error="Username and password are required.")
        if password != confirm:
            return render_template("setup.html", error="Passwords do not match.")
        password_hash = generate_password_hash(password)
        user_id = create_user(DB_PATH, name=username, password_hash=password_hash, is_admin=1)
        session["user_id"] = user_id
        session["user_name"] = username
        session["is_admin"] = True
        return redirect(url_for("index"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if count_users(DB_PATH) == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user_by_name(DB_PATH, username)
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["is_admin"] = bool(user["is_admin"])
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid username or password.")
    return render_template("login.html")


@app.route("/invite/<token>", methods=["GET", "POST"])
def invite_signup(token):
    invite = get_invite_by_token(DB_PATH, token)
    if not invite or invite["used_by"]:
        return render_template("signup.html", token=token, error="This invite link is invalid or has already been used.")
    if invite.get("expires_at"):
        try:
            exp = datetime.fromisoformat(invite["expires_at"])
            if datetime.utcnow() > exp:
                return render_template("login.html", error="This invite link has expired.")
        except (ValueError, TypeError):
            pass
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not username or not password:
            return render_template("signup.html", token=token, error="Username and password are required.")
        if password != confirm:
            return render_template("signup.html", token=token, error="Passwords do not match.")
        if get_user_by_name(DB_PATH, username):
            return render_template("signup.html", token=token, error="That username is taken.")
        password_hash = generate_password_hash(password)
        user_id = create_user(DB_PATH, name=username, password_hash=password_hash, is_admin=0, invite_id=invite["id"])
        use_invite(DB_PATH, token=token, used_by=user_id)
        session["user_id"] = user_id
        session["user_name"] = username
        session["is_admin"] = False
        return redirect(url_for("index"))
    return render_template("signup.html", token=token)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
@require_auth
def index():
    if session.get("is_admin"):
        jobs = list_jobs(DB_PATH)
    else:
        jobs = list_jobs(DB_PATH, user_id=session.get("user_id"))
    voices = sorted(p.name for p in REF_DIR.glob("*.wav"))
    return render_template("upload.html", jobs=jobs, voices=voices)


@app.route("/job/<job_id>")
@require_auth
def job_review(job_id):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return "Job not found.", 404
    segments = get_segments(DB_PATH, job_id)
    job_dir = JOBS_DIR / job_id
    activities = read_activity(job_dir)
    return render_template("review.html", job=job, segments=segments, activities=activities)


@app.route("/job/<job_id>/player")
@require_auth
def job_player(job_id):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return "Job not found.", 404
    return render_template("player.html", job=job)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
@require_auth
def api_upload():
    import traceback
    from audioslop import EXTRACTORS, chunk_text, clean_for_tts
    from synthesize import is_title_line, split_into_segments

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided."}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({
            "error": "This file type isn't supported. Try .docx, .pdf, .txt, .md, or .srt."
        }), 400

    speed = float(request.form.get("speed", 0.85))
    voice_ref = request.form.get("voice_ref", "amditis.wav")
    title_pause = float(request.form.get("title_pause", 2.0))
    para_pause = float(request.form.get("para_pause", 0.75))

    job_id = create_job(
        DB_PATH,
        filename=file.filename,
        speed=speed,
        voice_ref=voice_ref,
        title_pause=title_pause,
        para_pause=para_pause,
        user_id=session.get("user_id"),
    )

    UPLOAD_DIR.mkdir(exist_ok=True)
    save_path = UPLOAD_DIR / f"{job_id}_{file.filename}"
    file.save(str(save_path))

    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "upload", f"Uploaded {file.filename}")

    try:
        # Detect extension (handle double extensions like .md.docx)
        if save_path.name.lower().endswith(".md.docx"):
            detected_ext = ".docx"
        else:
            detected_ext = save_path.suffix.lower()

        extractor = EXTRACTORS.get(detected_ext)
        if not extractor:
            raise ValueError(f"No extractor for extension '{detected_ext}'")

        raw_text = extractor(save_path)
        if not raw_text or not raw_text.strip():
            raise ValueError("Extracted text is empty")

        cleaned = clean_for_tts(raw_text)
        chunks = chunk_text(cleaned, max_chars=4000)

        cleaned_dir = job_dir / "cleaned"
        cleaned_dir.mkdir(parents=True, exist_ok=True)

        seg_index = 0
        for i, chunk_content in enumerate(chunks):
            chunk_filename = f"part{i:03d}.txt"
            chunk_path = cleaned_dir / chunk_filename
            chunk_path.write_text(chunk_content, encoding="utf-8")

            segments = split_into_segments(
                chunk_content,
                title_pause=title_pause,
                para_pause=para_pause,
            )

            for seg in segments:
                create_segment(
                    DB_PATH,
                    job_id=job_id,
                    seg_index=seg_index,
                    chunk_file=chunk_filename,
                    source_text=seg["text"],
                    is_title=1 if is_title_line(seg["text"]) else 0,
                    pause_after=seg["pause_after"],
                )
                seg_index += 1

        update_job(DB_PATH, job_id, status="review", segments_total=seg_index)
        log_activity(
            job_dir,
            "clean_done",
            f"Cleaning complete: {len(chunks)} chunk(s), {seg_index} segment(s)",
        )

    except Exception as e:
        tb = traceback.format_exc()
        update_job(DB_PATH, job_id, status="failed", error_msg=str(e), error_detail=tb)
        log_activity(job_dir, "clean_error", str(e))

    return jsonify({"job_id": job_id}), 201


@app.route("/api/job/<job_id>/status")
@require_auth
def api_job_status(job_id):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return jsonify({"error": "Job not found."}), 404
    return jsonify({
        "status": job["status"],
        "progress_pct": job["progress_pct"],
        "segments_done": job["segments_done"],
        "segments_total": job["segments_total"],
        "error_msg": job["error_msg"],
        "error_detail": job["error_detail"],
    })


@app.route("/api/job/<job_id>/segments")
@require_auth
def api_job_segments(job_id):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return jsonify({"error": "Job not found."}), 404
    segments = get_segments(DB_PATH, job_id)
    return jsonify(segments)


@app.route("/api/job/<job_id>/segments/<int:index>/text", methods=["POST"])
@require_auth
def api_update_segment_text(job_id, index):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return jsonify({"error": "Job not found."}), 404

    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if text is None:
        return jsonify({"error": "Missing 'text' field."}), 400

    update_segment(DB_PATH, job_id, index, source_text=text, user_edited=1)
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/synthesize", methods=["POST"])
@require_auth
def api_synthesize(job_id):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return jsonify({"error": "Job not found."}), 404

    if job["status"] not in ("review", "failed"):
        return jsonify({"error": "Job must be in review or failed status to synthesize."}), 400

    update_job(DB_PATH, job_id, status="synthesizing", error_msg=None, error_detail=None)
    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "synthesize", "Synthesis started")
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>", methods=["DELETE"])
@require_auth
def api_delete_job(job_id):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return jsonify({"error": "Job not found."}), 404

    job_dir = JOBS_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(str(job_dir))

    upload_pattern = f"{job_id}_*"
    for f in UPLOAD_DIR.glob(upload_pattern):
        f.unlink()

    delete_job_cascade(DB_PATH, job_id)
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
@require_auth
def api_cancel_job(job_id):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return jsonify({"error": "Job not found."}), 404

    update_job(DB_PATH, job_id, status="cancelled")
    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "cancel", "Job cancelled")
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/audio/<path:filename>")
@require_auth
def api_serve_audio(job_id, filename):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return jsonify({"error": "Job not found."}), 404
    from r2 import presigned_url
    url = presigned_url(filename)
    return redirect(url)


@app.route("/api/job/<job_id>/audio-url")
@require_auth
def api_audio_url(job_id):
    job = get_job(DB_PATH, job_id)
    if not check_job_access(job):
        return jsonify({"error": "Job not found."}), 404
    if not job["final_audio"]:
        return jsonify({"error": "Audio not ready."}), 404
    from r2 import presigned_url
    url = presigned_url(job["final_audio"])
    return jsonify({"url": url})


@app.route("/api/job/<job_id>/activity")
@require_auth
def api_job_activity(job_id):
    job_dir = JOBS_DIR / job_id
    entries = read_activity(job_dir)
    return jsonify(entries)


@app.route("/api/voices")
@require_auth
def api_voices():
    voices = sorted(p.name for p in REF_DIR.glob("*.wav"))
    return jsonify(voices)


@app.route("/api/voices/upload", methods=["POST"])
@require_auth
def api_voices_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided."}), 400

    if not file.filename.lower().endswith(".wav"):
        return jsonify({"error": "Only .wav files are accepted."}), 400

    REF_DIR.mkdir(exist_ok=True)
    save_path = REF_DIR / file.filename
    file.save(str(save_path))
    return jsonify({"ok": True, "filename": file.filename}), 201


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

_worker_status = {}


@app.route("/admin")
@require_auth
@require_admin
def admin():
    users = list_users(DB_PATH)
    invites = list_invites(DB_PATH)
    user_map = {u["id"]: u["name"] for u in users}
    return render_template("admin.html", users=users, invites=invites, user_map=user_map)


@app.route("/api/admin/invites", methods=["POST"])
@require_auth
@require_admin
def api_admin_create_invite():
    invite = create_invite(DB_PATH, created_by=session["user_id"])
    invite_url = url_for("invite_signup", token=invite["token"], _external=True)
    return jsonify({"id": invite["id"], "token": invite["token"], "invite_url": invite_url}), 201


@app.route("/api/admin/invites/<invite_id>", methods=["DELETE"])
@require_auth
@require_admin
def api_admin_delete_invite(invite_id):
    delete_invite(DB_PATH, invite_id)
    return jsonify({"ok": True})


@app.route("/api/admin/users/<user_id>", methods=["DELETE"])
@require_auth
@require_admin
def api_admin_delete_user(user_id):
    if user_id == session["user_id"]:
        return jsonify({"error": "Cannot delete your own account."}), 400
    target = get_user_by_id(DB_PATH, user_id)
    if not target:
        return jsonify({"error": "User not found."}), 404
    if target["is_admin"]:
        return jsonify({"error": "Cannot delete admin users."}), 400
    delete_user(DB_PATH, user_id)
    return jsonify({"ok": True})


@app.route("/api/admin/worker-status")
@require_auth
@require_admin
def api_admin_worker_status():
    return jsonify(_worker_status)


# ---------------------------------------------------------------------------
# Worker API (called by remote GPU worker over Tailscale)
# ---------------------------------------------------------------------------

@app.route("/api/worker/jobs")
@require_worker_auth
def api_worker_jobs():
    status = request.args.get("status")
    jobs = list_jobs(DB_PATH, limit=50)
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    # Return oldest first (list_jobs returns newest first)
    jobs = list(reversed(jobs))
    fields = ("id", "filename", "speed", "voice_ref", "title_pause", "para_pause", "segments_total")
    return jsonify([{k: j[k] for k in fields} for j in jobs])


@app.route("/api/worker/job/<job_id>/segments")
@require_worker_auth
def api_worker_job_segments(job_id):
    job = get_job(DB_PATH, job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    segments = get_segments(DB_PATH, job_id)
    fields = ("seg_index", "source_text", "is_title", "pause_after", "audio_file")
    return jsonify([{k: s[k] for k in fields} for s in segments])


@app.route("/api/worker/ref/<path:filename>")
@require_worker_auth
def api_worker_ref(filename):
    return send_from_directory(str(REF_DIR), filename)


@app.route("/api/worker/job/<job_id>/segment/<int:seg_index>/complete", methods=["POST"])
@require_worker_auth
def api_worker_segment_complete(job_id, seg_index):
    job = get_job(DB_PATH, job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404

    data = request.get_json(silent=True) or {}
    audio_r2_key = data.get("audio_r2_key")
    accuracy = data.get("accuracy")
    duration_seconds = data.get("duration_seconds")
    word_timings = data.get("word_timings")

    update_segment(
        DB_PATH, job_id, seg_index,
        audio_file=audio_r2_key,
        accuracy=accuracy,
        duration_seconds=duration_seconds,
        word_timings_json=json.dumps(word_timings) if word_timings is not None else None,
    )

    # Recalculate progress from DB to avoid races
    segments = get_segments(DB_PATH, job_id)
    segments_done = sum(1 for s in segments if s["audio_file"])
    segments_total = job["segments_total"] or len(segments)
    progress_pct = int(segments_done / segments_total * 100) if segments_total else 0
    progress_pct = min(progress_pct, 100)
    update_job(DB_PATH, job_id, segments_done=segments_done, progress_pct=progress_pct)

    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "segment_done", f"Segment {seg_index} complete")

    return jsonify({"ok": True})


@app.route("/api/worker/job/<job_id>/complete", methods=["POST"])
@require_worker_auth
def api_worker_job_complete(job_id):
    job = get_job(DB_PATH, job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404

    data = request.get_json(silent=True) or {}
    final_audio = data.get("final_audio")

    update_job(DB_PATH, job_id, status="done", progress_pct=100, final_audio=final_audio)

    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "synthesis_done", "Synthesis complete")

    return jsonify({"ok": True})


@app.route("/api/worker/job/<job_id>/fail", methods=["POST"])
@require_worker_auth
def api_worker_job_fail(job_id):
    job = get_job(DB_PATH, job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404

    data = request.get_json(silent=True) or {}
    error_msg = data.get("error_msg", "Worker error")
    error_detail = data.get("error_detail")

    update_job(DB_PATH, job_id, status="failed", error_msg=error_msg, error_detail=error_detail)

    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "synthesis_failed", error_msg)

    return jsonify({"ok": True})


@app.route("/api/worker/heartbeat", methods=["POST"])
@require_worker_auth
def api_worker_heartbeat():
    data = request.get_json(silent=True) or {}
    _worker_status.clear()
    _worker_status.update(data)
    _worker_status["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    UPLOAD_DIR.mkdir(exist_ok=True)
    JOBS_DIR.mkdir(exist_ok=True)
    REF_DIR.mkdir(exist_ok=True)
    init_db(DB_PATH)
    app.run(debug=True, port=5000, use_reloader=False)
