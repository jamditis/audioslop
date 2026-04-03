#!/usr/bin/env python3
"""
synthesize.py - Convert cleaned text chunks to audio using F5-TTS.

Takes output from audioslop.py (cleaned .txt chunks) and synthesizes speech
using F5-TTS with zero-shot voice cloning from a reference audio clip.

Usage:
    python synthesize.py output/cathedral-and-bazaar/ --ref-audio voice.wav
    python synthesize.py output/cathedral-and-bazaar/01-foreword.txt --ref-audio voice.wav --speed 0.85
    python synthesize.py output/ --ref-audio voice.wav --ref-text-file transcript.txt -o audiobooks/ --concat
"""

import argparse
import struct
import sys
import textwrap
import time
import wave
from pathlib import Path
from unittest.mock import patch

# Reference audio constraints for F5-TTS.
# Clips over ~12s cause mel-frame cropping errors; 5s is the sweet spot
# for clean voice cloning without reference bleed.
MAX_REF_SECONDS = 5
TARGET_SAMPLE_RATE = 24000

# Pause durations in seconds, inserted as silence between segments
PAUSE_AFTER_TITLE = 2.0
PAUSE_BETWEEN_PARAGRAPHS = 0.75


def prepare_reference_audio(ref_path: Path) -> str:
    """
    Ensure reference audio is mono, 24kHz, and under MAX_REF_SECONDS.
    Returns path to a ready-to-use .wav file.
    """
    import torchaudio

    wav, sr = torchaudio.load(str(ref_path))

    needs_conversion = False

    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
        needs_conversion = True

    if sr != TARGET_SAMPLE_RATE:
        resampler = torchaudio.transforms.Resample(sr, TARGET_SAMPLE_RATE)
        wav = resampler(wav)
        sr = TARGET_SAMPLE_RATE
        needs_conversion = True

    max_samples = MAX_REF_SECONDS * TARGET_SAMPLE_RATE
    if wav.shape[1] > max_samples:
        wav = wav[:, :max_samples]
        needs_conversion = True

    if not needs_conversion:
        return str(ref_path)

    ready_path = ref_path.parent / (ref_path.stem + "_ready.wav")
    torchaudio.save(str(ready_path), wav, TARGET_SAMPLE_RATE)
    duration = wav.shape[1] / TARGET_SAMPLE_RATE
    print(f"Prepared reference audio: mono, {TARGET_SAMPLE_RATE}Hz, {duration:.1f}s")
    return str(ready_path)


def transcribe_reference(ref_audio_path: str) -> str:
    """Use Whisper to get an exact transcription of the reference audio."""
    import whisper
    print("Transcribing reference audio for alignment...")
    model = whisper.load_model("base")
    result = model.transcribe(ref_audio_path, language="en")
    text = result["text"].strip()
    print(f"Reference transcript: {text}")
    return text


def find_text_files(input_path: Path) -> list[Path]:
    """Collect .txt files, sorted by name."""
    if input_path.is_file():
        if input_path.suffix == ".txt":
            return [input_path]
        print(f"Not a .txt file: {input_path}")
        return []
    return sorted(input_path.rglob("*.txt"))


def is_title_line(line: str) -> bool:
    """Detect if a line is a title/heading rather than body text."""
    stripped = line.strip()
    if not stripped:
        return False
    # Short line, doesn't end with sentence punctuation
    if len(stripped) < 60 and not stripped[-1] in ".!?:;,":
        return True
    return False


def split_into_segments(text: str, title_pause: float = PAUSE_AFTER_TITLE,
                        para_pause: float = PAUSE_BETWEEN_PARAGRAPHS) -> list[dict]:
    """
    Split text into structural segments with pause metadata.
    Returns list of {"text": str, "pause_after": float} dicts.

    Title lines are always their own segment, even if separated from body
    text by a single newline rather than a double newline.
    """
    # First, split on double newlines to get paragraphs
    raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not raw_paragraphs:
        raw_paragraphs = [p.strip() for p in text.split("\n") if p.strip()]

    # Then, within each paragraph, split out title lines that appear
    # at the start (e.g. "Foreword\nFreedom is not...")
    paragraphs = []
    for para in raw_paragraphs:
        lines = para.split("\n")
        if len(lines) > 1 and is_title_line(lines[0]):
            paragraphs.append(lines[0].strip())
            rest = "\n".join(lines[1:]).strip()
            if rest:
                paragraphs.append(rest)
        else:
            paragraphs.append(para)

    segments = []
    for i, para in enumerate(paragraphs):
        is_last = i == len(paragraphs) - 1

        if is_title_line(para):
            pause = title_pause if not is_last else 0.0
        else:
            pause = para_pause if not is_last else 0.0

        segments.append({"text": para, "pause_after": pause})

    return segments


