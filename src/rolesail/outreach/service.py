"""Durable preparation, review, and sending for post-application outreach."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

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
DEFAULT_SCHEDULE = {
    "timezone": "America/Toronto",
    "weekdays": [0, 1, 2, 3, 4],
    "send_window_start": "09:00",
    "send_window_end": "16:00",
    "first_wave_size": 2,
    "second_wave_delay_business_days": 2,
    "min_spacing_minutes": 10,
    "daily_limit": 15,
}
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


def _domain_from_organization(organization: dict) -> str:
    domain = str(
        organization.get("primary_domain")
        or organization.get("website_url")
        or organization.get("website")
        or ""
    ).strip()
    if "://" not in domain:
        domain = f"https://{domain}"
    return (urlparse(domain).hostname or "").lower().removeprefix("www.")


def _choose_organization(company: str, organizations: list[dict]) -> dict | None:
    normalized = re.sub(r"[^a-z0-9]", "", company.lower())
    for organization in organizations:
        name = re.sub(r"[^a-z0-9]", "", str(organization.get("name") or "").lower())
        if name == normalized:
            return organization
    return organizations[0] if organizations else None


def _workday_tenant_alias(*urls: str | None) -> str | None:
    """Extract a company-owned Workday tenant as a conservative search alias."""
    for url in urls:
        host = (urlparse(str(url or "")).hostname or "").lower()
        match = re.fullmatch(r"([a-z0-9_-]+)\.wd\d+\.myworkdayjobs\.com", host)
        if match:
            return re.sub(r"[-_]+", " ", match.group(1)).strip()
    return None


def _resolve_organization(job: dict, apollo: ApolloClient) -> tuple[dict | None, str]:
    """Resolve an Apollo organization, retrying an exact Workday tenant alias."""
    company = str(job.get("company") or "").strip()
    organizations = apollo.search_organizations(company)
    organization = _choose_organization(company, organizations)
    domain = _domain_from_organization(organization or {})
    if domain:
        return organization, domain

    alias = _workday_tenant_alias(job.get("url"), job.get("application_url"))
    if alias and re.sub(r"[^a-z0-9]", "", alias.lower()) != re.sub(
        r"[^a-z0-9]", "", company.lower()
    ):
        alias_organization = _choose_organization(alias, apollo.search_organizations(alias))
        alias_domain = _domain_from_organization(alias_organization or {})
        if alias_domain:
            return alias_organization, alias_domain
    return organization, ""


def _candidate_kind(title: str) -> str:
    lowered = title.lower()
    if any(word in lowered for word in RECRUITER_WORDS):
        return "recruiter"
    if any(word in lowered for word in LEADER_WORDS):
        return "leader"
    if any(word in lowered for word in MANAGER_WORDS):
        return "manager"
    return "peer"


def _role_terms(job_title: str, description: str) -> set[str]:
    ignored = {"senior", "junior", "staff", "lead", "manager", "engineer", "developer", "the", "and", "with"}
    words = re.findall(r"[a-z][a-z+#.-]{2,}", f"{job_title} {description[:3000]}".lower())
    return {word.strip(".-") for word in words if word not in ignored}


def _location_queries(location: str | None) -> list[str]:
    """Return useful Apollo location filters, omitting remote-only fragments."""
    queries: list[str] = []
    for raw_part in re.split(r"\s*;\s*", str(location or "")):
        part = raw_part.strip()
        if not part:
            continue
        for word in REMOTE_ONLY_WORDS:
            part = re.sub(rf"\b{re.escape(word)}\b", " ", part, flags=re.IGNORECASE)
        part = re.sub(r"\s*[-–—()/]\s*", " ", part)
        part = re.sub(r"(?:^\s*,\s*|\s*,\s*$)", "", part)
        part = re.sub(r"\s+", " ", part).strip(" ,-–—")
        if part and part.lower() not in {item.lower() for item in queries}:
            queries.append(part)
    return queries[:5]


def _normalized_location(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _location_contains(job_location: str, value: object) -> bool:
    needle = _normalized_location(value)
    haystack = f" {_normalized_location(job_location)} "
    return bool(needle and f" {needle} " in haystack)


def _location_preference(person: dict, job_location: str | None) -> tuple[int, str | None]:
    """Score explicit Apollo location fields without excluding unknown locations."""
    if not job_location or not _location_queries(job_location):
        return 0, None
    if person.get("_location_search_match"):
        return 50, f"Located in or near the role's location ({job_location})"

    city = person.get("city")
    state = person.get("state")
    country = person.get("country")
    if _location_contains(job_location, city):
        return 50, f"Same city as the role ({city})"
    if _location_contains(job_location, state):
        return 30, f"Same region as the role ({state})"
    if _location_contains(job_location, country):
        return 12, f"Same country as the role ({country})"
    return 0, None


def rank_people(
    people: list[dict],
    job_title: str,
    description: str,
    job_location: str | None = None,
) -> list[dict]:
    """Rank current employees and retain a balanced hiring-circle ordering."""
    terms = _role_terms(job_title, description)
    ranked: list[dict] = []
    for person in people:
        person_id = person.get("id") or person.get("person_id")
        title = str(person.get("title") or "")
        if not person_id or not title:
            continue
        if person.get("employment_history") and not any(
            bool(item.get("current")) for item in person.get("employment_history", [])
        ):
            continue
        title_terms = set(re.findall(r"[a-z][a-z+#.-]{2,}", title.lower()))
        kind = _candidate_kind(title)
        score = min(60, len(terms & title_terms) * 15)
        score += {"manager": 30, "leader": 24, "recruiter": 20, "peer": 15}[kind]
        location_score, location_reason = _location_preference(person, job_location)
        score += location_score
        item = dict(person)
        item["person_id"] = str(person_id)
        item["candidate_kind"] = kind
        item["relevance_score"] = score
        role_reason = {
            "manager": "Likely manager for the role's function",
            "leader": "Leader in a function related to the role",
            "recruiter": "Recruiting or talent contact",
            "peer": "Senior employee close to the role's team",
        }[kind]
        item["relevance_reason"] = (
            f"{role_reason}; {location_reason}" if location_reason else role_reason
        )
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["relevance_score"], item.get("name") or ""))
    balanced: list[dict] = []
    for kind in ("manager", "leader", "recruiter", "peer"):
        match = next((item for item in ranked if item["candidate_kind"] == kind and item not in balanced), None)
        if match:
            balanced.append(match)
    balanced.extend(item for item in ranked if item not in balanced)
    return balanced


def _already_contacted(email: str, domain: str, conn: sqlite3.Connection) -> bool:
    cutoff = (datetime.now(UTC) - timedelta(days=SAME_COMPANY_COOLDOWN_DAYS)).isoformat()
    return bool(conn.execute(
        "SELECT 1 FROM outreach_recipients r JOIN outreach_batches b ON b.id = r.batch_id "
        "WHERE lower(r.email) = lower(?) AND b.company_domain = ? AND r.status = 'sent' "
        "AND r.sent_at >= ? LIMIT 1",
        (email, domain, cutoff),
    ).fetchone())


def _is_suppressed(person_id: str, email: str, conn: sqlite3.Connection) -> bool:
    keys = [f"person:{person_id}", f"email:{email.lower()}"]
    return bool(conn.execute(
        "SELECT 1 FROM outreach_suppressions WHERE key IN (?, ?) LIMIT 1", keys
    ).fetchone())


def _extract_json(text: str):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL
        )
    start = min((index for index in (cleaned.find("["), cleaned.find("{")) if index >= 0), default=-1)
    if start < 0:
        raise ValueError("The LLM did not return JSON")
    return json.loads(cleaned[start:])


def _message_words(body: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", body))


def _introduction_sentence(body: str, candidate_first_name: str = "") -> str:
    """Return the candidate's short biographical sentence after the greeting."""
    prose = re.sub(r"^hi\s+[^,\n]+,\s*", "", body.strip(), count=1, flags=re.IGNORECASE)
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])(?:\s+|$)", prose)
        if sentence.strip()
    ]
    if candidate_first_name:
        named_intro = re.compile(
            rf"^(?:i'm|i am)\s+{re.escape(candidate_first_name)}\b",
            re.IGNORECASE,
        )
        match = next((sentence for sentence in sentences if named_intro.search(sentence)), None)
        if match:
            return match
    return sentences[0] if sentences else ""


