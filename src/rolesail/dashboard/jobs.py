"""Dashboard job import, artifact, and status services."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

import yaml

from rolesail import config
from rolesail.dashboard_data import applied_view
from rolesail.database import get_connection

log = logging.getLogger(__name__)

MAX_URL_LENGTH = 2048
EXTERNAL_EMPLOYER_DOMAINS = {
    "konrad.com": {"name": "Konrad", "greenhouse_board": "konradgroup"},
    "salesforce.com": {"name": "Salesforce", "workday_employer": "salesforce"},
}

def _external_employer(url: str) -> dict | None:
    """Return canonical employer metadata for a recognized careers domain."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    employer = EXTERNAL_EMPLOYER_DOMAINS.get(hostname)
    if employer:
        return employer

    if hostname == "jobs.ashbyhq.com":
        segments = [unquote(segment).strip() for segment in parsed.path.split("/") if segment]
        if len(segments) >= 2 and segments[0]:
            board = segments[0]
            try:
                from rolesail.discovery.ats import load_ashby_companies

                configured = next(
                    (
                        company
                        for company in load_ashby_companies().values()
                        if str(company.get("board") or "").casefold() == board.casefold()
                    ),
                    None,
                )
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                configured = None
            return {
                "name": configured.get("name", board) if configured else board,
                "ashby_board": board,
                "provisional": True,
            }
    return None


def _backfill_external_employer_metadata(conn: sqlite3.Connection, url: str) -> None:
    """Normalize a direct upload and recover metadata from its backing ATS."""
    employer = _external_employer(url)
    if not employer:
        return

    name = employer["name"]
    if employer.get("provisional"):
        conn.execute(
            "UPDATE jobs SET company = COALESCE(company, ?), "
            "site = COALESCE(company, ?) WHERE url = ?",
            (name, name, url),
        )
        conn.commit()
        return

    conn.execute(
        "UPDATE jobs SET company = ?, site = ? WHERE url = ?",
        (name, name, url),
    )
    board = employer.get("greenhouse_board")
    job_id_match = re.search(r"_(\d+)(?:[/?#]|$)", url)
    if not board or not job_id_match:
        conn.commit()
        return

    try:
        from rolesail.discovery.greenhouse import fetch_company_jobs

        job = next(
            (
                candidate
                for candidate in fetch_company_jobs(board)
                if str(candidate.get("id")) == job_id_match.group(1)
            ),
            None,
        )
    except Exception:  # The page scrape is still usable if the ATS lookup fails.
        log.exception("Could not recover %s metadata from Greenhouse", name)
        conn.commit()
        return

    if job:
        location = job.get("location") or {}
        location_name = location.get("name") if isinstance(location, dict) else location
        conn.execute(
            "UPDATE jobs SET title = COALESCE(?, title), "
            "location = COALESCE(?, location) WHERE url = ?",
            (job.get("title"), location_name, url),
        )
    conn.commit()


