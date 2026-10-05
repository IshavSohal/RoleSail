"""Durable preparation, review, and sending for post-application outreach."""

# Compatibility facade: moved policy and delivery names remain importable here.
# ruff: noqa: F401

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from rolesail import config
from rolesail.database import get_connection
from rolesail.outreach.apollo import ApolloClient, ApolloError
from rolesail.outreach.research import fetch_official_pages

log = logging.getLogger(__name__)

DESIRED_RECIPIENTS = 5
MAX_ENRICHMENTS = 10
SAME_COMPANY_COOLDOWN_DAYS = 30
TERMINAL_BATCH_STATES = {"completed", "cancelled", "stopped", "drafted"}
RECRUITER_WORDS = ("recruit", "talent", "people partner", "sourcer")
LEADER_WORDS = ("chief", "vice president", "vp ", "head of", "director")
MANAGER_WORDS = ("manager", "lead")
REMOTE_ONLY_WORDS = ("remote", "anywhere", "work from home", "wfh", "distributed")
RETRY_DELAYS_MINUTES = (5, 30, 120)
PROHIBITED_PHRASES = (
    "i hope this email finds you well",
    "i came across your profile",
    "i wanted to reach out",
    "i'm reaching out",
    "i am reaching out",
    "pick your brain",
    "perfect fit",
    "aligns perfectly",
    "deeply impressed",
    "resonates with me",
    "unique opportunity",
    "leverage",
    "synergy",
    "act now",
    "limited time",
    "guaranteed",
    "buy now",
    "make money",
)
ISHAV_INTRO_PREFIX = (
    "I'm Ishav, a recent Computer Science graduate from the University of Toronto with experience in "
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def enabled() -> bool:
    return os.environ.get("OUTREACH_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def enqueue_for_job(
    job_url: str,
    conn: sqlite3.Connection | None = None,
    *,
    reapplied: bool = False,
) -> dict | None:
    """Create a batch, or restart unsent cancelled outreach after re-application."""
    if not enabled():
        return None
    conn = conn or get_connection()
    row = conn.execute("SELECT applied_at FROM jobs WHERE url = ?", (job_url,)).fetchone()
    if not row or not row["applied_at"]:
        return None
    now = _now()
    batch_id = str(uuid.uuid4())
    conn.execute(
        "INSERT OR IGNORE INTO outreach_batches "
        "(id, job_url, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?)",
        (batch_id, job_url, now, now),
    )
    existing = conn.execute(
        "SELECT id, status FROM outreach_batches WHERE job_url = ?", (job_url,)
    ).fetchone()
    if reapplied and existing and existing["status"] == "cancelled":
        started = conn.execute(
            "SELECT 1 FROM outreach_recipients WHERE batch_id = ? AND "
            "(status IN ('sending', 'sent', 'drafting', 'drafted') "
            "OR apollo_contact_id IS NOT NULL OR apollo_message_id IS NOT NULL "
            "OR gmail_account_email IS NOT NULL OR gmail_draft_id IS NOT NULL "
            "OR sent_at IS NOT NULL) LIMIT 1",
            (existing["id"],),
        ).fetchone()
        if not started:
            conn.execute(
                "DELETE FROM outreach_recipients WHERE batch_id = ?", (existing["id"],)
            )
            conn.execute(
                "UPDATE outreach_batches SET status = 'queued', company_domain = NULL, "
                "company_research_json = NULL, error = NULL, approved_at = NULL, "
                "completed_at = NULL, updated_at = ? WHERE id = ?",
                (now, existing["id"]),
            )
    conn.commit()
    row = conn.execute("SELECT * FROM outreach_batches WHERE job_url = ?", (job_url,)).fetchone()
    return dict(row) if row else None


def cancel_for_job(job_url: str, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_connection()
    now = _now()
    batch_ids = [row[0] for row in conn.execute(
        "SELECT id FROM outreach_batches WHERE job_url = ?", (job_url,)
    ).fetchall()]
    conn.execute(
        "UPDATE outreach_recipients SET status = 'cancelled', error = ?, updated_at = ? "
        "WHERE batch_id IN (SELECT id FROM outreach_batches WHERE job_url = ?) "
        "AND status IN ('ready', 'needs_edit', 'scheduled', 'failed')",
        ("The job is no longer marked as applied", now, job_url),
    )
    for batch_id in batch_ids:
        _update_batch_after_send(batch_id, conn)
    conn.commit()


def recover_reapplied_batches(conn: sqlite3.Connection | None = None) -> None:
    """Queue unsent legacy batches cancelled before their latest applied date."""
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT b.job_url, b.updated_at, j.applied_at "
        "FROM outreach_batches b JOIN jobs j ON j.url = b.job_url "
        "WHERE b.status = 'cancelled' AND j.applied_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        cancelled_at = _parse_timestamp(row["updated_at"])
        applied_at = _parse_timestamp(row["applied_at"])
        if cancelled_at and applied_at and applied_at > cancelled_at:
            enqueue_for_job(row["job_url"], conn, reapplied=True)


from rolesail.outreach.composition import (
    _candidate_kind,
    _choose_organization,
    _domain_from_organization,
    _extract_json,
    _flowing_email_body,
    _generate_messages,
    _introduction_employer,
    _introduction_errors,
    _introduction_sentence,
    _location_contains,
    _location_preference,
    _location_queries,
    _message_errors,
    _message_words,
    _normalized_location,
    _normalized_message,
    _outreach_job_link,
    _parse_message_output,
    _require_valid_reviewed_introductions,
    _resolve_organization,
    _role_terms,
    _workday_tenant_alias,
    rank_people,
)
from rolesail.outreach.repository import (
    _batch_row,
    _loads,
    get_batch,
)
from rolesail.outreach.repository import (
    already_contacted as _already_contacted,
)
from rolesail.outreach.repository import (
    is_suppressed as _is_suppressed,
)


def prepare_batch(
    identifier: str,
    *,
    conn: sqlite3.Connection | None = None,
    apollo: ApolloClient | None = None,
) -> dict:
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    if batch["status"] in TERMINAL_BATCH_STATES:
        return get_batch(batch["id"], conn) or {}
    now = _now()
    claimed = conn.execute(
        "UPDATE outreach_batches SET status = 'preparing', error = NULL, updated_at = ? "
        "WHERE id = ? AND status IN ('queued', 'failed')",
        (now, batch["id"]),
    ).rowcount
    conn.commit()
    if not claimed:
        return get_batch(batch["id"], conn) or {}

    try:
        job_row = conn.execute("SELECT * FROM jobs WHERE url = ?", (batch["job_url"],)).fetchone()
        if not job_row or not job_row["applied_at"]:
            raise ValueError("The job is not marked as applied")
        job = dict(job_row)
        profile = config.load_profile()
        apollo = apollo or ApolloClient()
        organization, domain = _resolve_organization(job, apollo)
        if not organization:
            raise ValueError("Apollo could not resolve the employer")
        if not domain:
            raise ValueError("Apollo did not provide the employer's official domain")

        cached = conn.execute(
            "SELECT facts_json, sources_json, researched_at FROM company_research WHERE domain = ?",
            (domain,),
        ).fetchone()
        fresh_after = datetime.now(UTC) - timedelta(days=7)
        use_cache = False
        if cached:
            try:
                use_cache = datetime.fromisoformat(cached["researched_at"]) >= fresh_after
            except (TypeError, ValueError):
                use_cache = False
        if use_cache:
            pages = _loads(cached["sources_json"], [])
        else:
            pages = fetch_official_pages(domain)
            conn.execute(
                "INSERT OR REPLACE INTO company_research (domain, facts_json, sources_json, researched_at) "
                "VALUES (?, ?, ?, ?)",
                (domain, json.dumps(organization), json.dumps(pages), _now()),
            )
        research = {"apollo": organization, "official_pages": pages}
        people_by_id: dict[str, dict] = {}
        location_queries = _location_queries(job.get("location"))
        if location_queries:
            try:
                local_people = apollo.search_people(
                    organization_id=str(organization.get("id") or organization.get("organization_id") or "") or None,
                    domain=domain,
                    locations=location_queries,
                    per_page=100,
                )
                for person in local_people:
                    person_id = str(person.get("id") or person.get("person_id") or "")
                    if person_id:
                        people_by_id[person_id] = {**person, "_location_search_match": True}
            except ApolloError as exc:
                log.warning("Apollo location-filtered people search failed; using company-wide results: %s", exc)

        company_people = apollo.search_people(
            organization_id=str(organization.get("id") or organization.get("organization_id") or "") or None,
            domain=domain,
            per_page=100,
        )
        for person in company_people:
            person_id = str(person.get("id") or person.get("person_id") or "")
            if person_id and person_id not in people_by_id:
                people_by_id[person_id] = person
        ranked = rank_people(
            list(people_by_id.values()),
            job.get("title") or "",
            job.get("full_description") or "",
            job.get("location"),
        )
        eligible: list[dict] = []
        attempted = 0
        for candidate in ranked:
            if attempted >= MAX_ENRICHMENTS or len(eligible) >= DESIRED_RECIPIENTS:
                break
            attempted += 1
            person = apollo.enrich_person(candidate["person_id"])
            if not person:
                continue
            email = str(person.get("email") or "").strip()
            email_status = str(person.get("email_status") or "").lower()
            if not email or email_status != "verified":
                continue
            if _is_suppressed(candidate["person_id"], email, conn) or _already_contacted(email, domain, conn):
                continue
            eligible.append({
                **candidate,
                "first_name": person.get("first_name") or candidate.get("first_name"),
                "last_name": person.get("last_name") or candidate.get("last_name"),
                "name": person.get("name") or candidate.get("name"),
                "title": person.get("title") or candidate.get("title"),
                "linkedin_url": person.get("linkedin_url") or candidate.get("linkedin_url"),
                "email": email,
                "email_status": email_status,
            })
        messages = _generate_messages(job, eligible, research, profile) if eligible else []
        if not messages:
            raise ValueError("No relevant employees with verified work emails were available")

        current = conn.execute(
            "SELECT status, updated_at FROM outreach_batches WHERE id = ?", (batch["id"],)
        ).fetchone()
        if not current or current["status"] != "preparing" or current["updated_at"] != now:
            conn.commit()
            return get_batch(batch["id"], conn) or {}
        still_applied = conn.execute(
            "SELECT applied_at FROM jobs WHERE url = ?", (batch["job_url"],)
        ).fetchone()
        if not still_applied or not still_applied["applied_at"]:
            conn.commit()
            return get_batch(batch["id"], conn) or {}

        conn.execute("DELETE FROM outreach_recipients WHERE batch_id = ? AND status != 'sent'", (batch["id"],))
        created = _now()
        for item in messages:
            conn.execute(
                "INSERT INTO outreach_recipients "
                "(id, batch_id, apollo_person_id, first_name, last_name, title, linkedin_url, "
                "email, email_status, relevance_score, relevance_reason, subject, body_text, "
                "source_facts_json, status, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()), batch["id"], item["person_id"], item.get("first_name"),
                    item.get("last_name"), item.get("title"), item.get("linkedin_url"),
                    item.get("email"), item.get("email_status"), item.get("relevance_score"),
                    item.get("relevance_reason"), item.get("subject"), item.get("body_text"),
                    json.dumps(item.get("used_facts") or []),
                    "needs_edit" if item.get("validation_errors") else "ready",
                    "; ".join(item.get("validation_errors") or [])[:1000] or None,
                    created,
                    created,
                ),
            )
        conn.execute(
            "UPDATE outreach_batches SET status = 'ready_for_review', company_domain = ?, "
            "company_research_json = ?, error = NULL, updated_at = ? WHERE id = ?",
            (domain, json.dumps(research), _now(), batch["id"]),
        )
        conn.commit()
    except Exception as exc:
        changed = conn.execute(
            "UPDATE outreach_batches SET status = 'failed', error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'preparing' AND updated_at = ?",
            (str(exc)[:1000], _now(), batch["id"], now),
        ).rowcount
        conn.commit()
        if not changed:
            return get_batch(batch["id"], conn) or {}
        raise
    return get_batch(batch["id"], conn) or {}


from rolesail.outreach.delivery import (
    DEFAULT_SCHEDULE,
    _add_business_days,
    _body_html,
    _is_transient_send_error,
    _next_eligible_time,
    _parse_timestamp,
    _reschedule_after_failure,
    _reserve_send_time,
    _reserved_send_times,
    _schedule_selected,
    _update_batch_after_send,
    approve_batch,
    create_gmail_drafts,
    dispatch_due_outreach,
    preview_batch_schedule,
    recover_outreach_dispatcher,
    refresh_delivery_statuses,
    refresh_inflight_outreach,
    reset_uncertain_gmail_draft,
    schedule_settings,
)


def retry_batch(identifier: str, *, conn: sqlite3.Connection | None = None, apollo: ApolloClient | None = None) -> dict:
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    if conn.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ? "
        "AND (gmail_account_email IS NOT NULL OR gmail_draft_id IS NOT NULL) LIMIT 1",
        (batch["id"],),
    ).fetchone():
        raise ValueError("Retry Gmail draft creation from the dashboard; Apollo sending is disabled for this batch")
    if batch["status"] == "failed" and not conn.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ?", (batch["id"],)
    ).fetchone():
        conn.execute("UPDATE outreach_batches SET status = 'queued', updated_at = ? WHERE id = ?", (_now(), batch["id"]))
        conn.commit()
        return prepare_batch(batch["id"], conn=conn, apollo=apollo)
    failed = [dict(row) for row in conn.execute(
        "SELECT id FROM outreach_recipients WHERE batch_id = ? AND status = 'failed'",
        (batch["id"],),
    ).fetchall()]
    if not failed:
        return refresh_delivery_statuses(batch["id"], conn=conn, apollo=apollo)
    settings = schedule_settings()
    for item in failed:
        slot = _reserve_send_time(conn, _utc_now(), settings)
        conn.execute(
            "UPDATE outreach_recipients SET status = 'scheduled', scheduled_for = ?, "
            "attempt_count = 0, error = NULL, updated_at = ? WHERE id = ?",
            (slot.isoformat(), _now(), item["id"]),
        )
    _update_batch_after_send(batch["id"], conn)
    conn.commit()
    return get_batch(batch["id"], conn) or {}


