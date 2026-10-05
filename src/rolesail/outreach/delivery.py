"""Outreach scheduling and external delivery workflows."""

from __future__ import annotations

import html
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from rolesail import config
from rolesail.database import get_connection
from rolesail.outreach.apollo import ApolloClient, ApolloError
from rolesail.outreach.composition import (
    _flowing_email_body,
    _require_valid_reviewed_introductions,
)
from rolesail.outreach.repository import _batch_row, get_batch, is_suppressed

log = logging.getLogger(__name__)
DEFAULT_SCHEDULE = {
    "timezone": "America/Toronto", "weekdays": [0, 1, 2, 3, 4],
    "send_window_start": "09:00", "send_window_end": "16:00",
    "first_wave_size": 2, "second_wave_delay_business_days": 2,
    "min_spacing_minutes": 10, "daily_limit": 15,
}
RETRY_DELAYS_MINUTES = (5, 30, 120)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _utc_now() -> datetime:
    return datetime.now(UTC)

def _body_html(body: str) -> str:
    return "".join(f"<p>{html.escape(paragraph).replace(chr(10), '<br>')}</p>" for paragraph in body.split("\n\n") if paragraph.strip())


def schedule_settings(profile: dict | None = None) -> dict:
    raw = (profile or config.load_profile()).get("outreach", {}).get("schedule", {})
    settings = {**DEFAULT_SCHEDULE, **(raw if isinstance(raw, dict) else {})}
    try:
        settings["timezone_info"] = ZoneInfo(str(settings["timezone"]))
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown outreach timezone: {settings['timezone']}") from exc
    weekdays = settings.get("weekdays")
    if not isinstance(weekdays, list) or not weekdays or any(
        not isinstance(day, int) or isinstance(day, bool) or day < 0 or day > 6
        for day in weekdays
    ):
        raise ValueError("Outreach weekdays must contain integers from 0 through 6")
    settings["weekdays"] = sorted(set(weekdays))
    for key in ("first_wave_size", "second_wave_delay_business_days", "min_spacing_minutes", "daily_limit"):
        value = settings.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"Outreach schedule field '{key}' must be a positive integer")
    for key in ("send_window_start", "send_window_end"):
        try:
            hour, minute = (int(part) for part in str(settings[key]).split(":"))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Outreach schedule field '{key}' must use HH:MM") from exc
        settings[f"{key}_parts"] = (hour, minute)
    if settings["send_window_start_parts"] >= settings["send_window_end_parts"]:
        raise ValueError("Outreach send window must end after it starts")
    return settings


def _next_eligible_time(moment: datetime, settings: dict) -> datetime:
    tz = settings["timezone_info"]
    local = moment.astimezone(tz)
    start_hour, start_minute = settings["send_window_start_parts"]
    end_hour, end_minute = settings["send_window_end_parts"]
    for _ in range(8):
        if local.weekday() not in settings["weekdays"]:
            local = (local + timedelta(days=1)).replace(
                hour=start_hour, minute=start_minute, second=0, microsecond=0
            )
            continue
        start = local.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        end = local.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        local = max(local, start)
        if local <= end:
            return local.astimezone(UTC)
        local = (local + timedelta(days=1)).replace(
            hour=start_hour, minute=start_minute, second=0, microsecond=0
        )
    raise ValueError("Outreach schedule has no eligible day")


def _add_business_days(moment: datetime, days: int, settings: dict) -> datetime:
    local = moment.astimezone(settings["timezone_info"])
    added = 0
    while added < days:
        local += timedelta(days=1)
        if local.weekday() in settings["weekdays"]:
            added += 1
    return _next_eligible_time(local.astimezone(UTC), settings)


def _parse_timestamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
        if parsed and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC) if parsed else None
    except (TypeError, ValueError):
        return None


def _reserved_send_times(conn: sqlite3.Connection) -> list[datetime]:
    rows = conn.execute(
        "SELECT scheduled_for, sent_at, status FROM outreach_recipients "
        "WHERE status IN ('scheduled', 'sending', 'sent')"
    ).fetchall()
    return [
        timestamp for row in rows
        if (timestamp := _parse_timestamp(row["sent_at"] or row["scheduled_for"])) is not None
    ]


