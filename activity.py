"""Per-job activity logging to JSONL files."""

import json
from datetime import datetime, timezone
from pathlib import Path


def log_activity(job_dir: Path, event: str, msg: str, **extra) -> None:
    """Append a JSON line to {job_dir}/activity.jsonl."""
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "event": event,
        "msg": msg,
        **extra,
    }
    log_file = job_dir / "activity.jsonl"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_activity(job_dir: Path) -> list[dict]:
    """Read all entries from {job_dir}/activity.jsonl. Returns empty list if file absent."""
    log_file = Path(job_dir) / "activity.jsonl"
    if not log_file.exists():
        return []
    entries = []
    with log_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries
