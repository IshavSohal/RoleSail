import json
import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import httpx
import pytest

from rolesail.dashboard_server import mark_job_applied, unmark_job_applied
from rolesail.database import init_db
from rolesail.outreach.apollo import ApolloClient, ApolloError
from rolesail.outreach.service import (
    _flowing_email_body,
    _generate_messages,
    _introduction_employer,
    _message_errors,
    _next_eligible_time,
    _outreach_job_link,
    _resolve_organization,
    approve_batch,
    cancel_batch,
    cancel_for_job,
    cancel_pending,
    clear_cancelled_batch,
    dispatch_due_outreach,
    enqueue_for_job,
    get_batch,
    prepare_batch,
    preview_batch_schedule,
    rank_people,
    recover_reapplied_batches,
    redraft_batch,
    restore_suppressed_recipient,
    schedule_settings,
    suppress_recipient,
)


class FakeApollo:
    def __init__(self):
        self.sent = []

    def search_organizations(self, name):
        return [{"id": "org-1", "name": name, "primary_domain": "example.com"}]

    def email_accounts(self):
        return [{"id": "mailbox-1", "email": "me@example.net"}]

    def search_people(self, **_kwargs):
        return [
            {"id": "manager", "name": "Morgan Manager", "first_name": "Morgan", "last_name": "Manager", "title": "Engineering Manager"},
            {"id": "leader", "name": "Lee Leader", "first_name": "Lee", "last_name": "Leader", "title": "Director of Engineering"},
            {"id": "recruiter", "name": "Rae Recruiter", "first_name": "Rae", "last_name": "Recruiter", "title": "Technical Recruiter"},
            {"id": "peer", "name": "Sam Senior", "first_name": "Sam", "last_name": "Senior", "title": "Senior Backend Engineer"},
            {"id": "peer-2", "name": "Pat Platform", "first_name": "Pat", "last_name": "Platform", "title": "Staff Platform Engineer"},
        ]

    def enrich_person(self, person_id):
        return {"email": f"{person_id}@example.com", "email_status": "verified"}

    def create_contact(self, recipient):
        return {"id": f"contact-{recipient['apollo_person_id']}"}

    def create_email_draft(self, **kwargs):
        return {"id": f"message-{kwargs['contact_id']}"}

    def send_email(self, message_id):
        self.sent.append(message_id)
        return {"id": message_id, "status": "scheduled"}

    def email_status(self, message_id):
        return {"id": message_id, "status": "completed", "completed_at": "2026-09-09T12:00:00+00:00"}


class FakeLLM:
    def ask(self, prompt, **_kwargs):
        recipients = json.loads(prompt.split("RECIPIENTS: ", 1)[1].split("\nWRITING SAMPLES:", 1)[0])
        return json.dumps([
            {
                "person_id": item["person_id"],
                "subject": "Backend Engineer application",
                "body_text": "Hi there,\n\nI applied for the Backend Engineer role and appreciated the focus on reliable systems. My background includes production Python services, and I would value your perspective on the engineering team.\n\nBest,\nTest User",
                "used_facts": ["The role focuses on reliable systems"],
            }
            for item in recipients
        ])


@pytest.fixture
def outreach_db(tmp_path):
    conn = init_db(tmp_path / "outreach.db")
    conn.execute(
        "INSERT INTO jobs (url, title, company, full_description, applied_at, apply_status) "
        "VALUES (?, ?, ?, ?, ?, 'applied')",
        (
            "https://jobs.example.com/backend",
            "Backend Engineer",
            "Example",
            "Build reliable Python services for a growing platform engineering team.",
            "2026-09-09T10:00:00+00:00",
        ),
    )
    conn.commit()
    return conn


def test_enqueue_is_disabled_by_default(outreach_db, monkeypatch):
    monkeypatch.delenv("OUTREACH_ENABLED", raising=False)
    assert enqueue_for_job("https://jobs.example.com/backend", outreach_db) is None


def test_reapplying_regenerates_cancelled_unsent_outreach(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    monkeypatch.setattr("rolesail.outreach.service.fetch_official_pages", lambda _domain: [])
    monkeypatch.setattr("rolesail.outreach.service.config.load_profile", dict)
    generations = []

    def generate(_job, recipients, _research, _profile):
        generations.append(len(recipients))
        return [
            {**recipient, "subject": f"Generation {len(generations)} for {recipient['person_id']}",
             "body_text": f"Hi {recipient['first_name']}, a test message.", "used_facts": ["Role detail"]}
            for recipient in recipients
        ]

    monkeypatch.setattr("rolesail.outreach.service._generate_messages", generate)
    url = "https://jobs.example.com/backend"
    first = enqueue_for_job(url, outreach_db)
    prepared = prepare_batch(first["id"], conn=outreach_db, apollo=FakeApollo())
    old_ids = {item["id"] for item in prepared["recipients"]}
    assert generations == [5]

    unmark_job_applied(url, outreach_db)
    assert outreach_db.execute(
        "SELECT status FROM outreach_batches WHERE id = ?", (first["id"],)
    ).fetchone()[0] == "cancelled"

    reapplied = mark_job_applied(url, outreach_db)
    assert reapplied["outreach"]["id"] == first["id"]
    assert reapplied["outreach"]["status"] == "queued"
    assert outreach_db.execute(
        "SELECT COUNT(*) FROM outreach_recipients WHERE batch_id = ?", (first["id"],)
    ).fetchone()[0] == 0

    fresh = prepare_batch(first["id"], conn=outreach_db, apollo=FakeApollo())
    assert fresh["status"] == "ready_for_review"
    assert generations == [5, 5]
    assert {item["id"] for item in fresh["recipients"]}.isdisjoint(old_ids)
    assert all(item["subject"].startswith("Generation 2") for item in fresh["recipients"])
    assert mark_job_applied(url, outreach_db)["outreach"]["status"] == "ready_for_review"


def test_reapplying_preserves_outreach_with_created_gmail_draft(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    url = "https://jobs.example.com/backend"
    batch = enqueue_for_job(url, outreach_db)
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, status, gmail_draft_id, gmail_account_email, created_at, updated_at) "
        "VALUES ('drafted-person', ?, 'person-1', 'drafted', 'gmail-draft-1', "
        "'me@example.com', ?, ?)",
        (batch["id"], "2026-09-10T10:00:00+00:00", "2026-09-10T10:00:00+00:00"),
    )
    outreach_db.commit()

    unmark_job_applied(url, outreach_db)
    reapplied = mark_job_applied(url, outreach_db)

    assert reapplied["outreach"]["status"] == "drafted"
    assert outreach_db.execute(
        "SELECT gmail_draft_id FROM outreach_recipients WHERE id = 'drafted-person'"
    ).fetchone()[0] == "gmail-draft-1"


def test_repeated_mark_does_not_restart_manually_cancelled_outreach(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    url = "https://jobs.example.com/backend"
    batch = enqueue_for_job(url, outreach_db)
    cancel_batch(batch["id"], outreach_db)

    repeated = mark_job_applied(url, outreach_db)

    assert repeated["outreach"]["status"] == "cancelled"
    unmark_job_applied(url, outreach_db)
    reapplied = mark_job_applied(url, outreach_db)
    assert reapplied["outreach"]["status"] == "queued"


def test_startup_recovers_job_reapplied_before_upgrade(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    url = "https://jobs.example.com/backend"
    batch = enqueue_for_job(url, outreach_db)
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, status, created_at, updated_at) "
        "VALUES ('old-recipient', ?, 'person-1', 'cancelled', ?, ?)",
        (batch["id"], "2026-09-10T10:00:00+00:00", "2026-09-10T10:00:00+00:00"),
    )
    outreach_db.execute(
        "UPDATE outreach_batches SET status = 'cancelled', updated_at = ? WHERE id = ?",
        ("2026-09-11T10:00:00+00:00", batch["id"]),
    )
    outreach_db.execute(
        "UPDATE jobs SET applied_at = ? WHERE url = ?",
        ("2026-09-12T10:00:00+00:00", url),
    )
    outreach_db.commit()

    recover_reapplied_batches(outreach_db)

    assert outreach_db.execute(
        "SELECT status FROM outreach_batches WHERE id = ?", (batch["id"],)
    ).fetchone()[0] == "queued"
    assert outreach_db.execute(
        "SELECT COUNT(*) FROM outreach_recipients WHERE batch_id = ?", (batch["id"],)
    ).fetchone()[0] == 0


