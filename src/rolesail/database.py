"""RoleSail database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rolesail.config import DB_PATH

JOB_RETENTION_DAYS = 7

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            company               TEXT,
            company_logo          TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,
            posted_at             TEXT,
            discovery_status      TEXT DEFAULT 'accepted',
            discovery_rejection_reason TEXT,
            discovery_checked_at  TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            stages_json TEXT NOT NULL,
            current_stage TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error_summary TEXT,
            result_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            created_at TEXT NOT NULL,
            stage TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reported_cost_microusd INTEGER,
            estimated_cost_microusd INTEGER,
            cost_kind TEXT NOT NULL,
            pricing_json TEXT NOT NULL,
            error TEXT,
            FOREIGN KEY(run_id) REFERENCES pipeline_runs(id)
        )
    """)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS outreach_batches (
            id TEXT PRIMARY KEY,
            job_url TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'queued',
            company_domain TEXT,
            company_research_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT,
            completed_at TEXT,
            FOREIGN KEY(job_url) REFERENCES jobs(url)
        );

        CREATE TABLE IF NOT EXISTS outreach_recipients (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            apollo_person_id TEXT NOT NULL,
            apollo_contact_id TEXT,
            apollo_message_id TEXT,
            gmail_draft_id TEXT,
            gmail_account_email TEXT,
            first_name TEXT,
            last_name TEXT,
            title TEXT,
            linkedin_url TEXT,
            email TEXT,
            email_status TEXT,
            relevance_score INTEGER,
            relevance_reason TEXT,
            subject TEXT,
            body_text TEXT,
            source_facts_json TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            status_before_suppression TEXT,
            error TEXT,
            sent_at TEXT,
            scheduled_for TEXT,
            wave INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(batch_id, apollo_person_id),
            FOREIGN KEY(batch_id) REFERENCES outreach_batches(id)
        );

        CREATE TABLE IF NOT EXISTS company_research (
            domain TEXT PRIMARY KEY,
            facts_json TEXT NOT NULL,
            sources_json TEXT NOT NULL,
            researched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS outreach_suppressions (
            key TEXT PRIMARY KEY,
            reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_outreach_batches_status
            ON outreach_batches(status);
        CREATE INDEX IF NOT EXISTS idx_outreach_recipients_batch
            ON outreach_recipients(batch_id, status);
        CREATE INDEX IF NOT EXISTS idx_outreach_recipients_email
            ON outreach_recipients(email);
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_usage_created_at ON llm_usage(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_usage_run_id ON llm_usage(run_id)"
    )
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    ensure_outreach_columns(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outreach_recipients_due "
        "ON outreach_recipients(status, scheduled_for)"
    )
    conn.commit()
    normalize_relative_posted_dates(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "company": "TEXT",
    "company_logo": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    "posted_at": "TEXT",
    "discovery_status": "TEXT DEFAULT 'accepted'",
    "discovery_rejection_reason": "TEXT",
    "discovery_checked_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
}

_OUTREACH_RECIPIENT_COLUMNS: dict[str, str] = {
    "scheduled_for": "TEXT",
    "wave": "INTEGER",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "last_attempt_at": "TEXT",
    "gmail_draft_id": "TEXT",
    "gmail_account_email": "TEXT",
    "status_before_suppression": "TEXT",
}


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def ensure_outreach_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Forward-migrate durable outreach scheduling fields."""
    if conn is None:
        conn = get_connection()
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(outreach_recipients)").fetchall()
    }
    added: list[str] = []
    for column, dtype in _OUTREACH_RECIPIENT_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE outreach_recipients ADD COLUMN {column} {dtype}")
            added.append(column)
    if added:
        conn.commit()
    return added


_RELATIVE_POSTED_RE = re.compile(
    r"^(?:posted\s+)?(?:(\d+)\+?\s+)?(hour|day|week|month)s?\s+ago$",
    re.IGNORECASE,
)


def normalize_posted_at(value: str | None, reference_at: str | None = None) -> str | None:
    """Convert a relative posting label to an ISO date.

    ``reference_at`` should be the timestamp at which the relative label was
    captured. Using it instead of the current time keeps legacy conversions
    accurate and idempotent.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    normalized = re.sub(r"^posted\s+", "", text, flags=re.IGNORECASE).strip()
    lowered = normalized.lower()
    relative_days: float | None = None
    if lowered == "today":
        relative_days = 0
    elif lowered == "yesterday":
        relative_days = 1
    else:
        match = _RELATIVE_POSTED_RE.fullmatch(text)
        if match:
            amount = int(match.group(1) or 1)
            relative_days = amount * {
                "hour": 1 / 24,
                "day": 1,
                "week": 7,
                "month": 30,
            }[match.group(2).lower()]

    if relative_days is None:
        return text

    try:
        reference = datetime.fromisoformat((reference_at or "").replace("Z", "+00:00"))
    except ValueError:
        reference = datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return (reference - timedelta(days=relative_days)).date().isoformat()


def normalize_relative_posted_dates(conn: sqlite3.Connection) -> int:
    """Replace relative posting labels in existing jobs with concrete dates."""
    rows = conn.execute(
        "SELECT url, posted_at, discovered_at FROM jobs "
        "WHERE lower(posted_at) LIKE '%today%' "
        "OR lower(posted_at) LIKE '%yesterday%' "
        "OR lower(posted_at) LIKE '%ago%'"
    ).fetchall()
    updates = []
    for row in rows:
        normalized = normalize_posted_at(row[1], row[2])
        if normalized and normalized != row[1]:
            updates.append((normalized, row[0]))
    if updates:
        conn.executemany("UPDATE jobs SET posted_at = ? WHERE url = ?", updates)
        conn.commit()
    return len(updates)


def _parse_job_date(value: str | None, reference_at: str | None = None) -> datetime | None:
    """Parse a stored job date, including source-specific display formats."""
    normalized = normalize_posted_at(value, reference_at)
    if not normalized:
        return None

    text = normalized.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for pattern in ("%B %d, %Y", "%b %d, %Y"):
            try:
                parsed = datetime.strptime(text, pattern).replace(tzinfo=UTC)
                break
            except ValueError:
                continue

    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def is_job_within_retention_window(
    posted_at: str | None,
    *,
    reference_at: str | None = None,
    days: int = JOB_RETENTION_DAYS,
    now: datetime | None = None,
) -> bool:
    """Return whether a discovered posting is recent enough to retain.

    Postings without a usable source date are accepted because their age is
    unknown. ``reference_at`` anchors relative labels such as ``2 days ago``.
    """
    if days < 1:
        raise ValueError("days must be at least 1")

    posted = _parse_job_date(posted_at, reference_at)
    if posted is None:
        return True

    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return posted >= reference - timedelta(days=days)


def delete_jobs_older_than(
    conn: sqlite3.Connection | None = None,
    *,
    days: int = JOB_RETENTION_DAYS,
    now: datetime | None = None,
) -> int:
    """Delete jobs whose posting date is older than the retention window.

    Applied jobs are always retained. For unapplied jobs, ``posted_at`` is
    authoritative when it can be parsed. Jobs without a usable posting date
    fall back to ``discovered_at`` so legacy and imported rows are still
    subject to retention.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    if conn is None:
        conn = get_connection()

    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff = reference - timedelta(days=days)

    urls: list[tuple[str]] = []
    rows = conn.execute(
        "SELECT url, posted_at, discovered_at FROM jobs WHERE applied_at IS NULL"
    ).fetchall()
    for row in rows:
        job_date = _parse_job_date(row[1], row[2])
        if job_date is None:
            job_date = _parse_job_date(row[2])
        if job_date is not None and job_date < cutoff:
            urls.append((row[0],))

    if urls:
        conn.executemany("DELETE FROM jobs WHERE url = ?", urls)
        conn.commit()
    return len(urls)


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    active = "COALESCE(discovery_status, 'accepted') = 'accepted'"
    stats["total"] = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {active}").fetchone()[0]
    stats["discovery_rejected"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE discovery_status = 'rejected'"
    ).fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        f"SELECT site, COUNT(*) as cnt FROM jobs WHERE {active} GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL AND {active}"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND {active}"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL AND {active}"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        f"WHERE full_description IS NOT NULL AND fit_score IS NULL AND {active}"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND applied_at IS NULL AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND applied_at IS NULL AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
    ).fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(UTC).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        if not is_job_within_retention_window(job.get("posted_at"), reference_at=now):
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at, posted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now, job.get("posted_at")),
            )
            new += 1
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE jobs SET posted_at = COALESCE(posted_at, ?) WHERE url = ?",
                (job.get("posted_at"), url),
            )
            existing += 1

    conn.commit()
    return new, existing


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL AND COALESCE(discovery_status, 'accepted') = 'accepted'",
        "enriched": "full_description IS NOT NULL AND COALESCE(discovery_status, 'accepted') = 'accepted'",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL AND COALESCE(discovery_status, 'accepted') = 'accepted'",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND applied_at IS NULL AND tailored_resume_path IS NULL "
            "AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    query = (
        f"SELECT * FROM jobs WHERE {where} "
        "ORDER BY fit_score DESC NULLS LAST, COALESCE(posted_at, discovered_at) DESC"
    )
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []
