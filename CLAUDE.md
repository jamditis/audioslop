# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**audioslop** is a two-stage document-to-audiobook pipeline:
1. `audioslop.py` — Extracts text from documents, cleans it for TTS, outputs size-chunked .txt files
2. `synthesize.py` — Feeds cleaned chunks to F5-TTS with zero-shot voice cloning, outputs .wav files

## Pipeline usage

```bash
# Process a directory of files
python audioslop.py content/cathedral-and-bazaar/ -o output/

# Process a single file
python audioslop.py myfile.pdf --chunk-size 3000

# Preview without writing files
python audioslop.py content/ --dry-run

# Limit to specific formats
python audioslop.py content/ --formats docx,pdf,srt
```

Supported input formats: `.docx`, `.pdf`, `.srt`, `.txt`, `.md`

## Synthesis usage

```bash
# Synthesize all chunks with a cloned voice
python synthesize.py output/cathedral-and-bazaar/ --ref-audio voice.wav --ref-text "Transcript of the voice clip."

# Synthesize and concatenate into a single audiobook file
python synthesize.py output/cathedral-and-bazaar/ --ref-audio voice.wav --ref-text "Transcript." --concat

# Read transcript from file instead of inline
python synthesize.py output/book/ --ref-audio voice.wav --ref-text-file voice_transcript.txt

# Adjust speed
python synthesize.py output/book/ --ref-audio voice.wav --ref-text "Hello." --speed 0.9

# Preview without generating audio
python synthesize.py output/book/ --ref-audio voice.wav --ref-text "Hello." --dry-run
```

F5-TTS uses zero-shot voice cloning: provide a 5-15 second .wav clip of any voice + its transcript, and the model clones that voice. Reference audio quality directly affects output quality — use clean recordings with no background noise.

## Architecture

`audioslop.py` is a single-file pipeline with four stages:

1. **Extraction** — Format-specific readers (`extract_docx`, `extract_pdf`, `extract_srt`, `extract_markdown`, `extract_txt`) pull raw text from each file type.
2. **Cleaning** (`clean_for_tts`) — Strips/transforms content that trips up voice models: abbreviations expanded to letter-by-letter pronunciation, dashes converted to pauses, footnotes/URLs/email addresses removed, parentheticals converted to appositive phrases, special characters cleaned.
3. **Chunking** (`chunk_text`) — Splits cleaned text at paragraph then sentence boundaries, never mid-sentence. Default max 4000 chars per chunk.
4. **Output** — Writes numbered `.txt` files. Single-chunk files get no suffix; multi-chunk files get `_part001.txt`, `_part002.txt`, etc.

## Key design decisions

- **Abbreviation dictionary** (`ABBREVIATIONS` dict) maps acronyms to spoken forms. Add entries here when a new domain introduces abbreviations the TTS model mispronounces.
- **Parenthetical handling** — Long parentheticals become appositives (commas); bare years like `(1991)` are stripped. Short meaningful parentheticals are kept.
- **Chunk splitting priority** — Paragraph boundaries first, sentence boundaries second, never mid-sentence even if it means slightly exceeding `max_chars`.

## Content

Source material lives in `content/` subdirectories. Current content:
- `content/cathedral-and-bazaar/` — 10 .docx files from Eric S. Raymond's "The Cathedral and the Bazaar"

Output goes to `output/` (gitignored).

## Dependencies

- `python-docx` — .docx extraction
- `pdfplumber` — .pdf extraction
- `f5-tts` — voice synthesis (auto-downloads ~1.2GB model on first run)
- Standard library only for .srt, .txt, .md

## Directory layout

- `content/` — Source documents organized by book/project
- `output/` — Cleaned text chunks (from audioslop.py)
- `audio/` — Generated .wav files (from synthesize.py)
- `ref/` — Reference voice audio clips and transcripts

## Web UI

Run with `python app.py`. Flask dev server on port 5000.

Default password: `audioslop` (set via `AUDIOSLOP_PASSWORD` env var).

### Pages

- `/` -- Upload documents, configure settings, view job list
- `/job/{id}` -- Review cleaned text, edit segments, start synthesis, monitor progress
- `/job/{id}/player` -- Audio player with word-level synced transcript highlighting

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUDIOSLOP_PASSWORD` | `audioslop` | Login password |
| `AUDIOSLOP_SECRET` | `dev-secret-change-me` | Flask session secret |

### Worker

The background worker processes one job at a time (FIFO). It runs as a daemon thread inside the Flask process. Jobs go through: pending -> cleaning -> review -> synthesizing -> done.

### Data storage

- `uploads/` -- Raw uploaded files
- `jobs/{id}/cleaned/` -- Cleaned text chunks
- `jobs/{id}/audio/` -- Per-segment .wav files + concatenated full .wav
- `jobs/{id}/activity.jsonl` -- Timestamped event log
- `audioslop.db` -- SQLite database (jobs + segments tables)
- `ref/` -- Voice reference clips for F5-TTS cloning
