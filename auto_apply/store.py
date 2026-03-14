"""SQLite store for jobs, match scores, and application tracking."""

import sqlite3
from datetime import datetime
from typing import Optional
from auto_apply.config import DB_PATH
from auto_apply.models import (
    Job, JobSource, MatchResult, Application,
    ApplicationStatus, ApplyMethod,
)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            salary_min REAL,
            salary_max REAL,
            salary_text TEXT,
            description TEXT DEFAULT '',
            url TEXT NOT NULL,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            posted_date TEXT,
            easy_apply INTEGER DEFAULT 0,
            scraped_at TEXT NOT NULL,
            UNIQUE(source, external_id)
        );

        CREATE TABLE IF NOT EXISTS match_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            score INTEGER NOT NULL,
            reasoning TEXT DEFAULT '',
            matched_skills TEXT DEFAULT '',
            missing_skills TEXT DEFAULT '',
            scored_at TEXT NOT NULL,
            UNIQUE(job_id)
        );

        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            status TEXT NOT NULL DEFAULT 'pending',
            method TEXT NOT NULL DEFAULT 'none',
            applied_at TEXT,
            error_message TEXT,
            UNIQUE(job_id)
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
        CREATE INDEX IF NOT EXISTS idx_jobs_salary ON jobs(salary_min);
        CREATE INDEX IF NOT EXISTS idx_match_score ON match_scores(score);
        CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);
    """)
    conn.commit()
    conn.close()


def upsert_job(job: Job) -> int:
    """Insert or update a job. Returns the job ID."""
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO jobs (title, company, location, salary_min, salary_max,
           salary_text, description, url, source, external_id, posted_date,
           easy_apply, scraped_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source, external_id) DO UPDATE SET
             title=excluded.title, salary_min=excluded.salary_min,
             salary_max=excluded.salary_max, salary_text=excluded.salary_text,
             description=excluded.description, easy_apply=excluded.easy_apply,
             scraped_at=excluded.scraped_at
        """,
        (job.title, job.company, job.location, job.salary_min, job.salary_max,
         job.salary_text, job.description, job.url, job.source.value,
         job.external_id, job.posted_date, int(job.easy_apply),
         job.scraped_at.isoformat()),
    )
    # Get the ID (whether inserted or updated)
    row = conn.execute(
        "SELECT id FROM jobs WHERE source=? AND external_id=?",
        (job.source.value, job.external_id),
    ).fetchone()
    conn.commit()
    conn.close()
    return row["id"]


def record_match(match: MatchResult):
    """Store a match score for a job."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO match_scores (job_id, score, reasoning, matched_skills,
           missing_skills, scored_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET
             score=excluded.score, reasoning=excluded.reasoning,
             matched_skills=excluded.matched_skills,
             missing_skills=excluded.missing_skills,
             scored_at=excluded.scored_at
        """,
        (match.job_id, match.score, match.reasoning,
         ",".join(match.matched_skills), ",".join(match.missing_skills),
         match.scored_at.isoformat()),
    )
    conn.commit()
    conn.close()


def record_application(app: Application):
    """Record an application attempt."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO applications (job_id, status, method, applied_at, error_message)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET
             status=excluded.status, method=excluded.method,
             applied_at=excluded.applied_at, error_message=excluded.error_message
        """,
        (app.job_id, app.status.value, app.method.value,
         app.applied_at.isoformat() if app.applied_at else None,
         app.error_message),
    )
    conn.commit()
    conn.close()


def get_unscored_jobs() -> list[dict]:
    """Get jobs that haven't been scored yet."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT j.* FROM jobs j
           LEFT JOIN match_scores m ON j.id = m.job_id
           WHERE m.id IS NULL
           ORDER BY j.scraped_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unapplied_matches(min_score: int = 70) -> list[dict]:
    """Get scored jobs above threshold that haven't been applied to."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT j.*, m.score, m.reasoning, m.matched_skills, m.missing_skills
           FROM jobs j
           JOIN match_scores m ON j.id = m.job_id
           LEFT JOIN applications a ON j.id = a.job_id
           WHERE m.score >= ? AND a.id IS NULL
           ORDER BY m.score DESC""",
        (min_score,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_jobs_with_status() -> list[dict]:
    """Get all jobs with their match scores and application status."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT j.*, m.score, m.reasoning,
                  a.status as app_status, a.method as app_method, a.applied_at
           FROM jobs j
           LEFT JOIN match_scores m ON j.id = m.job_id
           LEFT JOIN applications a ON j.id = a.job_id
           ORDER BY m.score DESC NULLS LAST, j.scraped_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Get summary statistics."""
    conn = _get_conn()
    stats = {}
    stats["total_jobs"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    stats["scored"] = conn.execute("SELECT COUNT(*) FROM match_scores").fetchone()[0]
    stats["above_threshold"] = conn.execute(
        "SELECT COUNT(*) FROM match_scores WHERE score >= 70"
    ).fetchone()[0]
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status='applied'"
    ).fetchone()[0]
    stats["failed"] = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status='failed'"
    ).fetchone()[0]

    # Per-source breakdown
    source_rows = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM jobs GROUP BY source"
    ).fetchall()
    stats["by_source"] = {r["source"]: r["cnt"] for r in source_rows}

    conn.close()
    return stats


# Auto-init on import
init_db()