def redraft_batch(
    identifier: str,
    *,
    feedback: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Regenerate every editable message without searching or enriching people again."""
    if not isinstance(feedback, str):
        # This is request validation, so keep it a ValueError for the dashboard's 400 path.
        raise ValueError("Redraft feedback must be text")  # noqa: TRY004
    feedback = feedback.strip()
    if len(feedback) > 500:
        raise ValueError("Redraft feedback must be 500 characters or fewer")
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    if batch["status"] not in {"ready_for_review", "failed", "cancelled"}:
        raise ValueError("Only an unsent outreach batch can be redrafted")

    rows = conn.execute(
        "SELECT * FROM outreach_recipients WHERE batch_id = ? ORDER BY relevance_score DESC, created_at",
        (batch["id"],),
    ).fetchall()
    if any(
        row["status"] not in {"ready", "needs_edit", "failed", "cancelled", "suppressed"}
        or row["gmail_account_email"] is not None
        or row["gmail_draft_id"] is not None
        or row["apollo_contact_id"] is not None
        or row["apollo_message_id"] is not None
        or row["sent_at"] is not None
        for row in rows
    ):
        raise ValueError("This batch cannot be redrafted after drafting or sending has started")
    editable_rows = [row for row in rows if row["status"] != "suppressed"]
    if not editable_rows:
        raise ValueError("This batch has no editable outreach emails")

    job_row = conn.execute(
        "SELECT * FROM jobs WHERE url = ?", (batch["job_url"],)
    ).fetchone()
    if not job_row or not job_row["applied_at"]:
        raise ValueError("The job is no longer marked as applied")

    claim_time = _now()
    claimed = conn.execute(
        "UPDATE outreach_batches SET status = 'preparing', error = NULL, updated_at = ? "
        "WHERE id = ? AND status IN ('ready_for_review', 'failed', 'cancelled')",
        (claim_time, batch["id"]),
    ).rowcount
    conn.commit()
    if not claimed:
        raise ValueError("This outreach batch is already being updated")

    recipients = [
        {
            **dict(row),
            "person_id": str(row["apollo_person_id"]),
            "name": " ".join(
                part for part in (row["first_name"], row["last_name"]) if part
            ),
            "candidate_kind": _candidate_kind(str(row["title"] or "")),
        }
        for row in editable_rows
    ]
    try:
        messages = _generate_messages(
            dict(job_row),
            recipients,
            _loads(batch["company_research_json"], {}),
            config.load_profile(),
            feedback,
        )
        by_person_id = {
            str(item.get("person_id")): item for item in messages if isinstance(item, dict)
        }
        if set(by_person_id) != {item["person_id"] for item in recipients}:
            raise ValueError("The redraft did not return every editable recipient")

        current = conn.execute(
            "SELECT status, updated_at FROM outreach_batches WHERE id = ?", (batch["id"],)
        ).fetchone()
        if not current or current["status"] != "preparing" or current["updated_at"] != claim_time:
            raise ValueError("This outreach batch changed while it was being redrafted")
        for recipient in recipients:
            item = by_person_id[recipient["person_id"]]
            validation_errors = item.get("validation_errors") or []
            conn.execute(
                "UPDATE outreach_recipients SET subject = ?, body_text = ?, source_facts_json = ?, "
                "status = ?, error = ?, updated_at = ? WHERE id = ? AND batch_id = ?",
                (
                    str(item.get("subject") or "")[:200],
                    str(item.get("body_text") or "")[:4000],
                    json.dumps(item.get("used_facts") or []),
                    "needs_edit" if validation_errors else "ready",
                    "; ".join(validation_errors)[:1000] or None,
                    _now(),
                    recipient["id"],
                    batch["id"],
                ),
            )
        conn.execute(
            "UPDATE outreach_batches SET status = 'ready_for_review', error = NULL, "
            "approved_at = NULL, completed_at = NULL, updated_at = ? WHERE id = ?",
            (_now(), batch["id"]),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE outreach_batches SET status = ?, error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'preparing' AND updated_at = ?",
            (batch["status"], str(exc)[:1000], _now(), batch["id"], claim_time),
        )
        conn.commit()
        raise
    return get_batch(batch["id"], conn) or {}


def cancel_batch(identifier: str, conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    sent = conn.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ? "
        "AND status IN ('sending', 'sent', 'drafting', 'drafted') LIMIT 1",
        (batch["id"],),
    ).fetchone()
    if sent:
        raise ValueError("A batch cannot be cancelled after sending or Gmail draft creation has started")
    conn.execute(
        "UPDATE outreach_recipients SET status = 'cancelled', updated_at = ? "
        "WHERE batch_id = ? AND status IN ('ready', 'needs_edit', 'scheduled', 'failed')",
        (_now(), batch["id"]),
    )
    conn.execute(
        "UPDATE outreach_batches SET status = 'cancelled', updated_at = ? WHERE id = ?",
        (_now(), batch["id"]),
    )
    conn.commit()
    return get_batch(batch["id"], conn) or {}


def cancel_pending(identifier: str, conn: sqlite3.Connection | None = None) -> dict:
    """Cancel every not-yet-sending recipient while retaining delivery history."""
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    changed = conn.execute(
        "UPDATE outreach_recipients SET status = 'cancelled', updated_at = ? "
        "WHERE batch_id = ? AND status IN ('ready', 'needs_edit', 'scheduled', 'failed')",
        (_now(), batch["id"]),
    ).rowcount
    if not changed:
        raise ValueError("This batch has no pending outreach to cancel")
    _update_batch_after_send(batch["id"], conn)
    conn.commit()
    return get_batch(batch["id"], conn) or {}


def clear_cancelled_batch(identifier: str, conn: sqlite3.Connection | None = None) -> dict:
    """Permanently remove a cancelled batch and its unsent local recipients."""
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    if batch["status"] != "cancelled":
        raise ValueError("Only a cancelled outreach batch can be cleared")
    started = conn.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ? "
        "AND status IN ('sending', 'sent', 'drafting', 'drafted') LIMIT 1",
        (batch["id"],),
    ).fetchone()
    if started:
        raise ValueError("Outreach with sent emails or Gmail drafts cannot be cleared")
    result = {"id": batch["id"], "job_url": batch["job_url"], "status": "cleared"}
    conn.execute("DELETE FROM outreach_recipients WHERE batch_id = ?", (batch["id"],))
    conn.execute("DELETE FROM outreach_batches WHERE id = ?", (batch["id"],))
    conn.commit()
    return result


def suppress_recipient(recipient_id: str, reason: str = "user", conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or get_connection()
    recipient = conn.execute("SELECT * FROM outreach_recipients WHERE id = ?", (recipient_id,)).fetchone()
    if not recipient:
        raise ValueError("Outreach recipient not found")
    if recipient["status"] in {"sending", "sent", "drafting", "drafted"}:
        raise ValueError("A recipient cannot be suppressed after sending or Gmail draft creation has started")
    now = _now()
    keys = [f"person:{recipient['apollo_person_id']}"]
    if recipient["email"]:
        keys.append(f"email:{recipient['email'].lower()}")
    conn.executemany(
        "INSERT OR REPLACE INTO outreach_suppressions (key, reason, created_at) VALUES (?, ?, ?)",
        [(key, reason[:300], now) for key in keys],
    )
    conn.execute(
        "UPDATE outreach_recipients SET status_before_suppression = status, "
        "status = 'suppressed', updated_at = ? WHERE id = ?",
        (now, recipient_id),
    )
    _update_batch_after_send(recipient["batch_id"], conn)
    conn.commit()
    return get_batch(recipient["batch_id"], conn) or {}


def restore_suppressed_recipient(
    recipient_id: str, conn: sqlite3.Connection | None = None
) -> dict:
    """Remove a user suppression and return the recipient to its prior review state."""
    conn = conn or get_connection()
    recipient = conn.execute(
        "SELECT * FROM outreach_recipients WHERE id = ?", (recipient_id,)
    ).fetchone()
    if not recipient:
        raise ValueError("Outreach recipient not found")
    if recipient["status"] != "suppressed":
        raise ValueError("This recipient is not marked as never contact")

    keys = [f"person:{recipient['apollo_person_id']}"]
    if recipient["email"]:
        keys.append(f"email:{recipient['email'].lower()}")
    placeholders = ",".join("?" for _ in keys)
    conn.execute(
        f"DELETE FROM outreach_suppressions WHERE key IN ({placeholders})", keys
    )

    previous_status = recipient["status_before_suppression"]
    if previous_status not in {"ready", "needs_edit", "failed"}:
        previous_status = "ready"
    now = _now()
    conn.execute(
        "UPDATE outreach_recipients SET status = ?, status_before_suppression = NULL, "
        "updated_at = ? WHERE id = ? AND status = 'suppressed'",
        (previous_status, now, recipient_id),
    )
    _update_batch_after_send(recipient["batch_id"], conn)
    conn.execute(
        "UPDATE outreach_batches SET completed_at = NULL WHERE id = ? AND status != 'completed'",
        (recipient["batch_id"],),
    )
    conn.commit()
    return get_batch(recipient["batch_id"], conn) or {}
