#!/usr/bin/env python3
"""
audioslop - Convert documents to TTS-ready text.

Extracts text from multiple file formats (.docx, .pdf, .srt, .txt, .md),
cleans it for voice model consumption, and outputs size-chunked .txt files.

Usage:
    python audioslop.py content/cathedral-and-bazaar/ -o output/
    python audioslop.py myfile.pdf --chunk-size 4000
    python audioslop.py content/ --formats docx,pdf --dry-run
"""

import argparse
import os
import re
import sys
import textwrap
from pathlib import Path


# ---------------------------------------------------------------------------
# Format-specific text extractors
# ---------------------------------------------------------------------------

def extract_docx(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs)


def extract_pdf(path: Path) -> str:
    import pdfplumber
    texts = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                texts.append(text)
    return "\n".join(texts)


def extract_srt(path: Path) -> str:
    """Strip sequence numbers and timestamps, keep only subtitle text."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    text_lines = []
    timestamp_re = re.compile(
        r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}"
    )
    for line in lines:
        stripped = line.strip()
        # skip blank lines, sequence numbers, timestamps
        if not stripped:
            continue
        if stripped.isdigit():
            continue
        if timestamp_re.match(stripped):
            continue
        # strip inline HTML tags (e.g. <i>, <b>, <font>)
        cleaned = re.sub(r"<[^>]+>", "", stripped)
        if cleaned:
            text_lines.append(cleaned)
    return "\n".join(text_lines)


def extract_markdown(path: Path) -> str:
    """Strip markdown formatting, keep readable text."""
    text = path.read_text(encoding="utf-8", errors="replace")
    # remove images: ![alt](url)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    # convert links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # remove heading markers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # remove bold/italic markers
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)
    # remove horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # remove code fences
    text = re.sub(r"```[^`]*```", "", text, flags=re.DOTALL)
    # remove inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # remove blockquote markers
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # remove list bullets/numbers but keep content
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)
    return text


def extract_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


EXTRACTORS = {
    ".docx": extract_docx,
    ".pdf": extract_pdf,
    ".srt": extract_srt,
    ".md": extract_markdown,
    ".txt": extract_txt,
}

SUPPORTED_FORMATS = set(EXTRACTORS.keys())


# ---------------------------------------------------------------------------
# TTS text cleaning
# ---------------------------------------------------------------------------

# Abbreviations to expand for spoken clarity.
# Add entries as needed -- keys are case-sensitive.
ABBREVIATIONS = {
    "SMTP": "S M T P",
    "POP": "P O P",
    "POP3": "P O P 3",
    "IMAP": "I M A P",
    "HTTP": "H T T P",
    "HTTPS": "H T T P S",
    "HTML": "H T M L",
    "CSS": "C S S",
    "URL": "U R L",
    "URLs": "U R Ls",
    "API": "A P I",
    "APIs": "A P Is",
    "SQL": "S Q L",
    "GNU": "G N U",
    "GPL": "G P L",
    "TCP": "T C P",
    "IP": "I P",
    "DNS": "D N S",
    "SSH": "S S H",
    "FTP": "F T P",
    "OS": "O S",
    "CPU": "C P U",
    "GPU": "G P U",
    "RAM": "ram",
    "ROM": "rom",
    "BIOS": "bios",
    "IDE": "I D E",
    "CLI": "C L I",
    "GUI": "gooey",
    "USB": "U S B",
    "PDF": "P D F",
    "XML": "X M L",
    "JSON": "jason",
    "YAML": "yamel",
    "ASCII": "ask-ee",
    "UTF": "U T F",
    "UNIX": "unix",
    "IEEE": "I triple E",
    "FAQ": "F A Q",
    "PhD": "P H D",
    "CEO": "C E O",
    "CTO": "C T O",
    "SCCS": "S C C S",
    "RCS": "R C S",
    "CVS": "C V S",
    "RPOP": "R P O P",
    "APOP": "A P O P",
    "RFC": "R F C",
    "AI": "A I",
    "ML": "M L",
    "OEM": "O E M",
    "ISP": "I S P",
    "MIT": "M I T",
    "BSD": "B S D",
    "GCC": "G C C",
    "VC": "V C",
    "GUD": "G U D",
    "PC": "P C",
}


def clean_for_tts(text: str) -> str:
    """Clean extracted text so voice models read it naturally."""

    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove spaced-out dashes used in the source (e.g. "non -- source")
    text = re.sub(r"\s+--\s+", " -- ", text)  # normalize spacing first
    text = re.sub(r"(\w)\s*--\s*(\w)", r"\1-\2", text)  # join hyphenated words

    # Convert remaining double dashes to pause-friendly comma or em-dash
    text = text.replace(" -- ", ", ")
    text = text.replace("--", ", ")

    # Remove footnote/endnote markers: [1], [23], etc.
    text = re.sub(r"\[\d+\]", "", text)

    # Remove URLs
    text = re.sub(r"https?://\S+", "", text)

    # Remove email addresses
    text = re.sub(r"\S+@\S+\.\S+", "", text)

    # Remove file paths (Unix and Windows style)
    text = re.sub(r"(?:/[\w.-]+){2,}", "", text)
    text = re.sub(r"[A-Z]:\\[\w\\.-]+", "", text)

    # Convert parenthetical definitions to appositive phrases
    # e.g. "SMTP (Simple Mail Transfer Protocol)" -> "SMTP, Simple Mail Transfer Protocol,"
    text = re.sub(r"\s*\(([^)]{4,})\)", r", \1,", text)

    # Remove short parentheticals that are just noise (single words, numbers)
    # but keep meaningful short ones
    text = re.sub(r"\s*\(\d{4}\)\s*", " ", text)  # bare years like (1991)

    # Expand abbreviations (whole-word match, case-sensitive)
    for abbr, expansion in ABBREVIATIONS.items():
        text = re.sub(rf"\b{re.escape(abbr)}\b", expansion, text)

    # Spell out common symbols
    text = text.replace("&", " and ")
    text = text.replace("%", " percent")
    text = text.replace("+", " plus ")
    text = text.replace("=", " equals ")
    text = re.sub(r"\$(\d)", r"\1 dollars", text)

    # Remove stray special characters that confuse TTS
    text = re.sub(r"[{}\[\]<>|\\~^]", "", text)

    # Normalize quotes to simple quotes
    text = text.replace("\u201c", '"').replace("\u201d", '"')  # smart double
    text = text.replace("\u2018", "'").replace("\u2019", "'")  # smart single
    text = text.replace("\u2014", ", ")  # em dash
    text = text.replace("\u2013", " to ")  # en dash (often used for ranges)

    # Collapse multiple spaces / blank lines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Clean up punctuation collisions from transformations
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\.\s*,", ".", text)  # period-comma -> period
    text = re.sub(r",\s*\.", ".", text)  # comma-period -> period

    # Strip leading/trailing whitespace per line
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)

    # Final trim
    text = text.strip()

    return text


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_chars: int = 4000) -> list[str]:
    """
    Split text into chunks of approximately max_chars characters.
    Breaks at paragraph boundaries first, then at sentence boundaries.
    Never splits mid-sentence.
    """
    if len(text) <= max_chars:
        return [text]

    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        # If adding this paragraph fits, add it
        candidate = (current + "\n\n" + para).strip() if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue

        # If current chunk is non-empty, save it
        if current:
            chunks.append(current)
            current = ""

        # If the paragraph itself is too long, split by sentences
        if len(para) > max_chars:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sentence in sentences:
                candidate = (
                    (current + " " + sentence).strip() if current else sentence
                )
                if len(candidate) <= max_chars:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    # If a single sentence exceeds max_chars, include it whole
                    # rather than cutting mid-word
                    current = sentence
        else:
            current = para

    if current:
        chunks.append(current)

    return chunks


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def find_files(input_path: Path, formats: set[str]) -> list[Path]:
    """Collect files from a path (file or directory), sorted by name."""
    if input_path.is_file():
        if input_path.suffix.lower() in formats:
            return [input_path]
        # Handle .md.docx double extensions
        if input_path.name.endswith(".md.docx") and ".docx" in formats:
            return [input_path]
        print(f"  Skipping unsupported format: {input_path.suffix}")
        return []

    files = []
    for f in sorted(input_path.rglob("*")):
        if f.is_file() and f.suffix.lower() in formats:
            files.append(f)
    return files


def process_file(path: Path) -> str:
    """Extract text from a file and clean it for TTS."""
    ext = path.suffix.lower()
    # Handle double extension like .md.docx
    if path.name.lower().endswith(".md.docx"):
        ext = ".docx"

    extractor = EXTRACTORS.get(ext)
    if not extractor:
        print(f"  No extractor for {ext}, skipping {path.name}")
        return ""

    raw_text = extractor(path)
    cleaned = clean_for_tts(raw_text)
    return cleaned


def run_pipeline(
    input_path: Path,
    output_dir: Path,
    chunk_size: int = 4000,
    formats: set[str] | None = None,
    dry_run: bool = False,
) -> None:
    """Run the full extraction -> cleaning -> chunking pipeline."""

    if formats is None:
        formats = SUPPORTED_FORMATS

    files = find_files(input_path, formats)
    if not files:
        print(f"No supported files found in {input_path}")
        sys.exit(1)

    print(f"Found {len(files)} file(s) to process")

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    total_chunks = 0

    for fpath in files:
        print(f"  Processing: {fpath.name}")
        cleaned = process_file(fpath)

        if not cleaned.strip():
            print(f"    -> Empty after cleaning, skipping")
            continue

        chunks = chunk_text(cleaned, max_chars=chunk_size)
        total_chunks += len(chunks)

        if dry_run:
            print(f"    -> {len(cleaned)} chars, {len(chunks)} chunk(s)")
            # Show a preview of the first 300 chars
            preview = cleaned[:300].replace("\n", " ")
            print(f"    -> Preview: {preview}...")
            continue

        stem = fpath.stem
        # Strip .md from .md.docx stems
        if stem.endswith(".md"):
            stem = stem[:-3]

        if len(chunks) == 1:
            out_path = output_dir / f"{stem}.txt"
            out_path.write_text(chunks[0], encoding="utf-8")
            print(f"    -> {out_path.name} ({len(chunks[0])} chars)")
        else:
            for i, chunk in enumerate(chunks, 1):
                out_path = output_dir / f"{stem}_part{i:03d}.txt"
                out_path.write_text(chunk, encoding="utf-8")
                print(f"    -> {out_path.name} ({len(chunk)} chars)")

    print(f"\nDone. {total_chunks} total chunk(s) from {len(files)} file(s).")
    if not dry_run:
        print(f"Output: {output_dir.resolve()}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="audioslop",
        description="Convert documents to TTS-ready chunked text files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              python audioslop.py content/ -o output/
              python audioslop.py myfile.pdf --chunk-size 3000
              python audioslop.py content/ --dry-run
              python audioslop.py transcript.srt -o cleaned/
        """),
    )
    parser.add_argument(
        "input",
        type=Path,
        help="File or directory to process",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("output"),
        help="Output directory for cleaned text chunks (default: output/)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=4000,
        help="Max characters per output chunk (default: 4000)",
    )
    parser.add_argument(
        "--formats",
        type=str,
        default=None,
        help="Comma-separated list of formats to process (e.g. docx,pdf,srt)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without writing files",
    )

    args = parser.parse_args()

    if not args.input.exists():
        print(f"Input path not found: {args.input}")
        sys.exit(1)

    formats = SUPPORTED_FORMATS
    if args.formats:
        formats = {
            f".{f.strip().lstrip('.')}" for f in args.formats.split(",")
        }
        unsupported = formats - SUPPORTED_FORMATS
        if unsupported:
            print(f"Unsupported formats: {', '.join(unsupported)}")
            print(f"Supported: {', '.join(sorted(SUPPORTED_FORMATS))}")
            sys.exit(1)

    run_pipeline(
        input_path=args.input,
        output_dir=args.output,
        chunk_size=args.chunk_size,
        formats=formats,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