def test_reapplication_during_preparation_discards_stale_results(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    monkeypatch.setattr("rolesail.outreach.service.fetch_official_pages", lambda _domain: [])
    monkeypatch.setattr("rolesail.outreach.service.config.load_profile", dict)
    url = "https://jobs.example.com/backend"
    batch = enqueue_for_job(url, outreach_db)
    generations = []

    def generate(_job, recipients, _research, _profile):
        generations.append(len(recipients))
        if len(generations) == 1:
            unmark_job_applied(url, outreach_db)
            mark_job_applied(url, outreach_db)
        return [
            {**recipient, "subject": f"Generation {len(generations)}", "body_text": "Test body",
             "used_facts": ["Role detail"]}
            for recipient in recipients
        ]

    monkeypatch.setattr("rolesail.outreach.service._generate_messages", generate)
    stale = prepare_batch(batch["id"], conn=outreach_db, apollo=FakeApollo())
    assert stale["status"] == "queued"
    assert stale["recipients"] == []

    fresh = prepare_batch(batch["id"], conn=outreach_db, apollo=FakeApollo())
    assert fresh["status"] == "ready_for_review"
    assert generations == [5, 5]
    assert all(item["subject"] == "Generation 2" for item in fresh["recipients"])


def test_prepare_review_and_send_are_idempotent(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    monkeypatch.setenv("APOLLO_EMAIL_ACCOUNT_ID", "mailbox-1")
    monkeypatch.setattr("rolesail.outreach.service.fetch_official_pages", lambda _domain: [])
    monkeypatch.setattr(
        "rolesail.outreach.service.config.load_profile",
        lambda: {
            "personal": {"full_name": "Test User"},
            "resume_facts": {"real_metrics": ["Built production Python services"]},
            "outreach": {"signature": "Test User", "writing_samples": ["One", "Two", "Three"]},
        },
    )
    monkeypatch.setattr(
        "rolesail.outreach.service._generate_messages",
        lambda _job, recipients, _research, _profile: [
            {
                **recipient,
                "subject": f"Backend Engineer question for {recipient['first_name']}",
                "body_text": (
                    f"Hi {recipient['first_name']},\n\nI'm Test, a Computer Science graduate from "
                    "Example University with experience in backend systems. A reviewed test message."
                    "\n\nTest User"
                ),
                "used_facts": ["The role focuses on reliable systems"],
            }
            for recipient in recipients
        ],
    )
    apollo = FakeApollo()

    first = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    second = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    assert first["id"] == second["id"]

    batch = prepare_batch(first["id"], conn=outreach_db, apollo=apollo)
    assert batch["status"] == "ready_for_review"
    assert len(batch["recipients"]) == 5

    with pytest.raises(ValueError, match="confirmation"):
        approve_batch(first["id"], batch["recipients"], confirmed=False, conn=outreach_db, apollo=apollo)
    assert apollo.sent == []

    selected = [
        {"id": item["id"], "subject": item["subject"], "body_text": item["body_text"]}
        for item in batch["recipients"][:2]
    ]
    scheduled = approve_batch(
        first["id"],
        selected,
        confirmed=True,
        conn=outreach_db,
        apollo=apollo,
        now=datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
    )
    assert scheduled["status"] == "scheduled"
    assert apollo.sent == []
    selected_rows = [item for item in scheduled["recipients"] if item["status"] == "scheduled"]
    assert [item["wave"] for item in selected_rows] == [1, 1]

    for item in sorted(selected_rows, key=lambda value: value["scheduled_for"]):
        sent = dispatch_due_outreach(
            conn=outreach_db,
            apollo=apollo,
            now=datetime.fromisoformat(item["scheduled_for"]),
        )
    assert sent["status"] == "completed"
    assert len(apollo.sent) == 2
    assert [item["status"] for item in sent["recipients"]].count("sent") == 2
    assert [item["status"] for item in sent["recipients"]].count("excluded") == 3


def test_approval_rechecks_edited_introduction(outreach_db, monkeypatch):
    monkeypatch.setenv("APOLLO_EMAIL_ACCOUNT_ID", "mailbox-1")
    monkeypatch.setattr(
        "rolesail.outreach.service.config.load_profile",
        lambda: {"personal": {"full_name": "Ishav Sohal"}},
    )
    outreach_db.execute(
        "INSERT INTO outreach_batches (id, job_url, status, created_at, updated_at) "
        "VALUES ('batch-1', 'https://jobs.example.com/backend', 'ready_for_review', 'now', 'now')"
    )
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, subject, body_text, status, created_at, updated_at) "
        "VALUES ('recipient-1', 'batch-1', 'person-1', 'Original', 'Original body', "
        "'needs_edit', 'now', 'now')"
    )
    outreach_db.commit()
    bad_intro = (
        "Hi Morgan,\n\nI'm Ishav, a recent Computer Science graduate from the University of Toronto "
        "with AI infrastructure and backend systems."
    )
    edit = {"id": "recipient-1", "subject": "Backend Engineer application", "body_text": bad_intro}

    with pytest.raises(ValueError, match="biographical sentence must begin exactly"):
        approve_batch("batch-1", [edit], confirmed=True, conn=outreach_db)

    row = outreach_db.execute(
        "SELECT status, body_text FROM outreach_recipients WHERE id = 'recipient-1'"
    ).fetchone()
    assert (row["status"], row["body_text"]) == ("needs_edit", "Original body")

    edit["body_text"] = bad_intro.replace("with AI infrastructure", "with experience in AI infrastructure")
    scheduled = approve_batch("batch-1", [edit], confirmed=True, conn=outreach_db)
    assert scheduled["status"] == "scheduled"