def generate_silence(duration_seconds: float, sample_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """Generate silence as raw PCM bytes (16-bit mono)."""
    num_samples = int(duration_seconds * sample_rate)
    return struct.pack(f"<{num_samples}h", *([0] * num_samples))


def concatenate_wav_data(wav_parts: list[bytes], output_path: Path) -> None:
    """Write multiple raw PCM byte segments to a single .wav file."""
    with wave.open(str(output_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)  # 16-bit
        out.setframerate(TARGET_SAMPLE_RATE)
        for part in wav_parts:
            out.writeframes(part)


def concatenate_wavs(wav_files: list[Path], output_path: Path) -> None:
    """Concatenate multiple .wav files into one."""
    if not wav_files:
        return

    with wave.open(str(wav_files[0]), "rb") as first:
        params = first.getparams()

    with wave.open(str(output_path), "wb") as out:
        out.setparams(params)
        for wav_file in wav_files:
            with wave.open(str(wav_file), "rb") as inp:
                out.writeframes(inp.readframes(inp.getnframes()))


def _patch_sequential():
    """
    Force F5-TTS to process batches sequentially.
    F5-TTS v1.1.x has a threading bug: the DiT transformer's cache=True
    causes tensor size mismatches when ThreadPoolExecutor runs multiple
    chunks in parallel. Limiting to 1 worker fixes this.
    """
    from concurrent.futures import ThreadPoolExecutor
    original_init = ThreadPoolExecutor.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["max_workers"] = 1
        original_init(self, *args, **kwargs)

    return patch.object(ThreadPoolExecutor, "__init__", patched_init)


def synthesize_segment(tts, ref_audio_path: str, ref_text: str,
                       text: str, speed: float) -> bytes | None:
    """
    Synthesize a single text segment and return raw PCM bytes.
    Returns None on failure.
    """
    import numpy as np

    with _patch_sequential():
        wav_array, sr, _ = tts.infer(
            ref_file=ref_audio_path,
            ref_text=ref_text,
            gen_text=text,
            speed=speed,
        )

    # Convert float32 numpy array to 16-bit PCM bytes
    if isinstance(wav_array, np.ndarray):
        pcm = (wav_array * 32767).astype(np.int16)
        return pcm.tobytes()
    return None


def run_synthesis(
    input_path: Path,
    output_dir: Path,
    ref_audio: Path,
    ref_text: str,
    speed: float = 0.85,
    concatenate: bool = False,
    dry_run: bool = False,
    verify: bool = True,
    max_retries: int = 2,
    accuracy_threshold: float = 0.90,
) -> None:
    """Synthesize speech from text files using F5-TTS with QA verification."""
    from qa import (
        validate_text, verify_segment, analyze_flow,
        generate_report, ChapterQA, compute_accuracy,
    )

    files = find_text_files(input_path)
    if not files:
        print(f"No .txt files found in {input_path}")
        sys.exit(1)

    print(f"Found {len(files)} text file(s) to synthesize")
    print(f"Reference audio: {ref_audio}")
    print(f"Speed: {speed}x | Verify: {verify} | Max retries: {max_retries}")
    print()

    if dry_run:
        for f in files:
            text = f.read_text(encoding="utf-8").strip()
            segments = split_into_segments(text)
            titles = sum(1 for s in segments if is_title_line(s["text"]))
            # Run text validation
            issues = validate_text(text)
            issue_str = f", {len(issues)} issue(s)" if issues else ""
            print(f"  {f.name} ({len(text)} chars, {len(segments)} segments, {titles} title(s){issue_str})")
            for issue in issues[:3]:
                print(f"    L{issue.line}: {issue.description} -- \"{issue.match}\"")
        print(f"\nWould generate {len(files)} audio file(s)")
        return

    # Prepare reference audio
    ref_audio_path = prepare_reference_audio(ref_audio)

    # Auto-transcribe reference if no text provided
    if not ref_text:
        ref_text = transcribe_reference(ref_audio_path)

    print(f"Reference text: {ref_text[:80]}{'...' if len(ref_text) > 80 else ''}")
    print()

    # Load F5-TTS
    print("Loading F5-TTS model (first run downloads ~1.2GB)...")
    from f5_tts.api import F5TTS
    tts = F5TTS()
    print("Model loaded.")

    # Load Whisper for verification
    whisper_model = None
    if verify:
        import whisper
        print("Loading Whisper for verification...")
        whisper_model = whisper.load_model("base")
    print()

    output_dir.mkdir(parents=True, exist_ok=True)
    generated_wavs = []
    all_reports = []
    total_start = time.time()

    for i, txt_file in enumerate(files, 1):
        text = txt_file.read_text(encoding="utf-8").strip()
        if not text:
            print(f"  [{i}/{len(files)}] {txt_file.name} -- empty, skipping")
            continue

        wav_name = txt_file.stem + ".wav"
        wav_path = output_dir / wav_name

        # Pre-generation text validation
        issues = validate_text(text)
        if issues:
            print(f"  [{i}/{len(files)}] Text validation: {len(issues)} potential issue(s)")
            for issue in issues[:3]:
                print(f"    L{issue.line}: {issue.description} -- \"{issue.match}\"")

        segments = split_into_segments(text)
        print(f"  [{i}/{len(files)}] {txt_file.name} ({len(segments)} segments) -> {wav_name}")
        start = time.time()

        chapter_qa = ChapterQA(file_name=txt_file.name)
        wav_parts = []
        failed = False

        for j, seg in enumerate(segments):
            seg_type = "title" if is_title_line(seg["text"]) else "para"
            seg_preview = seg["text"][:50]

            best_pcm = None
            best_accuracy = 0.0
            attempts = 0

            for attempt in range(1, max_retries + 1):
                attempts = attempt
                label = f"[{j+1}/{len(segments)}]"
                if attempt > 1:
                    label += f" retry {attempt}/{max_retries}"
                print(f"           {label} ({seg_type}) {seg_preview}...")

                # For title segments, append a period so F5-TTS gets a
                # sentence-ending cue and trails off naturally instead of
                # cutting abruptly. Only modify the text sent to TTS --
                # seg["text"] stays unchanged for QA comparison.
                tts_text = seg["text"]
                if seg_type == "title" and tts_text and tts_text[-1] not in ".!?:;,":
                    tts_text = tts_text + "."

                try:
                    pcm = synthesize_segment(
                        tts, ref_audio_path, ref_text, tts_text, speed
                    )
                except Exception as e:
                    print(f"           FAILED: {e}")
                    failed = True
                    break

                if not pcm:
                    failed = True
                    break

                # Verify this segment
                if verify and whisper_model and len(seg["text"]) > 20:
                    seg_qa = verify_segment(
                        seg["text"], pcm, j, whisper_model
                    )
                    print(f"           accuracy: {seg_qa.accuracy:.1%}", end="")

                    if seg_qa.accuracy >= accuracy_threshold:
                        print(" OK")
                        best_pcm = pcm
                        best_accuracy = seg_qa.accuracy
                        seg_qa.attempts = attempt
                        chapter_qa.segments.append(seg_qa)
                        break
                    elif attempt < max_retries:
                        print(" -- regenerating")
                        if seg_qa.accuracy > best_accuracy:
                            best_pcm = pcm
                            best_accuracy = seg_qa.accuracy
                    else:
                        print(f" -- below threshold, using best ({best_accuracy:.1%})")
                        if pcm and (not best_pcm or seg_qa.accuracy > best_accuracy):
                            best_pcm = pcm
                            best_accuracy = seg_qa.accuracy
                        seg_qa.attempts = attempt
                        seg_qa.passed = False
                        chapter_qa.segments.append(seg_qa)
                else:
                    # No verification for very short segments (titles, etc.)
                    best_pcm = pcm
                    best_accuracy = 1.0
                    print(f"           (skipped verification for short segment)")
                    break

            if failed:
                break

            if best_pcm:
                wav_parts.append(best_pcm)
                chapter_qa.total_attempts += attempts
                if seg["pause_after"] > 0:
                    wav_parts.append(generate_silence(seg["pause_after"]))

        if wav_parts and not failed:
            concatenate_wav_data(wav_parts, wav_path)
            elapsed = time.time() - start
            total_bytes = sum(len(p) for p in wav_parts)
            audio_duration = total_bytes / (TARGET_SAMPLE_RATE * 2)

            # Overall accuracy
            if chapter_qa.segments:
                chapter_qa.overall_accuracy = sum(
                    s.accuracy for s in chapter_qa.segments
                ) / len(chapter_qa.segments)
                chapter_qa.passed = all(s.passed for s in chapter_qa.segments)

            status = "PASSED" if chapter_qa.passed else "NEEDS REVIEW"
            print(f"           done in {elapsed:.1f}s ({audio_duration:.0f}s audio) [{status} {chapter_qa.overall_accuracy:.1%}]")
            generated_wavs.append(wav_path)
            all_reports.append(chapter_qa)

            # Save QA report
            report_path = output_dir / (txt_file.stem + "_qa.txt")
            report_path.write_text(generate_report(chapter_qa), encoding="utf-8")

        elif wav_parts:
            concatenate_wav_data(wav_parts, wav_path)
            elapsed = time.time() - start
            print(f"           partial in {elapsed:.1f}s")
            generated_wavs.append(wav_path)

    total_elapsed = time.time() - total_start
    print(f"\nGenerated {len(generated_wavs)} audio file(s) in {total_elapsed:.1f}s")

    # Summary
    if all_reports:
        passed = sum(1 for r in all_reports if r.passed)
        avg_acc = sum(r.overall_accuracy for r in all_reports) / len(all_reports)
        total_retries = sum(r.total_attempts for r in all_reports)
        print(f"QA: {passed}/{len(all_reports)} passed, {avg_acc:.1%} average accuracy, {total_retries} total attempts")

    if concatenate and len(generated_wavs) > 1:
        if input_path.is_dir():
            concat_name = input_path.name + "_full.wav"
        else:
            stem = generated_wavs[0].stem.rsplit("_part", 1)[0]
            concat_name = stem + "_full.wav"

        concat_path = output_dir / concat_name
        print(f"Concatenating into {concat_name}...")
        concatenate_wavs(generated_wavs, concat_path)
        print(f"Full audiobook: {concat_path.resolve()}")

    print(f"Output: {output_dir.resolve()}")


def main():
    parser = argparse.ArgumentParser(
        prog="synthesize",
        description="Synthesize speech from cleaned text files using F5-TTS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              python synthesize.py output/book/ --ref-audio voice.wav
              python synthesize.py output/book/ --ref-audio voice.wav --speed 0.85 --concat
              python synthesize.py chapter.txt --ref-audio voice.wav --ref-text "Sample text."
        """),
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Text file or directory of .txt files to synthesize",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("audio"),
        help="Output directory for .wav files (default: audio/)",
    )
    parser.add_argument(
        "--ref-audio",
        type=Path,
        required=True,
        help="Reference audio clip for voice cloning (any format, auto-converted)",
    )
    ref_group = parser.add_mutually_exclusive_group()
    ref_group.add_argument(
        "--ref-text",
        type=str,
        help="Transcript of the reference audio clip (auto-transcribed if omitted)",
    )
    ref_group.add_argument(
        "--ref-text-file",
        type=Path,
        help="File containing the transcript of the reference audio clip",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=0.85,
        help="Playback speed multiplier (default: 0.85)",
    )
    parser.add_argument(
        "--title-pause",
        type=float,
        default=PAUSE_AFTER_TITLE,
        help=f"Seconds of silence after title lines (default: {PAUSE_AFTER_TITLE})",
    )
    parser.add_argument(
        "--para-pause",
        type=float,
        default=PAUSE_BETWEEN_PARAGRAPHS,
        help=f"Seconds of silence between paragraphs (default: {PAUSE_BETWEEN_PARAGRAPHS})",
    )
    parser.add_argument(
        "--concat",
        action="store_true",
        help="Concatenate all chunks into a single audiobook .wav",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synthesized without generating audio",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip transcription verification (faster but no QA)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max regeneration attempts per segment (default: 2)",
    )
    parser.add_argument(
        "--accuracy-threshold",
        type=float,
        default=0.90,
        help="Min accuracy before regeneration (default: 0.90)",
    )

    args = parser.parse_args()

    if not args.input.exists():
        print(f"Input not found: {args.input}")
        sys.exit(1)

    if not args.ref_audio.exists():
        print(f"Reference audio not found: {args.ref_audio}")
        sys.exit(1)

    ref_text = args.ref_text or ""
    if args.ref_text_file:
        if not args.ref_text_file.exists():
            print(f"Reference text file not found: {args.ref_text_file}")
            sys.exit(1)
        ref_text = args.ref_text_file.read_text(encoding="utf-8").strip()

    run_synthesis(
        input_path=args.input,
        output_dir=args.output,
        ref_audio=args.ref_audio,
        ref_text=ref_text,
        speed=args.speed,
        concatenate=args.concat,
        dry_run=args.dry_run,
        verify=not args.no_verify,
        max_retries=args.max_retries,
        accuracy_threshold=args.accuracy_threshold,
    )


if __name__ == "__main__":
    main()