def _introduction_errors(body: str, candidate_first_name: str) -> list[str]:
    """Check the introduction in both generated and reviewed outreach."""
    introduction = _introduction_sentence(body, candidate_first_name)
    intro_lower = introduction.casefold().replace("’", "'")
    failures: list[str] = []
    if candidate_first_name and candidate_first_name.casefold() not in intro_lower:
        failures.append("message does not include a short biographical sentence with the candidate's name")
    if not re.search(
        r"\b(?:graduate|graduated|student|degree|university|college|studied)\b",
        intro_lower,
    ):
        failures.append("biographical sentence does not establish the candidate's education")
    if _message_words(introduction) > 35:
        failures.append("biographical sentence is too detailed; keep it under 36 words")
    if candidate_first_name.casefold() == "ishav":
        required_prefix = ISHAV_INTRO_PREFIX.casefold()
        if not intro_lower.startswith(required_prefix):
            failures.append(f"biographical sentence must begin exactly with: {ISHAV_INTRO_PREFIX}")
        else:
            technical_clause = introduction[len(ISHAV_INTRO_PREFIX):].strip(" .")
            if not technical_clause or _message_words(technical_clause) > 12:
                failures.append(
                    "biographical sentence must end with one or two concise, job-relevant technical areas"
                )
            if re.search(
                r"\b(?:caching|fault tolerance|latency|throughput|scalability)\b",
                technical_clause,
                re.IGNORECASE,
            ):
                failures.append("biographical sentence contains overly detailed technical concerns")
    return failures