def test_rank_people_balances_hiring_circle():
    people = FakeApollo().search_people()
    ranked = rank_people(people, "Backend Engineer", "Python backend platform")
    kinds = [item["candidate_kind"] for item in ranked[:4]]
    assert kinds == ["manager", "leader", "recruiter", "peer"]


def test_clear_cancelled_batch_removes_batch_and_recipients(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, status, created_at, updated_at) "
        "VALUES ('recipient-1', ?, 'person-1', 'ready', ?, ?)",
        (batch["id"], "2026-09-10T10:00:00+00:00", "2026-09-10T10:00:00+00:00"),
    )
    outreach_db.commit()

    cancel_batch(batch["id"], outreach_db)
    result = clear_cancelled_batch(batch["id"], outreach_db)

    assert result["status"] == "cleared"
    assert outreach_db.execute(
        "SELECT 1 FROM outreach_batches WHERE id = ?", (batch["id"],)
    ).fetchone() is None
    assert outreach_db.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ?", (batch["id"],)
    ).fetchone() is None


def test_clear_cancelled_batch_rejects_active_batch(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)

    with pytest.raises(ValueError, match="Only a cancelled"):
        clear_cancelled_batch(batch["id"], outreach_db)


def test_never_contact_can_be_undone(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, email, email_status, subject, body_text, status, "
        "created_at, updated_at) VALUES "
        "('recipient-1', ?, 'person-1', 'Morgan@Example.com', 'verified', 'Subject', "
        "'Body', 'needs_edit', 'now', 'now')",
        (batch["id"],),
    )
    outreach_db.execute(
        "UPDATE outreach_batches SET status = 'ready_for_review' WHERE id = ?",
        (batch["id"],),
    )
    outreach_db.commit()

    suppressed = suppress_recipient("recipient-1", conn=outreach_db)

    assert suppressed["status"] == "completed"
    assert suppressed["recipients"][0]["status"] == "suppressed"
    assert {
        row[0]
        for row in outreach_db.execute(
            "SELECT key FROM outreach_suppressions ORDER BY key"
        ).fetchall()
    } == {"email:morgan@example.com", "person:person-1"}

    restored = restore_suppressed_recipient("recipient-1", conn=outreach_db)

    assert restored["status"] == "ready_for_review"
    assert restored["completed_at"] is None
    assert restored["recipients"][0]["status"] == "needs_edit"
    assert outreach_db.execute("SELECT 1 FROM outreach_suppressions").fetchone() is None


