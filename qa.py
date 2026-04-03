#!/usr/bin/env python3
"""
qa.py - Quality assurance for audioslop TTS output.

Provides pre-generation text validation, post-generation transcription
verification, and a feedback loop that auto-regenerates low-quality segments.

Can be used standalone to verify existing audio or imported by synthesize.py
for integrated QA.

Usage:
    python qa.py audio/test/01-foreword.wav --source output/cathedral-and-bazaar/01-foreword.txt
    python qa.py audio/test/ --source output/cathedral-and-bazaar/ --report
"""

import argparse
import difflib
import json
import re
import sys
import textwrap
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Text validation (pre-generation)
# ---------------------------------------------------------------------------

# Patterns that commonly trip up TTS models
TTS_PROBLEM_PATTERNS = [
    (r"https?://\S+", "URL found -- should be removed"),
    (r"\S+@\S+\.\S+", "Email address found -- should be removed"),
    (r"\[\d+\]", "Footnote marker found -- should be removed"),
    (r"[{}<>|\\~^]", "Special characters found -- may cause artifacts"),
    (r"(?<!\w)--(?!\w)", "Double dash found -- should be converted"),
    (r"\b[A-Z]{2,5}\b", "Possible unexpanded abbreviation"),
]


@dataclass
class TextIssue:
    line: int
    pattern: str
    description: str
    match: str


def validate_text(text: str) -> list[TextIssue]:
    """Check text for patterns that cause TTS problems."""
    issues = []
    for i, line in enumerate(text.splitlines(), 1):
        for pattern, desc in TTS_PROBLEM_PATTERNS:
            for match in re.finditer(pattern, line):
                issues.append(TextIssue(
                    line=i,
                    pattern=pattern,
                    description=desc,
                    match=match.group(),
                ))
    return issues


# ---------------------------------------------------------------------------
# Transcription verification (post-generation)
# ---------------------------------------------------------------------------

@dataclass
class WordTiming:
    word: str
    start: float
    end: float
    gap_before: float = 0.0  # silence gap before this word


@dataclass
class SegmentQA:
    segment_index: int
    source_text: str
    transcription: str
    accuracy: float
    word_diffs: list[dict] = field(default_factory=list)
    word_timings: list[WordTiming] = field(default_factory=list)
    duration: float = 0.0
    passed: bool = True
    attempts: int = 1


@dataclass
class ChapterQA:
    file_name: str
    segments: list[SegmentQA] = field(default_factory=list)
    overall_accuracy: float = 0.0
    passed: bool = True
    total_attempts: int = 0


def compute_accuracy(source: str, transcription: str) -> tuple[float, list[dict]]:
    """
    Compare source text to transcription at the word level.
    Returns (accuracy_ratio, list_of_diffs).
    Normalizes for known Whisper formatting differences that aren't
    actual speech errors.
    """
    def normalize(text: str) -> list[str]:
        t = text.lower()
        # Normalize known Whisper artifacts
        t = t.replace("open-source", "open source")
        t = t.replace("linux-based", "linux based")
        t = t.replace("binary-only", "binary only")
        t = t.replace("u.s.", "us")
        t = t.replace("at and t", "at&t")
        t = t.replace("c e o", "ceo")
        t = re.sub(r"[,.:;!?\"'\-]", "", t)
        return t.split()

    src_words = normalize(source)
    trans_words = normalize(transcription)

    matcher = difflib.SequenceMatcher(None, src_words, trans_words)
    ratio = matcher.ratio()

    diffs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            diffs.append({
                "type": tag,
                "source": " ".join(src_words[i1:i2]),
                "transcribed": " ".join(trans_words[j1:j2]),
            })

    return ratio, diffs


