"""Background worker thread for processing audioslop jobs."""

import glob
import json
import logging
import logging.handlers
import threading
import time
import traceback
import wave as wave_mod
from pathlib import Path

from activity import log_activity
from audioslop import EXTRACTORS, chunk_text, clean_for_tts
from db import create_segment, get_segments, list_jobs, update_job, update_segment
from synthesize import (
    concatenate_wav_data,
    generate_silence,
    is_title_line,
    prepare_reference_audio,
    split_into_segments,
    synthesize_segment,
    transcribe_reference,
)

logger = logging.getLogger("audioslop.worker")


def _setup_logging() -> None:
    """Configure rotating file handler for the worker logger."""
    if logger.handlers:
        return
    handler = logging.handlers.RotatingFileHandler(
        "audioslop.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


class Worker:
    def __init__(self, db_path, jobs_dir, uploads_dir, ref_dir):
        self._db_path = str(db_path)
        self._jobs_dir = Path(jobs_dir)
        self._uploads_dir = Path(uploads_dir)
        self._ref_dir = Path(ref_dir)
        self._cancel_event = threading.Event()
        self._current_job_id = None
        _setup_logging()

    def start(self):
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()
        logger.info("Worker thread started")

    def cancel_job(self, job_id):
        if self._current_job_id == job_id:
            self._cancel_event.set()

    def _run_loop(self):
        while True:
            try:
                self._poll_for_work()
            except Exception:
                logger.exception("Error in worker poll loop")
            time.sleep(2)

    def _poll_for_work(self):
        jobs = list_jobs(self._db_path)
        for job in jobs:
            if job["status"] == "cleaning":
                self._process_cleaning(job)
                return
            if job["status"] == "synthesizing":
                self._process_synthesis(job)
                return

    def _process_cleaning(self, job):
        job_id = job["id"]
        self._current_job_id = job_id
        self._cancel_event.clear()
        job_dir = self._jobs_dir / job_id

        logger.info("Cleaning job %s (%s)", job_id, job["filename"])
        log_activity(job_dir, "clean_start", f"Cleaning started for {job['filename']}")

        try:
            # Find uploaded file
            pattern = str(self._uploads_dir / f"{job_id}_*")
            matches = glob.glob(pattern)
            if not matches:
                raise FileNotFoundError(f"Upload file not found for job {job_id}")
            upload_path = Path(matches[0])

            # Detect extension (handle double extensions like .md.docx)
            if upload_path.name.lower().endswith(".md.docx"):
                ext = ".docx"
            else:
                ext = upload_path.suffix.lower()

            extractor = EXTRACTORS.get(ext)
            if not extractor:
                raise ValueError(f"No extractor for extension '{ext}'")

            # Extract and clean
            raw_text = extractor(upload_path)
            if not raw_text or not raw_text.strip():
                raise ValueError("Extracted text is empty")

            cleaned = clean_for_tts(raw_text)
            chunks = chunk_text(cleaned, max_chars=4000)

            # Create cleaned directory
            cleaned_dir = job_dir / "cleaned"
            cleaned_dir.mkdir(parents=True, exist_ok=True)

            seg_index = 0
            for i, chunk_content in enumerate(chunks):
                if self._cancel_event.is_set():
                    logger.info("Job %s cancelled during cleaning", job_id)
                    return

                chunk_filename = f"part{i:03d}.txt"
                chunk_path = cleaned_dir / chunk_filename
                chunk_path.write_text(chunk_content, encoding="utf-8")

                segments = split_into_segments(
                    chunk_content,
                    title_pause=job["title_pause"],
                    para_pause=job["para_pause"],
                )

                for seg in segments:
                    create_segment(
                        self._db_path,
                        job_id=job_id,
                        seg_index=seg_index,
                        chunk_file=chunk_filename,
                        source_text=seg["text"],
                        is_title=1 if is_title_line(seg["text"]) else 0,
                        pause_after=seg["pause_after"],
                    )
                    seg_index += 1

            update_job(
                self._db_path, job_id, status="review", segments_total=seg_index
            )
            logger.info(
                "Job %s cleaned: %d chunks, %d segments", job_id, len(chunks), seg_index
            )
            log_activity(
                job_dir,
                "clean_done",
                f"Cleaning complete: {len(chunks)} chunk(s), {seg_index} segment(s)",
            )

        except Exception as e:
            tb = traceback.format_exc()
            friendly = self._friendly_error(e)
            logger.error("Job %s failed: %s", job_id, tb)
            update_job(
                self._db_path,
                job_id,
                status="failed",
                error_msg=friendly,
                error_detail=tb,
            )
            log_activity(job_dir, "clean_error", friendly)

        finally:
            self._current_job_id = None

    def _process_synthesis(self, job):
        job_id = job["id"]
        self._current_job_id = job_id
        self._cancel_event.clear()
        job_dir = self._jobs_dir / job_id

        logger.info("Synthesis job %s started", job_id)
        log_activity(job_dir, "synthesis_start", f"Synthesis started for {job['filename']}")

        try:
            # Prepare reference audio
            ref_path = self._ref_dir / job["voice_ref"]
            ref_audio_path = prepare_reference_audio(ref_path)
            ref_text = transcribe_reference(ref_audio_path)

            # Load models (heavy imports deferred to here)
            from f5_tts.api import F5TTS
            import whisper

            logger.info("Loading F5-TTS model")
            tts = F5TTS()
            logger.info("Loading Whisper model")
            whisper_model = whisper.load_model("base")

            # Get segments from DB
            segments = get_segments(self._db_path, job_id)
            total = len(segments)
            update_job(self._db_path, job_id, segments_total=total)

            # Audio output directory
            audio_dir = job_dir / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)

            all_parts = []  # list of raw PCM bytes (segments + silence)
            max_retries = 2

            for seg in segments:
                # Check for cancellation
                if self._cancel_event.is_set():
                    logger.info("Job %s cancelled during synthesis", job_id)
                    update_job(self._db_path, job_id, status="cancelled")
                    return

                seg_idx = seg["seg_index"]
                source_text = seg["source_text"]
                is_title = seg["is_title"]
                pause_after = seg["pause_after"]

                # Skip segments already completed (retry support)
                if seg["audio_file"] and seg["word_timings_json"]:
                    existing_path = audio_dir / seg["audio_file"]
                    if existing_path.exists():
                        existing_pcm = existing_path.read_bytes()
                        # Strip wav header -- read the raw frames
                        with wave_mod.open(str(existing_path), "rb") as wf:
                            existing_pcm = wf.readframes(wf.getnframes())
                        all_parts.append(existing_pcm)
                        if pause_after > 0:
                            all_parts.append(generate_silence(pause_after))
                        logger.debug("Segment %d already done, skipping", seg_idx)
                        continue

                log_activity(job_dir, "segment_started", f"Segment {seg_idx}", seg_index=seg_idx)

                # For titles, append punctuation so F5-TTS trails off naturally
                tts_text = source_text
                if is_title and tts_text and tts_text[-1] not in ".!?:;,":
                    tts_text = tts_text + "."

                best_pcm = None
                best_accuracy = 0.0
                best_timings = []

                for attempt in range(1, max_retries + 1):
                    pcm = synthesize_segment(
                        tts, ref_audio_path, ref_text, tts_text, job["speed"]
                    )

                    if not pcm:
                        logger.warning(
                            "Segment %d attempt %d returned no audio", seg_idx, attempt
                        )
                        continue

                    # Verify longer segments with Whisper
                    if len(source_text) > 20:
                        from qa import verify_segment
                        seg_qa = verify_segment(
                            source_text, pcm, seg_idx, whisper_model
                        )
                        accuracy = seg_qa.accuracy
                        timings = seg_qa.word_timings
                    else:
                        # Short segments (titles etc.) -- skip verification
                        accuracy = 1.0
                        timings = []

                    if accuracy > best_accuracy:
                        best_pcm = pcm
                        best_accuracy = accuracy
                        best_timings = timings

                    if accuracy >= 0.90:
                        break

                    if attempt < max_retries:
                        logger.info(
                            "Segment %d accuracy %.1f%%, retrying (%d/%d)",
                            seg_idx, accuracy * 100, attempt, max_retries,
                        )

                if not best_pcm:
                    logger.error("Segment %d produced no usable audio", seg_idx)
                    best_pcm = generate_silence(0.5)
                    best_accuracy = 0.0
                    best_timings = []

                # Save per-segment wav
                seg_filename = f"seg_{seg_idx:04d}.wav"
                seg_path = audio_dir / seg_filename
                with wave_mod.open(str(seg_path), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(24000)
                    wf.writeframes(best_pcm)

                duration = len(best_pcm) / (24000 * 2)

                # Serialize word timings
                timings_json = json.dumps([
                    {
                        "word": wt.word,
                        "start": wt.start,
                        "end": wt.end,
                        "gap_before": wt.gap_before,
                    }
                    for wt in best_timings
                ])

                # Update segment in DB
                update_segment(
                    self._db_path,
                    job_id,
                    seg_idx,
                    audio_file=seg_filename,
                    word_timings_json=timings_json,
                    accuracy=best_accuracy,
                    duration_seconds=round(duration, 2),
                )

                # Accumulate for final concatenation
                all_parts.append(best_pcm)
                if pause_after > 0:
                    all_parts.append(generate_silence(pause_after))

                # Update job progress
                done = seg_idx + 1
                pct = int((done / total) * 100) if total > 0 else 0
                update_job(
                    self._db_path, job_id, segments_done=done, progress_pct=pct
                )

                log_activity(
                    job_dir,
                    "segment_done",
                    f"Segment {seg_idx} done",
                    seg_index=seg_idx,
                    accuracy=round(best_accuracy, 3),
                    duration=round(duration, 2),
                )

            # Concatenate all parts into final audio
            final_name = f"{job_id}_full.wav"
            final_path = audio_dir / final_name
            concatenate_wav_data(all_parts, final_path)

            update_job(
                self._db_path,
                job_id,
                status="done",
                progress_pct=100,
                final_audio=final_name,
            )
            logger.info("Job %s synthesis complete: %s", job_id, final_name)
            log_activity(job_dir, "synthesis_done", f"Synthesis complete: {final_name}")

        except Exception as e:
            tb = traceback.format_exc()
            friendly = self._friendly_error(e)
            logger.error("Job %s synthesis failed: %s", job_id, tb)
            update_job(
                self._db_path,
                job_id,
                status="failed",
                error_msg=friendly,
                error_detail=tb,
            )
            log_activity(job_dir, "error", friendly)

        finally:
            self._current_job_id = None

    @staticmethod
    def _friendly_error(e):
        msg = str(e)
        lower = msg.lower()
        if "cuda out of memory" in lower or "oom" in lower:
            return (
                "The voice model ran out of memory. "
                "Try closing other GPU applications or using a smaller file."
            )
        if "no such file" in lower or "not found" in lower:
            return f"A required file was not found: {msg}"
        if "extractor" in lower:
            return (
                "Couldn't extract text from this file. "
                "The format may be unsupported or the file may be corrupted."
            )
        return f"Something went wrong: {msg}"