def _require_valid_reviewed_introductions(edits: list[tuple[str, str, str]]) -> None:
    """Reject an edited intro before scheduling or creating external drafts."""
    profile = config.load_profile()
    personal = profile.get("personal") or {}
    outreach = profile.get("outreach") or {}
    candidate_name = str(personal.get("full_name") or outreach.get("signature") or "").strip()
    if not candidate_name:
        raise ValueError("Set a candidate name in the profile before approving outreach")
    first_name = candidate_name.split()[0]
    for recipient_id, _subject, body in edits:
        failures = _introduction_errors(body, first_name)
        if failures:
            raise ValueError(f"Recipient {recipient_id}: {failures[0]}")


def _introduction_employer(profile: dict, samples: list) -> str:
    """Find a structured or explicitly stated current employer for the introduction."""
    experience = profile.get("experience") or {}
    employer = str(experience.get("current_company") or "").strip()
    if employer:
        return employer
    sample_text = "\n".join(str(item) for item in samples if str(item).strip())
    match = re.search(
        r"\bcurrently work(?:ing)?(?:\s+as\s+[^.\n]{1,100}?)?\s+at\s+"
        r"([A-Z][A-Za-z0-9&.'’ -]{1,60})(?=[,.\n])",
        sample_text,
    )
    return match.group(1).strip() if match else ""


def _normalized_message(body: str, first_name: str, signature: str) -> str:
    normalized = body.strip().lower()
    normalized = re.sub(rf"^hi\s+{re.escape(first_name.lower())}\s*,?\s*", "", normalized)
    if signature:
        normalized = re.sub(rf"\s*{re.escape(signature.strip().lower())}\s*$", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _flowing_email_body(body: str) -> str:
    """Remove hard-wrapped prose while retaining intentional email paragraphs."""
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return ""
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", body):
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in block.split("\n")]
        lines = [line for line in lines if line]
        if not lines:
            continue
        if len(lines) > 1 and re.match(r"^hi\s+[^,]+,$", lines[0], re.IGNORECASE):
            paragraphs.append(lines.pop(0))
        if lines:
            paragraph = " ".join(lines)
            sign_off = re.fullmatch(
                r"((?:thanks|thank you|best|regards|best regards|kind regards|sincerely|cheers),)\s+"
                r"([^\n.!?]{1,100})",
                paragraph,
                re.IGNORECASE,
            )
            paragraphs.append(
                f"{sign_off.group(1)}\n{sign_off.group(2)}" if sign_off else paragraph
            )
    return "\n\n".join(paragraphs)