def test_redraft_batch_reuses_recipients_and_preserves_suppressed_contacts(
    outreach_db, monkeypatch
):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    monkeypatch.setattr(
        "rolesail.outreach.service.config.load_profile",
        lambda: {"personal": {"full_name": "Test User"}},
    )
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    now_text = "2026-09-10T14:00:00+00:00"
    outreach_db.executemany(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, first_name, last_name, title, email, email_status, "
        "relevance_score, relevance_reason, subject, body_text, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "recipient-1", batch["id"], "person-1", "Morgan", "Manager",
                "Engineering Manager", "morgan@example.com", 90, "Relevant manager",
                "Old subject", "Old body", "ready", now_text, now_text,
            ),
            (
                "recipient-2", batch["id"], "person-2", "Sam", "Senior",
                "Senior Backend Engineer", "sam@example.com", 70, "Relevant peer",
                "Suppressed subject", "Suppressed body", "suppressed", now_text, now_text,
            ),
        ],
    )
    outreach_db.execute(
        "UPDATE outreach_batches SET status = 'ready_for_review', company_research_json = ? "
        "WHERE id = ?",
        (json.dumps({"apollo": {"name": "Example"}}), batch["id"]),
    )
    outreach_db.commit()
    cancelled = cancel_batch(batch["id"], outreach_db)
    assert cancelled["status"] == "cancelled"
    assert next(
        item for item in cancelled["recipients"] if item["id"] == "recipient-1"
    )["status"] == "cancelled"
    captured = {}

    def generate(job, recipients, research, profile, redraft_feedback=""):
        captured.update(
            job=job,
            recipients=recipients,
            research=research,
            profile=profile,
            redraft_feedback=redraft_feedback,
        )
        return [{
            **recipients[0],
            "subject": "Reliable systems at Example",
            "body_text": "A newly generated body",
            "used_facts": ["The role focuses on reliable systems"],
            "validation_errors": [],
        }]

    monkeypatch.setattr("rolesail.outreach.service._generate_messages", generate)

    result = redraft_batch(
        batch["id"],
        feedback="Make the openings more direct and emphasize backend experience.",
        conn=outreach_db,
    )

    assert result["status"] == "ready_for_review"
    assert [item["person_id"] for item in captured["recipients"]] == ["person-1"]
    assert captured["recipients"][0]["candidate_kind"] == "manager"
    assert captured["research"] == {"apollo": {"name": "Example"}}
    assert captured["redraft_feedback"] == (
        "Make the openings more direct and emphasize backend experience."
    )
    by_id = {item["id"]: item for item in result["recipients"]}
    assert by_id["recipient-1"]["subject"] == "Reliable systems at Example"
    assert by_id["recipient-1"]["body_text"] == "A newly generated body"
    assert by_id["recipient-2"]["subject"] == "Suppressed subject"
    assert by_id["recipient-2"]["status"] == "suppressed"


def test_redraft_batch_refuses_after_gmail_drafting_has_started(
    outreach_db, monkeypatch
):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, email, email_status, status, gmail_draft_id, "
        "created_at, updated_at) VALUES "
        "('recipient-1', ?, 'person-1', 'morgan@example.com', 'verified', 'drafted', "
        "'gmail-draft-1', 'now', 'now')",
        (batch["id"],),
    )
    outreach_db.execute(
        "UPDATE outreach_batches SET status = 'drafted' WHERE id = ?", (batch["id"],)
    )
    outreach_db.commit()

    with pytest.raises(ValueError, match="Only an unsent"):
        redraft_batch(batch["id"], conn=outreach_db)


