"""Persistence helpers for outreach batches and recipients."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from rolesail.database import get_connection
from rolesail.outreach.composition import _flowing_email_body

SAME_COMPANY_COOLDOWN_DAYS = 30

def _batch_row(identifier: str, conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM outreach_batches WHERE id = ? OR job_url = ? LIMIT 1",
        (identifier, identifier),
    ).fetchone()


def get_batch(identifier: str, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn or get_connection()
    row = _batch_row(identifier, conn)
    if not row:
        return None
    batch = dict(row)
    batch["company_research"] = _loads(batch.pop("company_research_json", None), {})
    recipients = conn.execute(
        "SELECT * FROM outreach_recipients WHERE batch_id = ? "
        "ORDER BY relevance_score DESC, created_at",
        (batch["id"],),
    ).fetchall()
    batch["recipients"] = []
    for recipient in recipients:
        item = dict(recipient)
        item["source_facts"] = _loads(item.pop("source_facts_json", None), [])
        if item.get("body_text"):
            item["body_text"] = _flowing_email_body(item["body_text"])
        batch["recipients"].append(item)
    return batch


def _loads(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def already_contacted(email: str, domain: str, conn: sqlite3.Connection) -> bool:
    cutoff = (
        datetime.now(UTC) - timedelta(days=SAME_COMPANY_COOLDOWN_DAYS)
    ).isoformat()
    return bool(
        conn.execute(
            "SELECT 1 FROM outreach_recipients r "
            "JOIN outreach_batches b ON b.id = r.batch_id "
            "WHERE lower(r.email) = lower(?) AND b.company_domain = ? "
            "AND r.status = 'sent' AND r.sent_at >= ? LIMIT 1",
            (email, domain, cutoff),
        ).fetchone()
    )


def is_suppressed(person_id: str, email: str, conn: sqlite3.Connection) -> bool:
    keys = [f"person:{person_id}", f"email:{email.lower()}"]
    return bool(
        conn.execute(
            "SELECT 1 FROM outreach_suppressions WHERE key IN (?, ?) LIMIT 1",
            keys,
        ).fetchone()
    )