def _outreach_job_link(job: dict) -> str | None:
    """Return a clean saved posting URL, never an application form or tracking link."""
    link = str(job.get("url") or "").strip()
    if not link or len(link) > 200 or re.search(r"\s", link):
        return None
    parsed = urlparse(link)
    if (parsed.scheme != "https" or not parsed.hostname or "." not in parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path in {"", "/"}):
        return None
    if re.search(r"/(?:apply|application|apply-now)/?$", parsed.path, re.IGNORECASE):
        return None
    host = parsed.hostname.lower()
    if any(host == domain or host.endswith("." + domain) for domain in (
        "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    )):
        return None
    return link


def _message_errors(
    job: dict,
    recipients: list[dict],
    messages: list[dict],
    signature: str,
    introduction_employer: str = "",
) -> dict[str, list[str]]:
    """Return deterministic safety and authenticity failures by person ID."""
    errors: dict[str, list[str]] = {}
    recipients_by_id = {str(item["person_id"]): item for item in recipients}
    messages_by_id = {
        str(item.get("person_id")): item for item in messages if isinstance(item, dict)
    }
    seen_subjects: dict[str, str] = {}
    normalized_bodies: dict[str, str] = {}
    role = str(job.get("title") or "").strip()
    job_link = _outreach_job_link(job)
    candidate_first_name = signature.strip().split()[0] if signature.strip() else ""

    for person_id, recipient in recipients_by_id.items():
        item = messages_by_id.get(person_id)
        failures: list[str] = []
        if not item:
            errors[person_id] = ["missing draft"]
            continue
        subject = str(item.get("subject") or "").strip()
        body = str(item.get("body_text") or "").strip()
        first_name = str(recipient.get("first_name") or "").strip()
        lowered = f"{subject}\n{body}".lower().replace("’", "'")
        subject_words = _message_words(subject)
        if not subject or len(subject) > 70 or not 2 <= subject_words <= 8:
            failures.append("subject must be 2-8 words and no more than 70 characters")
        if re.match(r"^(?:re|fwd?)\s*:", subject, flags=re.IGNORECASE):
            failures.append("subject falsely implies a reply or forward")
        letters = re.sub(r"[^A-Za-z]", "", subject)
        if len(letters) >= 6 and letters.isupper():
            failures.append("subject uses all caps")
        if re.search(r"[!?]{2,}", f"{subject}\n{body}"):
            failures.append("message uses repeated punctuation")
        body_without_job_link = body
        if job_link:
            body_without_job_link = re.sub(
                rf"(?<!\S){re.escape(job_link)}(?!\S)", "", body, count=1
            )
        if re.search(r"(?:https?://|www\.|\b[a-z0-9.-]+\.(?:com|net|org|io)\b)", body_without_job_link, re.IGNORECASE):
            failures.append("message contains a URL")
        for phrase in PROHIBITED_PHRASES:
            if phrase in lowered:
                failures.append(f"message uses prohibited phrase: {phrase}")
        if re.search(r"\burgent\b", lowered):
            failures.append("message uses urgency language")
        if re.search(
            r"\b(?:applied|applying)\s+(?:for|to)\s+the\s+exact\b.{0,80}\b(?:role|position)\b",
            lowered,
        ):
            failures.append("message unnaturally describes the opening as an exact role")
        questions = re.findall(r"[^.!?\n]*\?", body)
        if len(questions) != 1:
            failures.append("message must contain exactly one question")
        elif _message_words(questions[0]) > 25:
            failures.append("closing question must be no more than 25 words")
        if re.search(
            r"\b(?:coffee|15[- ]minute)\s+(?:chat|call|conversation|meeting)\b"
            r"|\bhop on (?:a )?call\b"
            r"|\b(?:open|available|free|willing)\s+to\s+(?:have\s+)?(?:a\s+)?(?:chat|talk|meet|call)\b",
            lowered,
        ):
            failures.append("first-touch message asks for a chat, call, or meeting")
        count = _message_words(body)
        if count < 70 or count > 120:
            failures.append(f"message is {count} words; required range is 70-120")
        if first_name and not re.match(
            rf"^hi\s+{re.escape(first_name)}\s*,", body, flags=re.IGNORECASE
        ):
            failures.append(f"message must begin with 'Hi {first_name},'")
        failures.extend(_introduction_errors(body, candidate_first_name))
        if role and role.lower() not in body.lower():
            failures.append("message does not mention the job title")
        if job_link and job_link not in body:
            failures.append("message does not include the exact job-posting link")
        used_facts = item.get("used_facts")
        if not isinstance(used_facts, list) or not any(str(fact).strip() for fact in used_facts):
            failures.append("message has no source-backed used_fact")
        subject_key = subject.casefold()
        if subject_key in seen_subjects:
            failures.append(f"subject duplicates recipient {seen_subjects[subject_key]}")
        elif subject_key:
            seen_subjects[subject_key] = person_id
        normalized_bodies[person_id] = _normalized_message(body, first_name, signature)
        if failures:
            errors[person_id] = failures

    ids = list(normalized_bodies)
    for index, left_id in enumerate(ids):
        for right_id in ids[index + 1:]:
            similarity = SequenceMatcher(
                None, normalized_bodies[left_id], normalized_bodies[right_id]
            ).ratio()
            if similarity > 0.75:
                errors.setdefault(left_id, []).append(
                    f"body is {similarity:.0%} similar to recipient {right_id}"
                )
                errors.setdefault(right_id, []).append(
                    f"body is {similarity:.0%} similar to recipient {left_id}"
                )
    return errors


def _parse_message_output(text: str) -> list[dict]:
    result = _extract_json(text)
    if not isinstance(result, list):
        raise TypeError("The LLM returned an invalid email batch")
    messages = [dict(item) for item in result if isinstance(item, dict)]
    for item in messages:
        if "body_text" in item:
            item["body_text"] = _flowing_email_body(item["body_text"])
    return messages


def _generate_messages(job: dict, recipients: list[dict], research: dict, profile: dict) -> list[dict]:
    from rolesail.llm import get_client

    samples = profile.get("outreach", {}).get("writing_samples", [])
    if len([item for item in samples if str(item).strip()]) < 3:
        raise ValueError("Add at least three outreach writing samples to your profile")
    candidate_name = str(profile.get("personal", {}).get("full_name") or "").strip()
    signature = str(profile.get("outreach", {}).get("signature") or profile.get("personal", {}).get("full_name") or "")
    resume_facts = profile.get("resume_facts", {})
    introduction_employer = _introduction_employer(profile, samples)
    introduction_template = (
        ISHAV_INTRO_PREFIX + "[one or two broad technical areas relevant to this job]."
        if (candidate_name.split(maxsplit=1) or [""])[0].casefold() == "ishav"
        else "I'm [first name], a recent [field] graduate from [school] with experience in [one or two broad technical areas relevant to this job]."
    )
    job_link = _outreach_job_link(job)
    safe_research = {
        "apollo": research.get("apollo", {}),
        "official_pages": [
            {"url": page["url"], "text": page["text"][:2500]}
            for page in research.get("official_pages", [])
        ],
    }
    prompt = f"""Write one punchy, human networking email per recipient after a job application. The goal is to earn a thoughtful reply that starts a useful professional exchange and improves the candidate's chance of an interview—not to summarize the resume.
Return ONLY a JSON array with objects: person_id, subject, body_text, used_facts (array of short source-backed facts).

Rules:
- Silently infer the candidate's recurring formality, contractions, sentence length, vocabulary, directness, and sign-off from the writing samples. Reproduce those traits without copying unrelated facts.
- Keep the complete email to 70-120 words. Use short sentences, concrete language, and plain text. Begin exactly with "Hi [first name],".
- Let the email client wrap text naturally: never hard-wrap prose or insert a newline within a paragraph. Separate intentional paragraphs with one blank line.
- Format the sign-off on exactly two lines, with the closing phrase on one line and the supplied signature on the next (for example, "Thanks,\nIshav Sohal"). Never place them together on one line.
- Write a 2-8 word subject (70 characters maximum) around a specific role, team, company priority, or recipient-relevant angle. Make it informative and intriguing, not vague or clickbait. Avoid generic subjects such as "Job application," "Quick question," or "Exciting opportunity."
- The first prose sentence after the greeting is the hook. In 22 words or fewer, lead with a source-backed detail about the role, company, or recipient and make the reason for writing immediately clear. Do not open with the candidate's biography, generic praise, or "I applied..." by itself. If no individual-specific fact is supplied, personalize to the recipient's function/title and the role; never invent a post, project, shared connection, or familiarity.
- Follow the hook with one short biographical sentence that uses REQUIRED INTRODUCTION TEMPLATE below and is no more than 35 words. Keep the words "with experience in" before the technical areas; do not place technical areas directly after "with". Replace its bracketed slot with only one or two broad technical areas relevant to this job, such as "backend systems and AI infrastructure."
- Keep that biographical sentence high-level. Do not describe multiple projects, responsibilities, tools, or detailed engineering concerns there. Never put scalability, caching, fault tolerance, throughput, or latency in it.
- Then prove relevance with exactly one strong candidate fact: a closely matched accomplishment, skill, or project, preferably with a real outcome or metric. Connect it directly to a stated need in the role. Do not paste a mini-resume, stack credentials, or use unsupported claims. Mention the current employer only when it strengthens this proof.
- State naturally that the candidate applied, naming the position with the supplied job title and company in either the hook or the next sentence. When the hook connects a company or role detail to the application, make the transition causal and conversational instead of abruptly appending "so I applied." For example: "Example's focus on reliable systems drew me to apply for its Backend Engineer position" or "That work made me especially interested in the Backend Engineer position I applied for at Example." These are suggestions, not templates; vary the language and use a separate sentence when it flows better. Never write "the exact role," "the exact [job title] role," or otherwise insert "exact" into this sentence. Do not spend words repeating the location unless it is genuinely relevant to the connection.
- REQUIRED: When JOB POSTING LINK below is present, the email is incomplete unless it includes that exact URL once on its own line near the end so the recipient can immediately identify the opening. This is mandatory, not optional. Introduce it briefly and naturally, such as "Role for context:". If JOB POSTING LINK is null, omit any job link rather than inventing one. Never shorten or change the supplied URL, and never link to an application form, job board, or another site.
- State the intention plainly, then end with exactly one concise, insightful question. Ground it in a supplied role, company, or recipient detail and connect it to the recipient's function so it feels uniquely worth answering. Prefer a question about a real priority, tradeoff, challenge, or decision behind the work—not a fact available in the posting or on the company website.
- Keep the question easy to answer in a reply and under 25 words. Do not ask multiple or compound questions. Do not ask for a coffee chat, call, meeting, referral, resume forwarding, application update, or interview. Those may follow later only if the employee's response supports them.
- Tailor the question to the recipient kind: recruiter = a non-administrative insight into the role's most important near-term priority; manager = a concrete team tradeoff, challenge, or success measure; peer = a specific aspect of how the advertised work happens in practice; leader = how a stated company or function priority shapes this team. Avoid generic questions such as "What qualities do you value?", "What is the day-to-day like?", or "Do you have any advice?"
- Explain why contacting this person's function makes sense; never claim they own the opening.
- Use materially different wording, subject, opening, candidate connection, and question for every recipient.
- Connect only to candidate facts supplied below. Never invent experience or company facts.
- Avoid bullets unless two very short proof points are both essential; prefer one strong proof point. Avoid exaggerated enthusiasm, generic praise, invented familiarity, sales language, and automation-like filler.
- Never use: "I hope this email finds you well", "I came across your profile", "I wanted to reach out", "I'm reaching out", "pick your brain", "perfect fit", "aligns perfectly", "deeply impressed", "resonates with me", "unique opportunity", "leverage", "synergy", "urgent", "act now", "limited time", "guaranteed", "buy now", or "make money".
- Subjects must not begin with Re: or Fwd:. Do not use emojis, all caps, repeated punctuation, citations, URLs other than the required exact JOB POSTING LINK when one is supplied, tracking language, attachments, or an unsubscribe paragraph.
- Match the writing samples' voice. End with the supplied signature.

JOB: {json.dumps({'title': job.get('title'), 'company': job.get('company'), 'location': job.get('location'), 'description': (job.get('full_description') or '')[:8000]})}
JOB POSTING LINK: {json.dumps(job_link)}
CANDIDATE NAME: {json.dumps(candidate_name)}
REQUIRED INTRODUCTION TEMPLATE: {json.dumps(introduction_template)}
CANDIDATE FACTS: {json.dumps(resume_facts)}
INTRODUCTION REQUIREMENTS: {json.dumps({'education': profile.get('education'), 'experience': profile.get('experience'), 'current_employer_from_writing_samples': introduction_employer or None})}
OFFICIAL COMPANY RESEARCH: {json.dumps(safe_research)}
RECIPIENTS: {json.dumps([{'person_id': r['person_id'], 'first_name': r.get('first_name'), 'name': r.get('name'), 'title': r.get('title'), 'kind': r.get('candidate_kind'), 'reason': r.get('relevance_reason')} for r in recipients])}
WRITING SAMPLES: {json.dumps(samples)}
SIGNATURE: {signature}
"""
    client = get_client()
    result = _parse_message_output(client.ask(prompt, temperature=0.3, max_tokens=3500))
    errors = _message_errors(
        job, recipients, result, signature, introduction_employer
    )
    if errors:
        invalid_ids = set(errors)
        invalid_recipients = [item for item in recipients if item["person_id"] in invalid_ids]
        invalid_drafts = [item for item in result if str(item.get("person_id")) in invalid_ids]
        repair_prompt = f"""{prompt}

Repair ONLY the recipients listed below. Return a JSON array containing exactly those recipients and no others.
VALIDATION FAILURES: {json.dumps(errors)}
INVALID RECIPIENTS: {json.dumps(invalid_recipients)}
INVALID DRAFTS: {json.dumps(invalid_drafts)}
"""
        repaired = _parse_message_output(
            client.ask(repair_prompt, temperature=0.2, max_tokens=2500)
        )
        repaired_by_id = {str(item.get("person_id")): item for item in repaired}
        result = [
            repaired_by_id.get(str(item.get("person_id")), item)
            if str(item.get("person_id")) in invalid_ids else item
            for item in result
        ]
        existing_ids = {str(item.get("person_id")) for item in result}
        result.extend(
            item for person_id, item in repaired_by_id.items() if person_id not in existing_ids
        )
        errors = _message_errors(
            job, recipients, result, signature, introduction_employer
        )
    by_id = {str(item.get("person_id")): item for item in result if isinstance(item, dict)}
    output = []
    for recipient in recipients:
        item = by_id.get(recipient["person_id"], {})
        subject = str(item.get("subject") or "").strip()
        body = str(item.get("body_text") or "").strip()
        validation_errors = errors.get(recipient["person_id"], [])
        if not validation_errors and (
            not subject or not body or len(subject) > 200 or len(body) > 4000
        ):
            continue
        output.append({
            **recipient,
            "subject": subject[:200],
            "body_text": body[:4000],
            "used_facts": item.get("used_facts") or [],
            "validation_errors": validation_errors,
        })
    return output


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
    if any(_is_suppressed(selected[item_id]["apollo_person_id"], selected[item_id]["email"], conn) for item_id in ids):
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
    if _is_suppressed(item["apollo_person_id"], item["email"], conn):
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


def redraft_batch(identifier: str, *, conn: sqlite3.Connection | None = None) -> dict:
    """Regenerate every editable message without searching or enriching people again."""
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
        "UPDATE outreach_recipients SET status = 'suppressed', updated_at = ? WHERE id = ?",
        (now, recipient_id),
    )
    _update_batch_after_send(recipient["batch_id"], conn)
    conn.commit()
    return get_batch(recipient["batch_id"], conn) or {}