def test_failed_redraft_keeps_the_previous_messages(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    monkeypatch.setattr("rolesail.outreach.service.config.load_profile", dict)
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, first_name, title, email, email_status, "
        "subject, body_text, status, created_at, updated_at) VALUES "
        "('recipient-1', ?, 'person-1', 'Morgan', 'Engineering Manager', "
        "'morgan@example.com', 'verified', 'Keep subject', 'Keep body', 'ready', 'now', 'now')",
        (batch["id"],),
    )
    outreach_db.execute(
        "UPDATE outreach_batches SET status = 'ready_for_review' WHERE id = ?", (batch["id"],)
    )
    outreach_db.commit()
    monkeypatch.setattr(
        "rolesail.outreach.service._generate_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("LLM unavailable")),
    )

    with pytest.raises(RuntimeError, match="LLM unavailable"):
        redraft_batch(batch["id"], conn=outreach_db)

    result = get_batch(batch["id"], outreach_db)
    assert result["status"] == "ready_for_review"
    assert result["error"] == "LLM unavailable"
    assert result["recipients"][0]["subject"] == "Keep subject"
    assert result["recipients"][0]["body_text"] == "Keep body"


def test_redraft_feedback_is_limited(outreach_db):
    with pytest.raises(ValueError, match="500 characters"):
        redraft_batch("batch-1", feedback="x" * 501, conn=outreach_db)


def test_schedule_preview_uses_two_waves_and_skips_weekend(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    monkeypatch.setattr(
        "rolesail.outreach.service.config.load_profile",
        lambda: {"outreach": {"schedule": {"timezone": "America/Toronto"}}},
    )
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    now_text = "2026-09-11T20:00:00+00:00"
    for index in range(5):
        outreach_db.execute(
            "INSERT INTO outreach_recipients "
            "(id, batch_id, apollo_person_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'ready', ?, ?)",
            (f"recipient-{index}", batch["id"], f"person-{index}", now_text, now_text),
        )
    outreach_db.execute(
        "UPDATE outreach_batches SET status = 'ready_for_review' WHERE id = ?", (batch["id"],)
    )
    outreach_db.commit()

    preview = preview_batch_schedule(
        batch["id"],
        [f"recipient-{index}" for index in range(5)],
        conn=outreach_db,
        now=datetime(2026, 9, 11, 21, 0, tzinfo=UTC),
    )

    assert [item["wave"] for item in preview] == [1, 1, 2, 2, 2]
    local_dates = [
        datetime.fromisoformat(item["scheduled_for"]).astimezone(
            ZoneInfo("America/Toronto")
        ).date().isoformat()
        for item in preview
    ]
    assert local_dates[:2] == ["2026-09-14", "2026-09-14"]
    assert local_dates[2:] == ["2026-09-16", "2026-09-16", "2026-09-16"]
    times = [datetime.fromisoformat(item["scheduled_for"]) for item in preview]
    assert all((right - left).total_seconds() >= 600 for left, right in pairwise(times[:2]))


def test_schedule_preview_respects_daily_limit(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    monkeypatch.setattr(
        "rolesail.outreach.service.config.load_profile",
        lambda: {"outreach": {"schedule": {"timezone": "America/Toronto", "daily_limit": 15}}},
    )
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, status, created_at, updated_at) "
        "VALUES ('new-recipient', ?, 'new-person', 'ready', ?, ?)",
        (batch["id"], "2026-09-14T16:00:00+00:00", "2026-09-14T16:00:00+00:00"),
    )
    for index in range(15):
        sent_at = datetime(2026, 9, 14, 13, 0, tzinfo=UTC) + timedelta(minutes=10 * index)
        outreach_db.execute(
            "INSERT INTO outreach_recipients "
            "(id, batch_id, apollo_person_id, status, sent_at, created_at, updated_at) "
            "VALUES (?, 'another-batch', ?, 'sent', ?, ?, ?)",
            (f"sent-{index}", f"sent-person-{index}", sent_at.isoformat(), sent_at.isoformat(), sent_at.isoformat()),
        )
    outreach_db.execute(
        "UPDATE outreach_batches SET status = 'ready_for_review' WHERE id = ?", (batch["id"],)
    )
    outreach_db.commit()

    preview = preview_batch_schedule(
        batch["id"],
        ["new-recipient"],
        conn=outreach_db,
        now=datetime(2026, 9, 14, 16, 0, tzinfo=UTC),
    )

    local = datetime.fromisoformat(preview[0]["scheduled_for"]).astimezone(
        ZoneInfo("America/Toronto")
    )
    assert (local.date().isoformat(), local.hour, local.minute) == ("2026-09-15", 9, 0)