def _reserve_send_time(
    conn: sqlite3.Connection,
    preferred: datetime,
    settings: dict,
    reserved_times: list[datetime] | None = None,
) -> datetime:
    spacing = timedelta(minutes=settings["min_spacing_minutes"])
    tz = settings["timezone_info"]
    reserved = list(reserved_times) if reserved_times is not None else _reserved_send_times(conn)
    candidate = _next_eligible_time(preferred, settings)
    for _ in range(10000):
        local_day = candidate.astimezone(tz).date()
        same_day = [item for item in reserved if item.astimezone(tz).date() == local_day]
        if len(same_day) >= settings["daily_limit"]:
            next_local_day = (candidate.astimezone(tz) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            candidate = _next_eligible_time(next_local_day.astimezone(UTC), settings)
            continue
        conflict = next(
            (item for item in sorted(reserved) if abs(item - candidate) < spacing), None
        )
        if conflict:
            candidate = _next_eligible_time(conflict + spacing, settings)
            continue
        return candidate
    raise ValueError("Could not find an available outreach send time")


def _schedule_selected(
    conn: sqlite3.Connection,
    batch_id: str,
    selected_ids: list[str],
    settings: dict,
    now: datetime,
) -> None:
    spacing = timedelta(minutes=settings["min_spacing_minutes"])
    first_size = min(settings["first_wave_size"], len(selected_ids))
    first_base = _next_eligible_time(now + timedelta(minutes=1), settings)
    first_slot: datetime | None = None
    for index, recipient_id in enumerate(selected_ids):
        if index < first_size:
            preferred = first_base + spacing * index
            wave = 1
        else:
            if first_slot is None:
                first_slot = first_base
            second_base = _add_business_days(
                first_slot, settings["second_wave_delay_business_days"], settings
            )
            preferred = second_base + spacing * (index - first_size)
            wave = 2
        slot = _reserve_send_time(conn, preferred, settings)
        if first_slot is None:
            first_slot = slot
        conn.execute(
            "UPDATE outreach_recipients SET status = 'scheduled', scheduled_for = ?, wave = ?, "
            "error = NULL, updated_at = ? WHERE id = ? AND batch_id = ?",
            (slot.isoformat(), wave, _now(), recipient_id, batch_id),
        )


def preview_batch_schedule(
    batch_id: str,
    recipient_ids: list[str],
    *,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Calculate the exact prospective wave schedule without writing it."""
    if not recipient_ids:
        raise ValueError("Select at least one recipient")
    conn = conn or get_connection()
    batch = _batch_row(batch_id, conn)
    if not batch or batch["status"] not in {"ready_for_review", "failed", "partial_failed"}:
        raise ValueError("This outreach batch is not ready to schedule")
    placeholders = ",".join("?" for _ in recipient_ids)
    valid = {row[0] for row in conn.execute(
        f"SELECT id FROM outreach_recipients WHERE batch_id = ? AND id IN ({placeholders}) "
        "AND status IN ('ready', 'needs_edit', 'failed')",
        [batch["id"], *recipient_ids],
    ).fetchall()}
    if len(valid) != len(set(recipient_ids)):
        raise ValueError("One or more selected recipients are no longer eligible")
    settings = schedule_settings()
    spacing = timedelta(minutes=settings["min_spacing_minutes"])
    first_size = min(settings["first_wave_size"], len(recipient_ids))
    first_base = _next_eligible_time((now or _utc_now()) + timedelta(minutes=1), settings)
    first_slot: datetime | None = None
    reserved = _reserved_send_times(conn)
    result: list[dict] = []
    for index, recipient_id in enumerate(recipient_ids):
        if index < first_size:
            preferred = first_base + spacing * index
            wave = 1
        else:
            second_base = _add_business_days(
                first_slot or first_base,
                settings["second_wave_delay_business_days"],
                settings,
            )
            preferred = second_base + spacing * (index - first_size)
            wave = 2
        slot = _reserve_send_time(conn, preferred, settings, reserved)
        reserved.append(slot)
        if first_slot is None:
            first_slot = slot
        result.append({"id": recipient_id, "wave": wave, "scheduled_for": slot.isoformat()})
    return result


def refresh_delivery_statuses(
    identifier: str,
    *,
    conn: sqlite3.Connection | None = None,
    apollo: ApolloClient | None = None,
) -> dict:
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    rows = conn.execute(
        "SELECT id, apollo_message_id FROM outreach_recipients "
        "WHERE batch_id = ? AND status = 'sending' AND apollo_message_id IS NOT NULL",
        (batch["id"],),
    ).fetchall()
    if rows:
        apollo = apollo or ApolloClient()
    for row in rows:
        try:
            result = apollo.email_status(row["apollo_message_id"])
            status = str(result.get("status") or "").lower()
            if status == "completed":
                conn.execute(
                    "UPDATE outreach_recipients SET status = 'sent', sent_at = ?, error = NULL, updated_at = ? WHERE id = ?",
                    (result.get("completed_at") or _now(), _now(), row["id"]),
                )
            elif status == "failed":
                error = result.get("failure_reason") or result.get("not_sent_reason") or result.get("message")
                conn.execute(
                    "UPDATE outreach_recipients SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                    (str(error or "Apollo could not send the email")[:1000], _now(), row["id"]),
                )
            elif status == "drafted":
                # The message ID was persisted before send_now. Resuming is safe.
                apollo.send_email(row["apollo_message_id"])
        except ApolloError as exc:
            log.warning("Could not refresh Apollo email %s: %s", row["apollo_message_id"], exc)
    _update_batch_after_send(batch["id"], conn)
    conn.commit()
    return get_batch(batch["id"], conn) or {}


def _update_batch_after_send(batch_id: str, conn: sqlite3.Connection) -> None:
    statuses = [row[0] for row in conn.execute(
        "SELECT status FROM outreach_recipients WHERE batch_id = ? AND status != 'excluded'", (batch_id,)
    ).fetchall()]
    if not statuses:
        status = "cancelled"
    elif any(item == "sending" for item in statuses):
        status = "sending"
    elif any(item == "scheduled" for item in statuses):
        status = "partial_failed" if any(item == "failed" for item in statuses) else "scheduled"
    elif any(item == "drafting" for item in statuses):
        status = "drafting"
    elif any(item == "failed" for item in statuses):
        status = "partial_failed" if any(item in {"sent", "drafted"} for item in statuses) else "failed"
    elif any(item == "cancelled" for item in statuses):
        status = "stopped" if any(item in {"sent", "drafted"} for item in statuses) else "cancelled"
    elif all(item in {"sent", "suppressed"} for item in statuses):
        status = "completed"
    elif all(item in {"drafted", "suppressed"} for item in statuses):
        status = "drafted"
    else:
        status = "ready_for_review"
    completed_at = _now() if status == "completed" else None
    conn.execute(
        "UPDATE outreach_batches SET status = ?, completed_at = COALESCE(completed_at, ?), updated_at = ? WHERE id = ?",
        (status, completed_at, _now(), batch_id),
    )


def create_gmail_drafts(
    batch_id: str,
    recipients: list[dict],
    *,
    confirmed_account: str,
    conn: sqlite3.Connection | None = None,
    gmail=None,
) -> dict:
    """Copy reviewed messages to personal Gmail Drafts without sending anything.

    A claimed recipient remains `drafting` after an ambiguous provider failure so
    an automatic retry cannot silently create a duplicate Gmail draft.
    """
    from rolesail.outreach.gmail import GmailDraftClient

    if not isinstance(recipients, list) or not recipients:
        raise ValueError("Select at least one recipient")
    if not isinstance(confirmed_account, str):
        raise TypeError("Confirm the connected Gmail address before creating drafts")
    conn = conn or get_connection()
    batch = _batch_row(batch_id, conn)
    if not batch or batch["status"] not in {"ready_for_review", "failed", "partial_failed", "drafted"}:
        raise ValueError("This outreach batch is not ready for Gmail drafts")
    if conn.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ? AND status IN ('scheduled', 'sending') LIMIT 1",
        (batch["id"],),
    ).fetchone():
        raise ValueError("Cancel the remaining Apollo sends before creating Gmail drafts")
    job = conn.execute("SELECT applied_at FROM jobs WHERE url = ?", (batch["job_url"],)).fetchone()
    if not job or not job["applied_at"]:
        raise ValueError("The job is no longer marked as applied")
    gmail = gmail or GmailDraftClient()
    account_email = str(gmail.email).strip().lower()
    if not account_email or confirmed_account.strip().lower() != account_email:
        raise ValueError("Confirm the connected Gmail address before creating drafts")
    existing_accounts = {
        str(row[0]).lower() for row in conn.execute(
            "SELECT DISTINCT gmail_account_email FROM outreach_recipients "
            "WHERE batch_id = ? AND gmail_account_email IS NOT NULL",
            (batch["id"],),
        ).fetchall()
    }
    if existing_accounts and existing_accounts != {account_email}:
        raise ValueError("This batch already has Gmail drafts in another account; reconnect that account to continue")

    edits: list[tuple[str, str, str]] = []
    for edit in recipients:
        if not isinstance(edit, dict) or not edit.get("id"):
            raise ValueError("Each selected recipient must include an ID")
        subject = str(edit.get("subject") or "").strip()
        body = _flowing_email_body(edit.get("body_text") or "")
        if not subject or not body or len(subject) > 200 or len(body) > 4000 or "\n" in subject or "\r" in subject:
            raise ValueError("Every selected draft needs a valid subject and body")
        edits.append((str(edit["id"]), subject, body))
    ids = [item[0] for item in edits]
    if len(set(ids)) != len(ids):
        raise ValueError("A recipient can only be selected once")
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM outreach_recipients WHERE batch_id = ? AND id IN ({placeholders})",
        [batch["id"], *ids],
    ).fetchall()
    selected = {row["id"]: dict(row) for row in rows}
    if len(selected) != len(ids) or any(
        selected[item_id]["status"] not in {"ready", "needs_edit", "failed", "drafted"}
        or not selected[item_id]["email"]
        or any(character in selected[item_id]["email"] for character in "\r\n")
        or selected[item_id]["email_status"] != "verified"
        or (selected[item_id]["status"] == "drafted" and (
            not selected[item_id]["gmail_draft_id"]
            or selected[item_id]["subject"] != subject
            or selected[item_id]["body_text"] != body
        ))
        for item_id, subject, body in edits
    ):
        raise ValueError("A selected recipient is no longer eligible for a Gmail draft")
    if any(is_suppressed(selected[item_id]["apollo_person_id"], selected[item_id]["email"], conn) for item_id in ids):
        raise ValueError("A selected recipient is suppressed")
    _require_valid_reviewed_introductions(edits)

    conn.execute("SAVEPOINT gmail_draft_approval")
    try:
        conn.execute(
            f"UPDATE outreach_recipients SET status = 'excluded', updated_at = ? "
            f"WHERE batch_id = ? AND status IN ('ready', 'needs_edit', 'failed') AND id NOT IN ({placeholders})",
            [_now(), batch["id"], *ids],
        )
        conn.execute(
            "UPDATE outreach_batches SET status = 'drafting', approved_at = COALESCE(approved_at, ?), "
            "error = NULL, updated_at = ? WHERE id = ?",
            (_now(), _now(), batch["id"]),
        )
        conn.execute("RELEASE SAVEPOINT gmail_draft_approval")
    except sqlite3.Error:
        conn.execute("ROLLBACK TO SAVEPOINT gmail_draft_approval")
        conn.execute("RELEASE SAVEPOINT gmail_draft_approval")
        raise
    conn.commit()

    for item_id, subject, body in edits:
        if selected[item_id]["status"] == "drafted":
            continue
        claimed = conn.execute(
            "UPDATE outreach_recipients SET status = 'drafting', subject = ?, body_text = ?, "
            "gmail_account_email = ?, error = NULL, updated_at = ? "
            "WHERE id = ? AND batch_id = ? AND status IN ('ready', 'needs_edit', 'failed')",
            (subject, body, account_email, _now(), item_id, batch["id"]),
        ).rowcount
        conn.commit()
        if not claimed:
            continue
        try:
            draft_id = gmail.create_draft(
                recipient_email=selected[item_id]["email"], subject=subject, body_text=body
            )
            if not draft_id:
                raise RuntimeError("Gmail returned no draft ID")
        except Exception as exc:  # noqa: BLE001 - an interrupted create may have succeeded remotely
            status_code = getattr(getattr(exc, "resp", None), "status", None)
            definitely_rejected = isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429
            state = "failed" if definitely_rejected else "drafting"
            detail = str(exc)[:700] if definitely_rejected else (
                "Gmail draft creation may have succeeded. Check Gmail Drafts before trying again: " + str(exc)[:700]
            )
            conn.execute(
                "UPDATE outreach_recipients SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (state, detail, _now(), item_id),
            )
        else:
            conn.execute(
                "UPDATE outreach_recipients SET status = 'drafted', gmail_draft_id = ?, "
                "error = NULL, updated_at = ? WHERE id = ?",
                (draft_id, _now(), item_id),
            )
        conn.commit()
    _update_batch_after_send(batch["id"], conn)
    conn.commit()
    return get_batch(batch["id"], conn) or {}


def reset_uncertain_gmail_draft(
    recipient_id: str,
    *,
    confirmed_no_draft: bool,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Allow retry only after the user has checked Gmail for a missing draft."""
    if confirmed_no_draft is not True:
        raise ValueError("Confirm that no matching draft exists in Gmail")
    conn = conn or get_connection()
    recipient = conn.execute(
        "SELECT * FROM outreach_recipients WHERE id = ?", (recipient_id,)
    ).fetchone()
    if not recipient or recipient["status"] != "drafting" or recipient["gmail_draft_id"]:
        raise ValueError("This recipient has no uncertain Gmail draft to reset")
    last_change = _parse_timestamp(recipient["updated_at"])
    if last_change and _utc_now() - last_change < timedelta(minutes=2):
        raise ValueError("Wait two minutes for Gmail Drafts to update, then check again")
    conn.execute(
        "UPDATE outreach_recipients SET status = 'failed', error = ?, updated_at = ? WHERE id = ? AND status = 'drafting'",
        ("User confirmed no matching Gmail draft exists; ready to retry", _now(), recipient_id),
    )
    _update_batch_after_send(recipient["batch_id"], conn)
    conn.commit()
    return get_batch(recipient["batch_id"], conn) or {}


def approve_batch(
    batch_id: str,
    recipients: list[dict],
    *,
    confirmed: bool,
    conn: sqlite3.Connection | None = None,
    apollo: ApolloClient | None = None,
    now: datetime | None = None,
) -> dict:
    """Persist reviewed edits and durably schedule explicitly selected recipients."""
    if confirmed is not True:
        raise ValueError("Explicit send confirmation is required")
    if not isinstance(recipients, list) or not recipients:
        raise ValueError("Select at least one recipient")
    email_account_id = os.environ.get("APOLLO_EMAIL_ACCOUNT_ID", "").strip()
    if not email_account_id:
        raise ApolloError("APOLLO_EMAIL_ACCOUNT_ID is not configured")
    conn = conn or get_connection()
    batch = _batch_row(batch_id, conn)
    if not batch or batch["status"] not in {"ready_for_review", "failed", "partial_failed"}:
        raise ValueError("This outreach batch is not ready to send")
    if conn.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ? "
        "AND (gmail_account_email IS NOT NULL OR gmail_draft_id IS NOT NULL) LIMIT 1",
        (batch["id"],),
    ).fetchone():
        raise ValueError("This batch uses Gmail drafts and cannot be sent through Apollo")
    normalized_edits: list[tuple[str, str, str]] = []
    for edit in recipients:
        if not isinstance(edit, dict) or not edit.get("id"):
            raise ValueError("Each selected recipient must include an ID")
        subject = str(edit.get("subject") or "").strip()
        body = _flowing_email_body(edit.get("body_text") or "")
        if not subject or not body or len(subject) > 200 or len(body) > 4000:
            raise ValueError("Every selected email needs a valid subject and body")
        normalized_edits.append((str(edit["id"]), subject, body))
    selected_ids = [item[0] for item in normalized_edits]
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError("A recipient can only be selected once")
    _require_valid_reviewed_introductions(normalized_edits)
    settings = schedule_settings()
    conn.execute("SAVEPOINT approve_outreach")
    try:
        for recipient_id, subject, body in normalized_edits:
            updated = conn.execute(
                "UPDATE outreach_recipients SET subject = ?, body_text = ?, updated_at = ? "
                "WHERE id = ? AND batch_id = ? AND status IN ('ready', 'needs_edit', 'failed')",
                (subject, body, _now(), recipient_id, batch["id"]),
            ).rowcount
            if not updated:
                raise ValueError("A selected recipient is no longer eligible to send")
        placeholders = ",".join("?" for _ in selected_ids)
        conn.execute(
            f"UPDATE outreach_recipients SET status = 'excluded', updated_at = ? "
            f"WHERE batch_id = ? AND status IN ('ready', 'needs_edit', 'failed') AND id NOT IN ({placeholders})",
            [_now(), batch["id"], *selected_ids],
        )
        _schedule_selected(conn, batch["id"], selected_ids, settings, now or _utc_now())
        conn.execute(
            "UPDATE outreach_batches SET status = 'scheduled', approved_at = COALESCE(approved_at, ?), "
            "error = NULL, updated_at = ? WHERE id = ?",
            (_now(), _now(), batch["id"]),
        )
        conn.execute("RELEASE SAVEPOINT approve_outreach")
    except (ValueError, sqlite3.Error):
        conn.execute("ROLLBACK TO SAVEPOINT approve_outreach")
        conn.execute("RELEASE SAVEPOINT approve_outreach")
        raise
    conn.commit()
    return get_batch(batch["id"], conn) or {}


def _is_transient_send_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, ApolloError):
        return exc.status_code is None or exc.status_code == 429 or (exc.status_code or 0) >= 500
    return False


