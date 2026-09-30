"""Read models used by the browser dashboard.

The dashboard is a client-rendered application.  This module is deliberately
free of HTTP and markup concerns so the same serialization rules can be
tested directly and consumed by any future UI.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rolesail.config import load_search_config
from rolesail.database import get_connection, normalize_posted_at

# Applications created before outreach shipped do not have a draft workflow
# to finish, so they remain in the completed/legacy bucket.
OUTREACH_INTRODUCED_AT = datetime.fromisoformat("2026-09-10T23:26:04-04:00")


def applied_view(applied_at: str | None, has_email_draft: bool = False) -> str:
    """Classify an application for the dashboard's applied buckets."""
    if not applied_at:
        return "active"
    try:
        timestamp = datetime.fromisoformat(applied_at)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
    except ValueError:
        return "drafts_done" if has_email_draft else "needs_drafts"
    return (
        "drafts_done"
        if has_email_draft or timestamp < OUTREACH_INTRODUCED_AT
        else "needs_drafts"
    )


def format_posted_at(value: str | None, reference_at: str | None = None) -> str:
    """Format a source posting date for display."""
    value = normalize_posted_at(value, reference_at)
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    parsed = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, pattern).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
    if parsed:
        label = parsed.strftime("%b %d, %Y").replace(" 0", " ")
        return f"Posted {label}"
    return f"Posted {text}"


def format_applied_at(value: str | None) -> str:
    """Format an application timestamp for display."""
    posted_label = format_posted_at(value)
    return posted_label.replace("Posted", "Applied", 1) if posted_label else ""


def posted_at_sort_key(value: str | None) -> float:
    """Return a sortable timestamp for absolute and relative source dates."""
    if not value:
        return float("-inf")
    text = str(value).strip()
    if not text:
        return float("-inf")

    normalized = re.sub(r"^posted\s+", "", text, flags=re.IGNORECASE).strip()
    now = datetime.now(UTC)
    relative = re.fullmatch(
        r"(?:(\d+)\+?\s+)?(hour|day|week|month)s?\s+ago",
        normalized,
        flags=re.IGNORECASE,
    )
    if relative:
        amount = int(relative.group(1) or 1)
        unit_days = {"hour": 1 / 24, "day": 1, "week": 7, "month": 30}
        return (
            now - timedelta(days=amount * unit_days[relative.group(2).lower()])
        ).timestamp()
    if normalized.lower() == "today":
        return now.timestamp()
    if normalized.lower() == "yesterday":
        return (now - timedelta(days=1)).timestamp()

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(normalized, pattern).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
    if parsed is None:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _priority_terms() -> list[str]:
    return [
        str(term).lower().strip()
        for term in load_search_config().get("priority_titles", [])
        if term
    ]