def test_schedule_preserves_local_hour_across_daylight_saving_change():
    settings = schedule_settings({
        "outreach": {"schedule": {"timezone": "America/Toronto"}}
    })

    eligible = _next_eligible_time(
        datetime(2026, 11, 1, 15, 0, tzinfo=UTC),  # Sunday after the fall-back.
        settings,
    )

    local = eligible.astimezone(ZoneInfo("America/Toronto"))
    assert (local.date().isoformat(), local.hour, eligible.hour) == ("2026-11-02", 9, 14)


def test_cancel_pending_marks_partially_sent_batch_stopped(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    now_text = "2026-09-10T14:00:00+00:00"
    for recipient_id, status in (("sent-one", "sent"), ("pending-one", "scheduled")):
        outreach_db.execute(
            "INSERT INTO outreach_recipients "
            "(id, batch_id, apollo_person_id, status, scheduled_for, sent_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                recipient_id,
                batch["id"],
                recipient_id,
                status,
                now_text,
                now_text if status == "sent" else None,
                now_text,
                now_text,
            ),
        )
    outreach_db.execute(
        "UPDATE outreach_batches SET status = 'scheduled' WHERE id = ?", (batch["id"],)
    )
    outreach_db.commit()

    stopped = cancel_pending(batch["id"], outreach_db)

    assert stopped["status"] == "stopped"
    assert {item["status"] for item in stopped["recipients"]} == {"sent", "cancelled"}


def test_dispatcher_does_not_send_outside_business_window(outreach_db, monkeypatch):
    monkeypatch.setenv("APOLLO_EMAIL_ACCOUNT_ID", "mailbox-1")
    monkeypatch.setattr(
        "rolesail.outreach.service.config.load_profile",
        lambda: {"outreach": {"schedule": {"timezone": "America/Toronto"}}},
    )
    batch_id = "scheduled-batch"
    due = "2026-09-12T14:00:00+00:00"  # Saturday morning in Toronto.
    outreach_db.execute(
        "INSERT INTO outreach_batches (id, job_url, status, created_at, updated_at) "
        "VALUES (?, 'https://jobs.example.com/backend', 'scheduled', ?, ?)",
        (batch_id, due, due),
    )
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, email, subject, body_text, status, scheduled_for, "
        "created_at, updated_at) VALUES ('weekend-recipient', ?, 'person-weekend', "
        "'person@example.com', 'Subject', 'Body', 'scheduled', ?, ?, ?)",
        (batch_id, due, due, due),
    )
    outreach_db.commit()
    apollo = FakeApollo()

    result = dispatch_due_outreach(
        conn=outreach_db,
        apollo=apollo,
        now=datetime.fromisoformat(due),
    )

    recipient = result["recipients"][0]
    local = datetime.fromisoformat(recipient["scheduled_for"]).astimezone(
        ZoneInfo("America/Toronto")
    )
    assert recipient["status"] == "scheduled"
    assert (local.weekday(), local.hour, local.minute) == (0, 9, 0)
    assert apollo.sent == []


