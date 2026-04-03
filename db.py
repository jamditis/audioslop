"""SQLite database layer for audioslop job tracking."""

import sqlite3
import uuid
from typing import Optional


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    """Create tables if they don't exist."""
    conn = _connect(db_path)
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                speed REAL DEFAULT 0.85,
                voice_ref TEXT DEFAULT 'amditis.wav',
                title_pause REAL DEFAULT 2.0,
                para_pause REAL DEFAULT 0.75,
                progress_pct INTEGER DEFAULT 0,
                segments_done INTEGER DEFAULT 0,
                segments_total INTEGER DEFAULT 0,
                final_audio TEXT,
                error_msg TEXT,
                error_detail TEXT
            );

            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                seg_index INTEGER NOT NULL,
                chunk_file TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL,
                user_edited INTEGER DEFAULT 0,
                audio_file TEXT,
                word_timings_json TEXT,
                accuracy REAL,
                duration_seconds REAL,
                pause_after REAL DEFAULT 0.0,
                is_title INTEGER DEFAULT 0
            );
        """)
    conn.close()


def create_job(
    db_path: str,
    filename: str,
    speed: float = 0.85,
    voice_ref: str = "amditis.wav",
    title_pause: float = 2.0,
    para_pause: float = 0.75,
) -> str:
    """Create a new job and return its id (uuid hex[:12])."""
    job_id = uuid.uuid4().hex[:12]
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO jobs (id, filename, speed, voice_ref, title_pause, para_pause)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, filename, speed, voice_ref, title_pause, para_pause),
        )
    conn.close()
    return job_id


def get_job(db_path: str, job_id: str) -> Optional[dict]:
    """Return job as dict or None if not found."""
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_job(db_path: str, job_id: str, **fields) -> None:
    """Update arbitrary columns on a job."""
    if not fields:
        return
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    values = list(fields.values()) + [job_id]
    conn = _connect(db_path)
    with conn:
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
    conn.close()


def list_jobs(db_path: str, limit: int = 50) -> list[dict]:
    """Return jobs ordered newest first."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def create_segment(
    db_path: str,
    job_id: str,
    seg_index: int,
    chunk_file: str = "",
    source_text: str = "",
    is_title: int = 0,
    pause_after: float = 0.0,
) -> None:
    """Insert a new segment row."""
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO segments (job_id, seg_index, chunk_file, source_text, is_title, pause_after)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, seg_index, chunk_file, source_text, is_title, pause_after),
        )
    conn.close()


def get_segments(db_path: str, job_id: str) -> list[dict]:
    """Return all segments for a job, ordered by seg_index."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM segments WHERE job_id = ? ORDER BY seg_index",
        (job_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_segment(db_path: str, job_id: str, seg_index: int, **fields) -> None:
    """Update arbitrary columns on a segment identified by job_id + seg_index."""
    if not fields:
        return
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    values = list(fields.values()) + [job_id, seg_index]
    conn = _connect(db_path)
    with conn:
        conn.execute(
            f"UPDATE segments SET {set_clause} WHERE job_id = ? AND seg_index = ?",
            values,
        )
    conn.close()


def delete_job_cascade(db_path: str, job_id: str) -> None:
    """Delete a job and all its segments (cascade via foreign key)."""
    conn = _connect(db_path)
    with conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.close()