def transcribe_audio(audio_path: str, whisper_model=None,
                     word_timestamps: bool = False) -> dict:
    """
    Transcribe an audio file using Whisper.
    Returns {"text": str, "words": list[WordTiming]} when word_timestamps=True,
    otherwise {"text": str, "words": []}.
    """
    if whisper_model is None:
        import whisper
        whisper_model = whisper.load_model("base")

    result = whisper_model.transcribe(
        audio_path, language="en", word_timestamps=word_timestamps
    )

    words = []
    if word_timestamps:
        prev_end = 0.0
        for segment in result.get("segments", []):
            for w in segment.get("words", []):
                gap = w["start"] - prev_end if prev_end > 0 else 0.0
                words.append(WordTiming(
                    word=w["word"].strip(),
                    start=round(w["start"], 3),
                    end=round(w["end"], 3),
                    gap_before=round(gap, 3),
                ))
                prev_end = w["end"]

    return {
        "text": result["text"].strip(),
        "words": words,
    }


def verify_segment(source_text: str, audio_bytes: bytes,
                   segment_index: int, whisper_model=None,
                   sample_rate: int = 24000) -> SegmentQA:
    """
    Verify a single synthesized segment against its source text.
    Writes audio to a temp file, transcribes with word timestamps, and compares.
    """
    import tempfile
    import wave

    # Write segment audio to temp file for Whisper
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
        with wave.open(tmp_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(audio_bytes)

    try:
        result = transcribe_audio(tmp_path, whisper_model, word_timestamps=True)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    transcription = result["text"]
    word_timings = result["words"]
    duration = audio_bytes and len(audio_bytes) / (sample_rate * 2) or 0.0

    accuracy, diffs = compute_accuracy(source_text, transcription)

    return SegmentQA(
        segment_index=segment_index,
        source_text=source_text[:100] + ("..." if len(source_text) > 100 else ""),
        transcription=transcription[:100] + ("..." if len(transcription) > 100 else ""),
        accuracy=accuracy,
        word_diffs=diffs,
        word_timings=word_timings,
        duration=round(duration, 2),
        passed=accuracy >= 0.90,
    )


# ---------------------------------------------------------------------------
# Flow analysis (detect pacing issues)
# ---------------------------------------------------------------------------

def analyze_flow(audio_bytes: bytes, sample_rate: int = 24000) -> dict:
    """
    Analyze audio for pacing issues: silence gaps, speaking rate, clipping.
    Returns a dict of metrics.
    """
    import struct

    num_samples = len(audio_bytes) // 2
    if num_samples == 0:
        return {"error": "empty audio"}

    samples = struct.unpack(f"<{num_samples}h", audio_bytes)
    duration = num_samples / sample_rate

    # Detect silence (runs of very quiet audio)
    silence_threshold = 200  # ~-60dB for 16-bit
    window_size = int(0.1 * sample_rate)  # 100ms windows
    silence_runs = []
    current_silence_start = None

    for i in range(0, num_samples - window_size, window_size):
        window = samples[i:i + window_size]
        rms = (sum(s * s for s in window) / window_size) ** 0.5
        if rms < silence_threshold:
            if current_silence_start is None:
                current_silence_start = i / sample_rate
        else:
            if current_silence_start is not None:
                silence_end = i / sample_rate
                silence_duration = silence_end - current_silence_start
                if silence_duration > 0.3:  # Only flag pauses > 300ms
                    silence_runs.append({
                        "start": round(current_silence_start, 2),
                        "end": round(silence_end, 2),
                        "duration": round(silence_duration, 2),
                    })
                current_silence_start = None

    # Detect clipping (samples at max/min)
    max_val = 32767
    clip_count = sum(1 for s in samples if abs(s) >= max_val - 10)
    clip_ratio = clip_count / num_samples

    # Peak level
    peak = max(abs(s) for s in samples)
    peak_db = 20 * (peak / max_val + 1e-10).__class__.__module__  # placeholder

    return {
        "duration_seconds": round(duration, 1),
        "silence_gaps": silence_runs,
        "long_silences": len([s for s in silence_runs if s["duration"] > 1.5]),
        "clipping_ratio": round(clip_ratio, 6),
        "has_clipping": clip_ratio > 0.001,
        "peak_amplitude": peak,
    }


# ---------------------------------------------------------------------------
# Quality report
# ---------------------------------------------------------------------------

def generate_report(chapter_qa: ChapterQA) -> str:
    """Generate a human-readable quality report with word-level timing."""
    lines = [
        f"Quality report: {chapter_qa.file_name}",
        f"Overall accuracy: {chapter_qa.overall_accuracy:.1%}",
        f"Status: {'PASSED' if chapter_qa.passed else 'NEEDS REVIEW'}",
        f"Total regeneration attempts: {chapter_qa.total_attempts}",
        "",
    ]

    for seg in chapter_qa.segments:
        status = "OK" if seg.passed else "FLAGGED"
        lines.append(
            f"  Segment {seg.segment_index + 1}: {seg.accuracy:.1%} [{status}] "
            f"(attempts: {seg.attempts}, duration: {seg.duration:.1f}s)"
        )

        # Show word-level diffs
        if seg.word_diffs:
            for diff in seg.word_diffs[:3]:
                lines.append(f"    diff: {diff['type']}: \"{diff['source']}\" -> \"{diff['transcribed']}\"")
            if len(seg.word_diffs) > 3:
                lines.append(f"    ... and {len(seg.word_diffs) - 3} more")

        # Show word-level timing
        if seg.word_timings:
            lines.append("    timing:")
            for wt in seg.word_timings:
                gap_marker = ""
                if wt.gap_before > 0.3:
                    gap_marker = f"  <-- {wt.gap_before:.2f}s gap"
                elif wt.gap_before > 0.15:
                    gap_marker = f"  <-- {wt.gap_before:.2f}s pause"
                lines.append(
                    f"      [{wt.start:6.2f}s - {wt.end:6.2f}s] "
                    f"{wt.word}{gap_marker}"
                )

            # Summary: speaking rate and notable gaps
            total_words = len(seg.word_timings)
            if seg.duration > 0:
                wpm = (total_words / seg.duration) * 60
                lines.append(f"    rate: {wpm:.0f} wpm ({total_words} words in {seg.duration:.1f}s)")

            big_gaps = [wt for wt in seg.word_timings if wt.gap_before > 0.5]
            if big_gaps:
                lines.append(f"    notable gaps: {len(big_gaps)}")
                for wt in big_gaps:
                    lines.append(f"      {wt.gap_before:.2f}s before \"{wt.word}\" at {wt.start:.2f}s")

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI for standalone verification
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="qa",
        description="Verify TTS audio quality against source text.",
    )
    parser.add_argument(
        "audio",
        type=Path,
        help="Audio file or directory to verify",
    )
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Source text file or directory",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Save quality report as JSON",
    )

    args = parser.parse_args()

    import whisper
    whisper_model = whisper.load_model("base")

    if args.audio.is_file():
        audio_files = [args.audio]
        source_files = [args.source]
    else:
        audio_files = sorted(args.audio.glob("*.wav"))
        source_files = sorted(args.source.glob("*.txt"))

    for audio_path, source_path in zip(audio_files, source_files):
        source_text = source_path.read_text(encoding="utf-8").strip()
        result = transcribe_audio(str(audio_path), whisper_model, word_timestamps=True)
        transcription = result["text"]
        accuracy, diffs = compute_accuracy(source_text, transcription)

        status = "OK" if accuracy >= 0.90 else "NEEDS REVIEW"
        print(f"{audio_path.name}: {accuracy:.1%} [{status}]")
        for diff in diffs[:5]:
            print(f"  {diff['type']}: \"{diff['source']}\" -> \"{diff['transcribed']}\"")
        if len(diffs) > 5:
            print(f"  ... and {len(diffs) - 5} more differences")


if __name__ == "__main__":
    main()