def normalize_job_url(raw_url: str) -> str:
    """Validate and normalize an externally supplied job URL."""
    if not isinstance(raw_url, str):
        raise ValueError("URL must be a string")
    raw_url = raw_url.strip()
    if not raw_url:
        raise ValueError("Enter a job URL")
    if len(raw_url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("URL must include a valid hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def _amazon_job_id(url: str) -> str | None:
    """Return the numeric job ID for an Amazon Jobs detail URL."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname != "amazon.jobs" and not hostname.endswith(".amazon.jobs"):
        return None
    match = re.search(r"/jobs/(\d+)(?:/|$)", parsed.path, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _external_workday_job_id(url: str) -> tuple[str, str] | None:
    """Return the Workday employer key and requisition ID for a vanity URL."""
    employer = _external_employer(url)
    employer_key = employer.get("workday_employer") if employer else None
    if not employer_key:
        return None
    match = re.search(r"/jobs/(JR\d+)(?:/|$)", urlparse(url).path, re.IGNORECASE)
    if not match:
        return None
    return employer_key, match.group(1).upper()


def load_dashboard_company_logo(
    raw_url: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[bytes, str] | None:
    """Return a job's cached company logo, downloading it on first use."""
    url = normalize_job_url(raw_url)
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT company, company_logo FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row or not row["company"]:
        return None
    from rolesail.company_logos import company_logo_candidates, load_company_logo

    for source_url in company_logo_candidates(row["company"], row["company_logo"]):
        logo = load_company_logo(row["company"], source_url)
        if logo:
            if source_url != row["company_logo"]:
                conn.execute(
                    "UPDATE jobs SET company_logo = ? WHERE company = ? "
                    "AND (company_logo IS NULL OR company_logo = ?)",
                    (source_url, row["company"], row["company_logo"]),
                )
                conn.commit()
            return logo
    return None


def import_external_job(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Insert an external job URL and return its import state."""
    url = normalize_job_url(raw_url)
    conn = conn or get_connection()
    existing = conn.execute(
        "SELECT url, title, strategy, full_description, detail_scraped_at, "
        "detail_error, fit_score "
        "FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if existing:
        display_title = existing["title"]
        employer = _external_employer(url)
        if employer and employer.get("provisional"):
            company = employer["name"]
            placeholder = "Imported job from jobs.ashbyhq.com"
            if display_title == placeholder:
                display_title = f"Imported job from {company}"
            conn.execute(
                "UPDATE jobs SET company = CASE "
                "WHEN company IS NULL OR company = 'jobs.ashbyhq.com' THEN ? "
                "ELSE company END, site = CASE WHEN site = 'jobs.ashbyhq.com' "
                "THEN ? ELSE site END, title = CASE "
                "WHEN title = 'Imported job from jobs.ashbyhq.com' THEN ? "
                "ELSE title END WHERE url = ?",
                (company, company, display_title, url),
            )
            conn.commit()
        should_enrich = not bool(existing["full_description"])
        should_retry_score = (
            existing["strategy"] == "external_upload"
            and bool(existing["full_description"])
            and existing["fit_score"] == 0
        )
        if should_enrich or should_retry_score:
            conn.execute(
                "UPDATE jobs SET detail_scraped_at = CASE WHEN ? THEN NULL ELSE detail_scraped_at END, "
                "detail_error = CASE WHEN ? THEN NULL ELSE detail_error END, fit_score = NULL, "
                "score_reasoning = NULL, scored_at = NULL, discovery_status = 'accepted', "
                "discovery_rejection_reason = NULL "
                "WHERE url = ?",
                (should_enrich, should_enrich, url),
            )
            conn.commit()
        return {
            "created": False,
            "url": url,
            "title": display_title,
            "status": (
                "pending"
                if should_enrich
                else "scoring"
                if should_retry_score
                else job_import_status(url, conn)["status"]
            ),
            "enrichment_pending": should_enrich or should_retry_score,
        }

    hostname = urlparse(url).hostname or "external"
    employer = _external_employer(url)
    company = employer["name"] if employer else None
    site = company or hostname
    title = f"Imported job from {company or hostname}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, strategy, discovered_at, application_url) "
        "VALUES (?, ?, ?, ?, 'external_upload', ?, ?)",
        (url, title, company, site, now, url),
    )
    conn.commit()
    return {
        "created": True,
        "url": url,
        "title": title,
        "status": "pending",
        "enrichment_pending": True,
    }


def job_import_status(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Return the current enrichment status for an imported URL."""
    url = normalize_job_url(raw_url)
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT url, title, site, strategy, full_description, detail_scraped_at, "
        "detail_error, fit_score "
        "FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"url": url, "status": "missing"}
    if row["detail_error"]:
        status = "error"
    elif row["detail_scraped_at"] and row["full_description"]:
        from rolesail.config import get_tier

        needs_score = row["strategy"] == "external_upload" and get_tier() >= 2
        status = "scoring" if needs_score and row["fit_score"] is None else "complete"
    elif row["detail_scraped_at"]:
        status = "partial"
    else:
        status = "pending"
    return {
        "url": url,
        "status": status,
        "title": row["title"],
        "site": row["site"],
        "score": row["fit_score"],
        "error": row["detail_error"],
    }


def mark_job_applied(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Mark an existing dashboard job as manually applied."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT title, applied_at FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"updated": False, "url": url, "status": "missing"}

    applied_at = row["applied_at"] or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET applied_at = ?, "
        "apply_status = COALESCE(apply_status, 'manually_applied') WHERE url = ?",
        (applied_at, url),
    )
    conn.commit()
    from rolesail.outreach.service import enqueue_for_job
    outreach = enqueue_for_job(url, conn, reapplied=not bool(row["applied_at"]))
    has_email_draft = conn.execute(
        "SELECT 1 FROM outreach_recipients r "
        "JOIN outreach_batches b ON b.id = r.batch_id "
        "WHERE b.job_url = ? AND "
        "(r.gmail_draft_id IS NOT NULL OR r.apollo_message_id IS NOT NULL) LIMIT 1",
        (url,),
    ).fetchone() is not None
    return {
        "updated": True,
        "url": url,
        "title": row["title"],
        "status": "applied",
        "applied_at": applied_at,
        "applied_tab": applied_view(applied_at, has_email_draft),
        "outreach": outreach,
    }


def unmark_job_applied(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Return an applied dashboard job to the active queue."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT title FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"updated": False, "url": url, "status": "missing"}

    conn.execute(
        "UPDATE jobs SET applied_at = NULL, apply_status = NULL WHERE url = ?",
        (url,),
    )
    conn.commit()
    from rolesail.outreach.service import cancel_for_job
    cancel_for_job(url, conn)
    return {
        "updated": True,
        "url": url,
        "title": row["title"],
        "status": "active",
        "applied_at": None,
    }


def load_tailored_artifact(
    raw_url: str,
    kind: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[Path, bytes, str]:
    """Load a generated artifact for a job without accepting filesystem paths."""
    if kind not in {"tex", "pdf", "report"}:
        raise ValueError("Artifact kind must be tex, pdf, or report")
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT tailored_resume_path FROM jobs WHERE url = ?",
        (raw_url,),
    ).fetchone()
    if not row or not row["tailored_resume_path"]:
        raise FileNotFoundError("Tailored resume not found")
    tex_path = Path(row["tailored_resume_path"]).resolve()
    tailored_root = config.TAILORED_DIR.resolve()
    if tex_path.parent != tailored_root:
        raise PermissionError("Stored artifact path is outside the tailored resume directory")
    paths = {
        "tex": tex_path,
        "pdf": tex_path.with_suffix(".pdf"),
        "report": tex_path.with_name(f"{tex_path.stem}_REPORT.json"),
    }
    path = paths[kind]
    if not path.is_file():
        raise FileNotFoundError(f"Tailored {kind} artifact not found")
    content_types = {
        "tex": "application/x-tex; charset=utf-8",
        "pdf": "application/pdf",
        "report": "application/json; charset=utf-8",
    }
    return path, path.read_bytes(), content_types[kind]


def clear_tailored_resume(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Remove tailored-resume artifacts for a job and clear its tailored DB fields."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT tailored_resume_path FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"cleared": False, "url": url, "status": "missing"}
    stored = (row["tailored_resume_path"] or "").strip()
    if not stored:
        return {"cleared": False, "url": url, "status": "not_tailored"}

    stored_path = Path(stored).resolve()
    tailored_root = config.TAILORED_DIR.resolve()
    if stored_path.parent != tailored_root:
        raise PermissionError("Stored artifact path is outside the tailored resume directory")

    siblings = (
        stored_path.with_suffix(".tex"),
        stored_path.with_suffix(".pdf"),
        stored_path.with_suffix(".txt"),
        stored_path.with_name(f"{stored_path.stem}_REPORT.json"),
        stored_path.with_name(f"{stored_path.stem}_JOB.txt"),
    )
    deleted_files: list[str] = []
    for path in siblings:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted_files.append(str(path))

    conn.execute(
        "UPDATE jobs SET tailored_resume_path = NULL, tailored_at = NULL WHERE url = ?",
        (url,),
    )
    conn.commit()
    return {
        "cleared": True,
        "url": url,
        "status": "cleared",
        "deleted_files": deleted_files,
    }


def delete_job(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Delete a job posting from the dashboard database."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT title FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"deleted": False, "url": url, "status": "missing"}

    conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
    conn.commit()
    return {
        "deleted": True,
        "url": url,
        "title": row["title"],
        "status": "deleted",
    }


def _enrich_external_amazon_job(conn: sqlite3.Connection, url: str) -> bool:
    """Use Amazon's public JSON endpoint for a complete manual import."""
    job_id = _amazon_job_id(url)
    if not job_id:
        return False

    from rolesail.discovery.greenhouse import (
        _normalize_description,
        fetch_amazon_job,
    )

    job = fetch_amazon_job(job_id)
    if not job or not job.get("content_is_full"):
        return False
    full_description = _normalize_description(job.get("content"))
    if len(full_description) < 200:
        return False

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET title = COALESCE(?, title), company = 'Amazon', "
        "salary = COALESCE(?, salary), description = ?, location = COALESCE(?, location), "
        "site = 'Amazon', full_description = ?, application_url = COALESCE(?, ?), "
        "posted_at = COALESCE(?, posted_at), detail_scraped_at = ?, detail_error = NULL "
        "WHERE url = ?",
        (
            job.get("title"),
            job.get("salary"),
            full_description[:500],
            job.get("location"),
            full_description,
            job.get("application_url"),
            url,
            job.get("posted_at"),
            now,
            url,
        ),
    )
    conn.commit()
    return True


def _enrich_external_workday_job(conn: sqlite3.Connection, url: str) -> bool:
    """Resolve a recognized vanity careers URL through its Workday API."""
    identity = _external_workday_job_id(url)
    if not identity:
        return False
    employer_key, job_id = identity

    from rolesail.discovery.workday import (
        load_employers,
        strip_html,
        workday_detail,
        workday_search,
    )

    employer = load_employers().get(employer_key)
    if not employer:
        return False
    search = workday_search(employer, job_id, limit=20)
    posting = next(
        (
            candidate
            for candidate in search.get("jobPostings", [])
            if job_id in candidate.get("bulletFields", [])
            or job_id in candidate.get("externalPath", "").upper()
        ),
        None,
    )
    if not posting or not posting.get("externalPath"):
        return False

    info = workday_detail(employer, posting["externalPath"]).get(
        "jobPostingInfo", {}
    )
    if str(info.get("jobReqId", "")).upper() != job_id:
        return False
    full_description = strip_html(info.get("jobDescription", ""))
    if len(full_description) < 200:
        return False

    locations = [info.get("location"), *(info.get("additionalLocations") or [])]
    location = "; ".join(
        str(item).strip() for item in locations if str(item or "").strip()
    ) or None
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET title = COALESCE(?, title), company = ?, site = ?, "
        "description = ?, location = COALESCE(?, location), full_description = ?, "
        "application_url = COALESCE(?, ?), posted_at = COALESCE(?, posted_at), "
        "detail_scraped_at = ?, detail_error = NULL WHERE url = ?",
        (
            info.get("title") or posting.get("title"),
            employer["name"],
            employer["name"],
            full_description[:500],
            location,
            full_description,
            info.get("externalUrl"),
            url,
            info.get("startDate"),
            now,
            url,
        ),
    )
    conn.commit()
    return True


def enrich_external_job(url: str) -> None:
    """Enrich and, when configured, score one imported URL."""
    from rolesail.enrichment.detail import scrape_site_batch

    conn = get_connection()
    row = conn.execute(
        "SELECT title, site, full_description, detail_error FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return

    from rolesail.usage import usage_context
    try:
        if not row["full_description"] or row["detail_error"]:
            with usage_context(stage="enrich"):
                enriched = _enrich_external_workday_job(conn, url)
                if not enriched:
                    enriched = _enrich_external_amazon_job(conn, url)
                if not enriched:
                    scrape_site_batch(
                        conn,
                        row["site"] or "external",
                        [(url, row["title"])],
                        delay=0,
                    )
    except Exception as exc:
        log.exception("External job enrichment failed for %s", url)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? WHERE url = ?",
            (str(exc)[:500], now, url),
        )
        conn.commit()
        return

    _backfill_external_employer_metadata(conn, url)

    location_row = conn.execute(
        "SELECT location FROM jobs WHERE url = ? AND strategy = 'external_upload'",
        (url,),
    ).fetchone()
    if location_row:
        location = location_row["location"]
        if location and not config.location_is_allowed(location):
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE jobs SET discovery_status = 'rejected', "
                "discovery_rejection_reason = 'outside_allowed_countries', "
                "discovery_checked_at = ? WHERE url = ?",
                (now, url),
            )
            conn.commit()
            return
        conn.execute(
            "UPDATE jobs SET discovery_status = 'accepted', "
            "discovery_rejection_reason = NULL WHERE url = ? "
            "AND (discovery_rejection_reason IS NULL "
            "OR discovery_rejection_reason = 'outside_allowed_countries')",
            (url,),
        )
        conn.commit()

    from rolesail.config import get_tier

    if get_tier() < 2:
        return

    try:
        from rolesail.scoring.scorer import run_scoring

        with usage_context(stage="score"):
            run_scoring(target_url=url, workers=1)
    except Exception as exc:
        # Scoring configuration/runtime failures should not make a successful
        # detail scrape look like an enrichment failure.
        log.exception("External job scoring failed for %s", url)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET fit_score = 0, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (f"Scoring error: {str(exc)[:500]}", now, url),
        )
        conn.commit()