def _reschedule_after_failure(
    conn: sqlite3.Connection,
    recipient: dict,
    exc: Exception,
    now: datetime,
) -> None:
    attempts = int(recipient.get("attempt_count") or 0)
    if _is_transient_send_error(exc) and attempts <= len(RETRY_DELAYS_MINUTES):
        delay = RETRY_DELAYS_MINUTES[attempts - 1]
        settings = schedule_settings()
        slot = _reserve_send_time(conn, now + timedelta(minutes=delay), settings)
        conn.execute(
            "UPDATE outreach_recipients SET status = 'scheduled', scheduled_for = ?, error = ?, "
            "updated_at = ? WHERE id = ?",
            (slot.isoformat(), str(exc)[:1000], _now(), recipient["id"]),
        )
    else:
        conn.execute(
            "UPDATE outreach_recipients SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (str(exc)[:1000], _now(), recipient["id"]),
        )


def dispatch_due_outreach(
    *,
    conn: sqlite3.Connection | None = None,
    apollo: ApolloClient | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Atomically claim and dispatch the next due outreach recipient."""
    conn = conn or get_connection()
    current = (now or _utc_now()).astimezone(UTC)
    row = conn.execute(
        "SELECT r.*, b.job_url FROM outreach_recipients r "
        "JOIN outreach_batches b ON b.id = r.batch_id "
        "WHERE r.status = 'scheduled' AND r.scheduled_for <= ? "
        "AND b.status IN ('scheduled', 'partial_failed') "
        "ORDER BY r.scheduled_for, r.created_at LIMIT 1",
        (current.isoformat(),),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    settings = schedule_settings()
    next_window = _next_eligible_time(current, settings)
    recent_attempts = [
        timestamp
        for attempt_row in conn.execute(
            "SELECT last_attempt_at, sent_at FROM outreach_recipients "
            "WHERE id != ? AND (last_attempt_at IS NOT NULL OR sent_at IS NOT NULL)",
            (item["id"],),
        ).fetchall()
        if (timestamp := _parse_timestamp(attempt_row["last_attempt_at"] or attempt_row["sent_at"]))
    ]
    earliest_for_spacing = (
        max(recent_attempts) + timedelta(minutes=settings["min_spacing_minutes"])
        if recent_attempts else current
    )
    preferred = max(next_window, earliest_for_spacing)
    tz = settings["timezone_info"]
    today = current.astimezone(tz).date()
    attempts_today = sum(
        1 for attempt in recent_attempts if attempt.astimezone(tz).date() == today
    )
    if attempts_today >= settings["daily_limit"]:
        next_local_day = (current.astimezone(tz) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        preferred = max(
            preferred,
            _next_eligible_time(next_local_day.astimezone(UTC), settings),
        )
    if preferred > current:
        conn.execute(
            "UPDATE outreach_recipients SET scheduled_for = NULL WHERE id = ? AND status = 'scheduled'",
            (item["id"],),
        )
        slot = _reserve_send_time(conn, preferred, settings)
        conn.execute(
            "UPDATE outreach_recipients SET scheduled_for = ?, updated_at = ? "
            "WHERE id = ? AND status = 'scheduled'",
            (slot.isoformat(), _now(), item["id"]),
        )
        conn.commit()
        return get_batch(item["batch_id"], conn)
    job = conn.execute("SELECT applied_at FROM jobs WHERE url = ?", (item["job_url"],)).fetchone()
    if not job or not job["applied_at"]:
        conn.execute(
            "UPDATE outreach_recipients SET status = 'cancelled', error = ?, updated_at = ? WHERE id = ?",
            ("The job is no longer marked as applied", _now(), item["id"]),
        )
        _update_batch_after_send(item["batch_id"], conn)
        conn.commit()
        return get_batch(item["batch_id"], conn)
    claimed = conn.execute(
        "UPDATE outreach_recipients SET status = 'sending', attempt_count = attempt_count + 1, "
        "last_attempt_at = ?, updated_at = ? WHERE id = ? AND status = 'scheduled'",
        (current.isoformat(), _now(), item["id"]),
    ).rowcount
    conn.commit()
    if not claimed:
        return None
    item = dict(conn.execute(
        "SELECT * FROM outreach_recipients WHERE id = ?", (item["id"],)
    ).fetchone())
    if is_suppressed(item["apollo_person_id"], item["email"], conn):
        conn.execute(
            "UPDATE outreach_recipients SET status = 'suppressed', updated_at = ? WHERE id = ?",
            (_now(), item["id"]),
        )
        _update_batch_after_send(item["batch_id"], conn)
        conn.commit()
        return get_batch(item["batch_id"], conn)

    try:
        email_account_id = os.environ.get("APOLLO_EMAIL_ACCOUNT_ID", "").strip()
        if not email_account_id:
            raise ApolloError("APOLLO_EMAIL_ACCOUNT_ID is not configured", status_code=422)
        apollo = apollo or ApolloClient()
        contact_id = item.get("apollo_contact_id")
        if not contact_id:
            contact_id = apollo.create_contact(item)["id"]
        message_id = item.get("apollo_message_id")
        if not message_id:
            message_id = apollo.create_email_draft(
                contact_id=contact_id,
                subject=item["subject"],
                body_html=_body_html(item["body_text"]),
                email_account_id=email_account_id,
            )["id"]
        conn.execute(
            "UPDATE outreach_recipients SET apollo_contact_id = ?, apollo_message_id = ?, "
            "error = NULL, updated_at = ? WHERE id = ?",
            (contact_id, message_id, _now(), item["id"]),
        )
        conn.commit()
        apollo.send_email(message_id)
    except Exception as exc:  # noqa: BLE001 - persist unexpected provider failures per recipient
        _reschedule_after_failure(conn, item, exc, current)
        _update_batch_after_send(item["batch_id"], conn)
        conn.commit()
        return get_batch(item["batch_id"], conn)
    _update_batch_after_send(item["batch_id"], conn)
    conn.commit()
    return refresh_delivery_statuses(item["batch_id"], conn=conn, apollo=apollo)


def recover_outreach_dispatcher(
    *, conn: sqlite3.Connection | None = None, apollo: ApolloClient | None = None
) -> None:
    """Recover in-flight sends and redistribute overdue schedules after restart."""
    conn = conn or get_connection()
    without_message = conn.execute(
        "SELECT id FROM outreach_recipients WHERE status = 'sending' AND apollo_message_id IS NULL"
    ).fetchall()
    if without_message:
        settings = schedule_settings()
        for row in without_message:
            slot = _reserve_send_time(conn, _utc_now(), settings)
            conn.execute(
                "UPDATE outreach_recipients SET status = 'scheduled', scheduled_for = ?, updated_at = ? WHERE id = ?",
                (slot.isoformat(), _now(), row["id"]),
            )
        conn.commit()
    refresh_inflight_outreach(conn=conn, apollo=apollo)

    overdue = conn.execute(
        "SELECT id FROM outreach_recipients WHERE status = 'scheduled' AND scheduled_for < ? "
        "ORDER BY scheduled_for, created_at",
        (_now(),),
    ).fetchall()
    if overdue:
        settings = schedule_settings()
        for row in overdue:
            conn.execute(
                "UPDATE outreach_recipients SET scheduled_for = NULL WHERE id = ?", (row["id"],)
            )
            slot = _reserve_send_time(conn, _utc_now(), settings)
            conn.execute(
                "UPDATE outreach_recipients SET scheduled_for = ?, updated_at = ? WHERE id = ?",
                (slot.isoformat(), _now(), row["id"]),
            )
        conn.commit()


def refresh_inflight_outreach(
    *, conn: sqlite3.Connection | None = None, apollo: ApolloClient | None = None
) -> None:
    conn = conn or get_connection()
    batch_ids = [row[0] for row in conn.execute(
        "SELECT DISTINCT batch_id FROM outreach_recipients "
        "WHERE status = 'sending' AND apollo_message_id IS NOT NULL"
    ).fetchall()]
    for batch_id in batch_ids:
        refresh_delivery_statuses(batch_id, conn=conn, apollo=apollo)
