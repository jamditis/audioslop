#!/usr/bin/env python3
"""
worker_remote.py - Standalone GPU worker for audioslop.

Polls the landofjawn worker API for synthesis jobs, synthesizes audio
using F5-TTS, and uploads results to R2. Designed to run on Legion
(RTX 4080 Super) and communicate with the Flask app over Tailscale.

Usage:
    python worker_remote.py --api-url http://100.123.224.40:5000 --api-key <key>
    python worker_remote.py --api-url http://100.123.224.40:5000  # key from env
"""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
import traceback
import wave as wave_mod
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

from r2 import upload_file as r2_upload
from synthesize import (
    TARGET_SAMPLE_RATE,
    concatenate_wav_data,
    generate_silence,
    is_title_line,
    prepare_reference_audio,
    split_into_segments,
    synthesize_segment,
    transcribe_reference,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file: str = "worker_remote.log") -> logging.Logger:
    """Configure rotating file + console logging."""
    logger = logging.getLogger("worker_remote")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# GPU utilities
# ---------------------------------------------------------------------------

def get_gpu_info() -> dict:
    """
    Query nvidia-smi for GPU memory stats.
    Returns dict with free_mb, used_mb, total_mb, name.
    Returns empty dict if nvidia-smi is unavailable.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.free,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {}
        line = result.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            return {}
        return {
            "name": parts[0],
            "free_mb": int(parts[1]),
            "used_mb": int(parts[2]),
            "total_mb": int(parts[3]),
        }
    except Exception as exc:
        log.debug("nvidia-smi query failed: %s", exc)
        return {}


def check_vram_available(min_free_mb: int = 8000) -> bool:
    """Return True if free VRAM is at or above min_free_mb."""
    info = get_gpu_info()
    if not info:
        # Can't determine -- assume OK and let CUDA OOM be the gate.
        return True
    free = info.get("free_mb", 0)
    if free < min_free_mb:
        log.warning("low VRAM: %d MB free, need %d MB -- skipping cycle", free, min_free_mb)
        return False
    return True


# ---------------------------------------------------------------------------
# Remote worker
# ---------------------------------------------------------------------------

class RemoteWorker:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        ref_dir: Path,
        work_dir: Path,
    ):
        self.api_url = api_url.rstrip("/")
        self.ref_dir = ref_dir
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"
        self._session.headers["Content-Type"] = "application/json"

        self._tts = None
        self._whisper = None

    # ------------------------------------------------------------------
    # Model loading (deferred until first job)
    # ------------------------------------------------------------------

    def _load_models(self) -> None:
        """Load F5-TTS and Whisper models if not already loaded."""
        if self._tts is None:
            log.info("loading F5-TTS model")
            from f5_tts.api import F5TTS
            self._tts = F5TTS()
            log.info("F5-TTS ready")

        if self._whisper is None:
            log.info("loading Whisper model")
            import whisper
            self._whisper = whisper.load_model("base")
            log.info("Whisper ready")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **kwargs):
        url = self.api_url + path
        resp = self._session.get(url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def _post(self, path: str, **kwargs):
        url = self.api_url + path
        resp = self._session.post(url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def heartbeat(self, current_job_id: str | None = None) -> None:
        """Report GPU status to the server."""
        import socket
        gpu = get_gpu_info()
        payload = {
            "hostname": socket.gethostname(),
            "gpu_name": gpu.get("name"),
            "gpu_memory_used_mb": gpu.get("used_mb"),
            "gpu_memory_total_mb": gpu.get("total_mb"),
            "current_job": current_job_id,
        }
        try:
            self._post("/api/worker/heartbeat", json=payload)
        except Exception as exc:
            log.warning("heartbeat failed: %s", exc)

    def poll_jobs(self) -> list[dict]:
        """Return jobs with status=synthesizing, oldest first."""
        try:
            resp = self._get("/api/worker/jobs", params={"status": "synthesizing"})
            return resp.json()
        except Exception as exc:
            log.warning("poll_jobs failed: %s", exc)
            return []

    def fetch_segments(self, job_id: str) -> list[dict]:
        """Return all segments for a job."""
        resp = self._get(f"/api/worker/job/{job_id}/segments")
        return resp.json()

    def fetch_ref_audio(self, filename: str) -> Path:
        """
        Download a reference voice clip from the server and cache it locally.
        Returns the local path.
        """
        local_path = self.ref_dir / filename
        if local_path.exists():
            return local_path

        self.ref_dir.mkdir(parents=True, exist_ok=True)
        log.info("downloading ref audio: %s", filename)
        resp = self._get(f"/api/worker/ref/{filename}", stream=True)
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        log.info("cached ref audio: %s", local_path)
        return local_path

    def report_segment(self, job_id: str, idx: int, data: dict) -> None:
        """Report a completed segment to the server."""
        self._post(f"/api/worker/job/{job_id}/segment/{idx}/complete", json=data)

    def report_complete(self, job_id: str, final_key: str) -> None:
        """Mark a job as done with the R2 key of the full audio."""
        self._post(f"/api/worker/job/{job_id}/complete", json={"final_audio": final_key})

    def report_fail(self, job_id: str, error_msg: str, error_detail: str | None = None) -> None:
        """Mark a job as failed."""
        try:
            self._post(
                f"/api/worker/job/{job_id}/fail",
                json={"error_msg": error_msg, "error_detail": error_detail},
            )
        except Exception as exc:
            log.warning("report_fail request itself failed: %s", exc)

    def is_job_active(self, job_id: str) -> bool:
        """
        Return True if the job is still in synthesizing status.
        Used to detect cancellations between segments.
        """
        try:
            resp = self._get("/api/worker/jobs", params={"status": "synthesizing"})
            jobs = resp.json()
            return any(j["id"] == job_id for j in jobs)
        except Exception as exc:
            log.warning("is_job_active check failed: %s", exc)
            # On network errors, assume still active and keep going.
            return True

    # ------------------------------------------------------------------
    # Synthesis pipeline
    # ------------------------------------------------------------------

    def process_job(self, job: dict) -> None:
        """Full synthesis pipeline for one job."""
        job_id = job["id"]
        speed = float(job.get("speed") or 0.85)
        voice_ref = job.get("voice_ref") or "amditis.wav"
        title_pause = float(job.get("title_pause") or 2.0)
        para_pause = float(job.get("para_pause") or 0.75)

        log.info("starting job %s (%s)", job_id, job.get("filename"))

        self._load_models()

        # Prepare reference audio
        raw_ref_path = self.fetch_ref_audio(voice_ref)
        ref_audio_path = prepare_reference_audio(raw_ref_path)

        # Transcribe reference audio if no transcript file is present
        transcript_path = self.ref_dir / (Path(voice_ref).stem + ".txt")
        if transcript_path.exists():
            ref_text = transcript_path.read_text(encoding="utf-8").strip()
        else:
            ref_text = transcribe_reference(ref_audio_path)

        segments = self.fetch_segments(job_id)
        if not segments:
            log.warning("job %s has no segments -- marking complete", job_id)
            self.report_complete(job_id, "")
            return

        log.info("job %s: %d segment(s)", job_id, len(segments))

        local_job_dir = self.work_dir / job_id
        local_job_dir.mkdir(parents=True, exist_ok=True)

        all_parts: list[bytes] = []
        accuracy_threshold = 0.90
        max_retries = 2

        for seg in segments:
            idx = seg["seg_index"]
            source_text = seg["source_text"]
            is_title = bool(seg.get("is_title"))
            pause_after = float(seg.get("pause_after") or 0.0)

            # Skip segments that already have audio (retry support)
            if seg.get("audio_file"):
                log.debug("seg %d already has audio -- downloading for concat", idx)
                try:
                    from r2 import presigned_url as r2_presign
                    url = r2_presign(seg["audio_file"])
                    resp = requests.get(url, timeout=60)
                    resp.raise_for_status()
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                        tmp.write(resp.content)
                        tmp_path = tmp.name
                    with wave_mod.open(tmp_path, "rb") as wf:
                        existing_pcm = wf.readframes(wf.getnframes())
                    os.unlink(tmp_path)
                    all_parts.append(existing_pcm)
                    if pause_after > 0:
                        all_parts.append(generate_silence(pause_after))
                except Exception:
                    log.warning("could not download segment %d for resume, using silence", idx)
                    all_parts.append(generate_silence(0.5))
                    if pause_after > 0:
                        all_parts.append(generate_silence(pause_after))
                continue

            # Cancellation check before each segment
            if not self.is_job_active(job_id):
                log.info("job %s cancelled -- stopping after seg %d", job_id, idx)
                return

            # TTS text for title lines
            tts_text = source_text
            if is_title and tts_text and tts_text[-1] not in ".!?:;,":
                tts_text = tts_text + "."

            best_pcm: bytes | None = None
            best_accuracy = 0.0
            best_word_timings: list[dict] = []
            best_duration = 0.0

            for attempt in range(1, max_retries + 1):
                log.info(
                    "  seg %d/%d attempt %d: %s...",
                    idx + 1, len(segments), attempt, source_text[:60],
                )
                try:
                    pcm = synthesize_segment(
                        self._tts, ref_audio_path, ref_text, tts_text, speed
                    )
                except Exception as exc:
                    log.error("synthesize_segment failed at seg %d: %s", idx, exc)
                    pcm = None

                if not pcm:
                    log.warning("  seg %d: empty PCM -- using silence placeholder", idx)
                    best_pcm = generate_silence(0.5)
                    best_accuracy = 0.0
                    break

                # QA verification for non-trivial segments
                if len(source_text) > 20:
                    from qa import verify_segment
                    seg_qa = verify_segment(source_text, pcm, idx, self._whisper)
                    acc = seg_qa.accuracy
                    duration = seg_qa.duration
                    word_timings = [
                        {"word": wt.word, "start": wt.start, "end": wt.end, "gap_before": wt.gap_before}
                        for wt in seg_qa.word_timings
                    ]
                    log.info("  seg %d accuracy: %.1f%%", idx, acc * 100)

                    if acc > best_accuracy:
                        best_pcm = pcm
                        best_accuracy = acc
                        best_word_timings = word_timings
                        best_duration = duration

                    if acc >= accuracy_threshold:
                        break
                    if attempt < max_retries:
                        log.info("  seg %d below threshold -- retrying", idx)
                else:
                    # Short segment (title etc.) -- no verification
                    best_pcm = pcm
                    best_accuracy = 1.0
                    best_duration = len(pcm) / (TARGET_SAMPLE_RATE * 2)
                    best_word_timings = []
                    break

            # Final fallback to silence
            if not best_pcm:
                log.warning("  seg %d: all attempts failed -- using silence", idx)
                best_pcm = generate_silence(0.5)
                best_accuracy = 0.0
                best_word_timings = []
                best_duration = 0.5

            # Save locally and upload to R2
            seg_filename = f"seg_{idx:04d}.wav"
            local_wav = local_job_dir / seg_filename
            with wave_mod.open(str(local_wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(TARGET_SAMPLE_RATE)
                w.writeframes(best_pcm)

            r2_key = f"{job_id}/{seg_filename}"
            r2_upload(str(local_wav), r2_key)
            log.info("  seg %d uploaded: %s", idx, r2_key)

            # Report to server
            self.report_segment(job_id, idx, {
                "audio_r2_key": r2_key,
                "accuracy": round(best_accuracy, 4),
                "duration_seconds": round(best_duration, 2),
                "word_timings": best_word_timings,
            })

            # Accumulate PCM for final concatenation
            all_parts.append(best_pcm)
            if pause_after > 0:
                all_parts.append(generate_silence(pause_after))

        # Concatenate and upload full audio
        if all_parts:
            final_filename = f"{job_id}_full.wav"
            final_path = local_job_dir / final_filename
            concatenate_wav_data(all_parts, final_path)
            final_key = f"{job_id}/{final_filename}"
            r2_upload(str(final_path), final_key)
            log.info("job %s: full audio uploaded: %s", job_id, final_key)
        else:
            final_key = ""
            log.warning("job %s: no audio parts to concatenate", job_id)

        self.report_complete(job_id, final_key)
        log.info("job %s complete", job_id)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, poll_interval: int = 30) -> None:
        """Poll for jobs and process them one at a time."""
        log.info("worker started -- polling %s every %ds", self.api_url, poll_interval)

        while True:
            self.heartbeat()

            if not check_vram_available(min_free_mb=8000):
                log.info("waiting for VRAM -- sleeping %ds", poll_interval)
                time.sleep(poll_interval)
                continue

            jobs = self.poll_jobs()
            if not jobs:
                log.debug("no jobs -- sleeping %ds", poll_interval)
                time.sleep(poll_interval)
                continue

            job = jobs[0]
            job_id = job["id"]
            log.info("picked up job %s", job_id)
            self.heartbeat(current_job_id=job_id)

            try:
                self.process_job(job)
            except Exception as exc:
                tb = traceback.format_exc()
                if "CUDA out of memory" in str(exc) or "CUDA out of memory" in tb:
                    msg = "CUDA out of memory -- free VRAM and retry"
                else:
                    msg = str(exc) or "worker error"
                log.error("job %s failed: %s\n%s", job_id, msg, tb)
                self.report_fail(job_id, msg, tb)

            # Brief pause between jobs to let CUDA clean up
            time.sleep(2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="worker_remote",
        description="Remote GPU worker for audioslop synthesis jobs.",
    )
    parser.add_argument(
        "--api-url",
        required=True,
        help="Base URL of the audioslop Flask app (e.g. http://100.123.224.40:5000)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("AUDIOSLOP_WORKER_KEY"),
        help="Worker API key (default: AUDIOSLOP_WORKER_KEY env var)",
    )
    parser.add_argument(
        "--ref-dir",
        type=Path,
        default=Path("ref"),
        help="Directory to cache reference voice clips (default: ref/)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("worker_output"),
        help="Working directory for local audio files (default: worker_output/)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between job polls (default: 30)",
    )

    args = parser.parse_args()

    if not args.api_key:
        parser.error("--api-key is required (or set AUDIOSLOP_WORKER_KEY env var)")

    worker = RemoteWorker(
        api_url=args.api_url,
        api_key=args.api_key,
        ref_dir=args.ref_dir,
        work_dir=args.work_dir,
    )
    worker.run(poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()
