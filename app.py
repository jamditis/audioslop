"""Flask web UI for audioslop document-to-audiobook pipeline."""

import os
import shutil
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

from activity import log_activity, read_activity
from db import (
    create_job,
    delete_job_cascade,
    get_job,
    get_segments,
    init_db,
    list_jobs,
    update_job,
    update_segment,
)

BASE_DIR = Path(__file__).parent
DB_PATH = str(BASE_DIR / "audioslop.db")
UPLOAD_DIR = BASE_DIR / "uploads"
JOBS_DIR = BASE_DIR / "jobs"
REF_DIR = BASE_DIR / "ref"
ALLOWED_EXTENSIONS = {".docx", ".pdf", ".srt", ".txt", ".md"}

app = Flask(__name__)
app.secret_key = os.environ.get("AUDIOSLOP_SECRET", "dev-secret-change-me")

PASSWORD = os.environ.get("AUDIOSLOP_PASSWORD", "audioslop")


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == PASSWORD:
            session["authed"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Wrong password.")
    return render_template("login.html")


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
    jobs = list_jobs(DB_PATH)
    voices = sorted(p.name for p in REF_DIR.glob("*.wav"))
    return render_template("upload.html", jobs=jobs, voices=voices)


@app.route("/job/<job_id>")
@require_auth
def job_review(job_id):
    job = get_job(DB_PATH, job_id)
    if not job:
        return "Job not found.", 404
    segments = get_segments(DB_PATH, job_id)
    job_dir = JOBS_DIR / job_id
    activities = read_activity(job_dir)
    return render_template("review.html", job=job, segments=segments, activities=activities)


@app.route("/job/<job_id>/player")
@require_auth
def job_player(job_id):
    job = get_job(DB_PATH, job_id)
    if not job:
        return "Job not found.", 404
    return render_template("player.html", job=job)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
@require_auth
def api_upload():
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
    )

    UPLOAD_DIR.mkdir(exist_ok=True)
    save_path = UPLOAD_DIR / f"{job_id}_{file.filename}"
    file.save(str(save_path))

    update_job(DB_PATH, job_id, status="cleaning")

    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "upload", f"Uploaded {file.filename}")

    return jsonify({"job_id": job_id}), 201


@app.route("/api/job/<job_id>/status")
@require_auth
def api_job_status(job_id):
    job = get_job(DB_PATH, job_id)
    if not job:
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
    if not job:
        return jsonify({"error": "Job not found."}), 404
    segments = get_segments(DB_PATH, job_id)
    return jsonify(segments)


@app.route("/api/job/<job_id>/segments/<int:index>/text", methods=["POST"])
@require_auth
def api_update_segment_text(job_id, index):
    job = get_job(DB_PATH, job_id)
    if not job:
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
    if not job:
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
    if not job:
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
    if not job:
        return jsonify({"error": "Job not found."}), 404

    try:
        get_worker().cancel_job(job_id)
    except Exception:
        pass

    update_job(DB_PATH, job_id, status="cancelled")
    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "cancel", "Job cancelled")
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/audio/<path:filename>")
@require_auth
def api_serve_audio(job_id, filename):
    audio_dir = JOBS_DIR / job_id / "audio"
    if not audio_dir.exists():
        return "Audio not found.", 404
    return send_from_directory(str(audio_dir), filename)


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
# Worker integration
# ---------------------------------------------------------------------------

_worker = None


def get_worker():
    global _worker
    if _worker is None:
        from worker import Worker
        _worker = Worker(DB_PATH, JOBS_DIR, UPLOAD_DIR, REF_DIR)
        _worker.start()
    return _worker


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    UPLOAD_DIR.mkdir(exist_ok=True)
    JOBS_DIR.mkdir(exist_ok=True)
    REF_DIR.mkdir(exist_ok=True)
    init_db(DB_PATH)
    get_worker()  # Start background worker
    app.run(debug=True, port=5000, use_reloader=False)