def test_transient_dispatch_failure_is_rescheduled(outreach_db, monkeypatch):
    class RateLimitedApollo(FakeApollo):
        def send_email(self, message_id):
            raise ApolloError("rate limited", status_code=429)

    monkeypatch.setenv("APOLLO_EMAIL_ACCOUNT_ID", "mailbox-1")
    monkeypatch.setattr(
        "rolesail.outreach.service.config.load_profile",
        lambda: {"outreach": {"schedule": {"timezone": "America/Toronto"}}},
    )
    due = "2026-09-14T14:00:00+00:00"
    outreach_db.execute(
        "INSERT INTO outreach_batches (id, job_url, status, created_at, updated_at) "
        "VALUES ('retry-batch', 'https://jobs.example.com/backend', 'scheduled', ?, ?)",
        (due, due),
    )
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, email, subject, body_text, status, scheduled_for, "
        "created_at, updated_at) VALUES ('retry-recipient', 'retry-batch', 'retry-person', "
        "'retry@example.com', 'Subject', 'Body', 'scheduled', ?, ?, ?)",
        (due, due, due),
    )
    outreach_db.commit()

    result = dispatch_due_outreach(
        conn=outreach_db,
        apollo=RateLimitedApollo(),
        now=datetime.fromisoformat(due),
    )

    recipient = result["recipients"][0]
    assert recipient["status"] == "scheduled"
    assert recipient["attempt_count"] == 1
    assert datetime.fromisoformat(recipient["scheduled_for"]) >= datetime.fromisoformat(due) + timedelta(minutes=5)


def test_unapplying_job_cancels_remaining_schedule(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    due = "2026-09-14T14:00:00+00:00"
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, status, scheduled_for, created_at, updated_at) "
        "VALUES ('pending', ?, 'person', 'scheduled', ?, ?, ?)",
        (batch["id"], due, due, due),
    )
    outreach_db.execute(
        "UPDATE outreach_batches SET status = 'scheduled' WHERE id = ?", (batch["id"],)
    )
    outreach_db.commit()

    cancel_for_job("https://jobs.example.com/backend", outreach_db)

    status = outreach_db.execute(
        "SELECT status FROM outreach_recipients WHERE id = 'pending'"
    ).fetchone()[0]
    assert status == "cancelled"


def test_existing_outreach_is_displayed_without_hard_wraps(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, body_text, status, created_at, updated_at) "
        "VALUES ('wrapped', ?, 'person-1', ?, 'ready', 'now', 'now')",
        (batch["id"], "Hi Morgan,\n\nThis was manually\nwrapped."),
    )
    outreach_db.commit()

    loaded = get_batch(batch["id"], outreach_db)

    assert loaded["recipients"][0]["body_text"] == "Hi Morgan,\n\nThis was manually wrapped."


def test_outreach_tables_are_available(outreach_db):
    names = {row[0] for row in outreach_db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"outreach_batches", "outreach_recipients", "company_research", "outreach_suppressions"} <= names
    columns = {row[1] for row in outreach_db.execute("PRAGMA table_info(outreach_recipients)")}
    assert {
        "scheduled_for",
        "wave",
        "attempt_count",
        "last_attempt_at",
        "status_before_suppression",
    } <= columns


def test_existing_outreach_database_is_forward_migrated(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE outreach_recipients ("
        "id TEXT PRIMARY KEY, batch_id TEXT, apollo_person_id TEXT, apollo_contact_id TEXT, "
        "apollo_message_id TEXT, first_name TEXT, last_name TEXT, title TEXT, linkedin_url TEXT, "
        "email TEXT, email_status TEXT, relevance_score INTEGER, relevance_reason TEXT, subject TEXT, "
        "body_text TEXT, source_facts_json TEXT, status TEXT, error TEXT, sent_at TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    legacy.commit()
    legacy.close()

    migrated = init_db(path)

    columns = {row[1] for row in migrated.execute("PRAGMA table_info(outreach_recipients)")}
    assert {
        "scheduled_for",
        "wave",
        "attempt_count",
        "last_attempt_at",
        "status_before_suppression",
    } <= columns