def load_dashboard_jobs(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Return the complete, sorted job read model consumed by the SPA."""
    connection = conn or get_connection()
    jobs = connection.execute(
        """
        SELECT url, title, company, company_logo, salary, description, location,
               site, strategy, full_description, application_url, detail_error,
               posted_at, discovered_at, fit_score, score_reasoning, applied_at,
               tailored_resume_path,
               COALESCE(tailor_attempts, 0) AS tailor_attempts,
               (SELECT id FROM outreach_batches WHERE job_url = jobs.url)
                   AS outreach_batch_id,
               (SELECT status FROM outreach_batches WHERE job_url = jobs.url)
                   AS outreach_status,
               (SELECT COUNT(*) FROM outreach_recipients r
                  JOIN outreach_batches b ON b.id = r.batch_id
                 WHERE b.job_url = jobs.url AND r.status = 'ready')
                   AS outreach_ready,
               (SELECT COUNT(*) FROM outreach_recipients r
                  JOIN outreach_batches b ON b.id = r.batch_id
                 WHERE b.job_url = jobs.url AND r.status = 'sent')
                   AS outreach_sent,
               (SELECT COUNT(*) FROM outreach_recipients r
                  JOIN outreach_batches b ON b.id = r.batch_id
                 WHERE b.job_url = jobs.url AND r.status = 'failed')
                   AS outreach_failed,
               (SELECT COUNT(*) FROM outreach_recipients r
                  JOIN outreach_batches b ON b.id = r.batch_id
                 WHERE b.job_url = jobs.url AND
                       (r.gmail_draft_id IS NOT NULL OR
                        r.apollo_message_id IS NOT NULL))
                   AS email_draft_count
          FROM jobs
         WHERE COALESCE(discovery_status, 'accepted') = 'accepted'
        """
    ).fetchall()

    priority_terms = _priority_terms()

    def is_priority_job(job: sqlite3.Row) -> bool:
        title = (job["title"] or "").lower()
        return any(
            re.search(rf"\b{re.escape(term)}\b", title) for term in priority_terms
        )

    jobs = sorted(
        jobs,
        key=lambda job: (
            job["fit_score"] is None,
            -(job["fit_score"] or 0),
            -posted_at_sort_key(job["posted_at"] or job["discovered_at"]),
            not is_priority_job(job),
            job["site"] or "",
            job["title"] or "",
        ),
    )

    result: list[dict] = []
    for job in jobs:
        stored = Path(job["tailored_resume_path"]) if job["tailored_resume_path"] else None
        has_pdf = bool(stored and stored.with_suffix(".pdf").is_file())
        has_tex = bool(stored and stored.suffix.lower() == ".tex" and stored.is_file())
        has_report = bool(
            stored and stored.with_name(f"{stored.stem}_REPORT.json").is_file()
        )
        email_draft_count = int(job["email_draft_count"] or 0)
        bucket = applied_view(job["applied_at"], bool(email_draft_count))
        if bucket == "drafts_done":
            if job["outreach_status"] == "completed":
                status = "Outreach sent"
            else:
                status = "Drafts created" if email_draft_count else "Applied (legacy)"
        elif bucket == "needs_drafts":
            status = "Needs drafts"
        elif stored:
            status = "Tailored"
        elif job["fit_score"] is not None:
            status = "Scored"
        else:
            status = "Discovered"

        result.append(
            {
                "url": job["url"] or "",
                "title": job["title"] or "Untitled role",
                "company": job["company"] or job["site"] or "Unknown company",
                "company_logo": job["company_logo"] or "",
                "location": job["location"] or "Location unavailable",
                "site": job["site"] or "Unknown",
                "strategy": job["strategy"] or "",
                "salary": job["salary"] or "",
                "posted_at": job["posted_at"] or job["discovered_at"] or "",
                "posted_label": format_posted_at(
                    job["posted_at"] or job["discovered_at"], job["discovered_at"]
                ),
                "score": job["fit_score"],
                "reasoning": job["score_reasoning"] or "",
                "description": job["full_description"] or job["description"] or "",
                "detail_error": job["detail_error"] or "",
                "application_url": job["application_url"] or job["url"] or "",
                "applied": bool(job["applied_at"]),
                "applied_at": job["applied_at"] or "",
                "applied_label": format_applied_at(job["applied_at"]),
                "applied_tab": bucket,
                "status": status,
                "priority": is_priority_job(job),
                "has_tailored": bool(stored),
                "has_pdf": has_pdf,
                "has_tex": has_tex,
                "has_report": has_report,
                "can_tailor": bool(
                    not job["applied_at"]
                    and job["full_description"]
                    and not stored
                    and job["tailor_attempts"] < 5
                ),
                "can_retailor": bool(
                    not job["applied_at"]
                    and job["full_description"]
                    and stored
                    and job["tailor_attempts"] < 5
                ),
                "tailor_attempts": int(job["tailor_attempts"] or 0),
                "outreach_summary": (
                    {
                        "batch_id": job["outreach_batch_id"],
                        "status": job["outreach_status"],
                        "ready": int(job["outreach_ready"] or 0),
                        "sent": int(job["outreach_sent"] or 0),
                        "failed": int(job["outreach_failed"] or 0),
                        "review_required": job["outreach_status"]
                        == "ready_for_review",
                    }
                    if job["outreach_batch_id"]
                    else None
                ),
            }
        )
    return result
