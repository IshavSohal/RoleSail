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
    schedule_settings,
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

    def generate(job, recipients, research, profile):
        captured.update(
            job=job, recipients=recipients, research=research, profile=profile
        )
        return [{
            **recipients[0],
            "subject": "Reliable systems at Example",
            "body_text": "A newly generated body",
            "used_facts": ["The role focuses on reliable systems"],
            "validation_errors": [],
        }]

    monkeypatch.setattr("rolesail.outreach.service._generate_messages", generate)

    result = redraft_batch(batch["id"], conn=outreach_db)

    assert result["status"] == "ready_for_review"
    assert [item["person_id"] for item in captured["recipients"]] == ["person-1"]
    assert captured["recipients"][0]["candidate_kind"] == "manager"
    assert captured["research"] == {"apollo": {"name": "Example"}}
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


def test_message_validation_rejects_generic_spammy_draft():
    errors = _message_errors(
        {"title": "Backend Engineer", "location": "Toronto, Ontario"},
        [{"person_id": "person-1", "first_name": "Morgan"}],
        [{
            "person_id": "person-1",
            "subject": "RE: URGENT OPPORTUNITY!!!",
            "body_text": "Hi there, I hope this email finds you well. I wanted to reach out about a perfect fit.",
            "used_facts": [],
        }],
        "Test User",
    )

    failures = " ".join(errors["person-1"])
    assert "reply or forward" in failures
    assert "prohibited phrase" in failures
    assert "70-120" in failures
    assert "job title" in failures


def test_message_validation_allows_a_hook_before_the_short_biography():
    base = {
        "person_id": "person-1",
        "subject": "Backend Engineer application",
        "used_facts": ["The role focuses on reliable systems"],
    }
    job = {"title": "Backend Engineer", "location": "Toronto, Ontario"}
    recipient = [{"person_id": "person-1", "first_name": "Morgan"}]
    hook = (
        "Hi Morgan,\n\nThe team's focus on reliable services stood out after I applied for "
        "the Backend Engineer role. "
    )
    remainder = (
        " My production Python work maps closely to that need. Where does the team feel the "
        "sharpest tradeoff between delivery speed and service reliability? No worries if you're "
        "not the right person to ask."
        "\n\nThanks,\nIshav Sohal"
    )
    bad = {
        **base,
        "body_text": (
            hook + "I'm Ishav, and I've worked on backend and distributed-systems projects "
            "where I had to think carefully about scalability, caching, fault tolerance, and latency."
            + remainder
        ),
    }
    good = {
        **base,
        "body_text": (
            hook + "I'm Ishav, a recent Computer Science graduate from the University of Toronto "
            "with experience in backend systems and AI infrastructure."
            + remainder
        ),
    }
    missing_experience = {
        **good,
        "body_text": good["body_text"].replace(
            "with experience in backend systems and AI infrastructure",
            "with AI infrastructure and backend systems",
        ),
    }

    bad_failures = _message_errors(
        job, recipient, [bad], "Ishav Sohal", "FGF Brands"
    )["person-1"]
    good_failures = _message_errors(
        job, recipient, [good], "Ishav Sohal", "FGF Brands"
    ).get("person-1", [])
    missing_experience_failures = _message_errors(
        job, recipient, [missing_experience], "Ishav Sohal", "FGF Brands"
    )["person-1"]

    assert "biographical sentence does not establish the candidate's education" in bad_failures
    assert any("biographical sentence must begin exactly" in failure for failure in bad_failures)
    assert not any("biographical sentence" in failure for failure in good_failures)
    unnatural = {
        **good,
        "body_text": good["body_text"].replace(
            "applied for the Backend Engineer role",
            "applied for the exact Backend Engineer role",
        ),
    }
    assert "message unnaturally describes the opening as an exact role" in _message_errors(
        job, recipient, [unnatural], "Ishav Sohal", "FGF Brands"
    )["person-1"]
    meeting_ask = {
        **good,
        "body_text": good["body_text"].replace(
            "Where does the team feel the sharpest tradeoff between delivery speed and service reliability?",
            "Would you be open to a 15-minute chat?",
        ),
    }
    assert "first-touch message asks for a chat, call, or meeting" in _message_errors(
        job, recipient, [meeting_ask], "Ishav Sohal", "FGF Brands"
    )["person-1"]
    assert any(
        "biographical sentence must begin exactly" in failure
        for failure in missing_experience_failures
    )


def test_current_employer_is_inferred_from_explicit_writing_sample():
    profile = {"experience": {"current_title": "AI Solutions Engineer"}}
    samples = [(
        "I'm a recent CS graduate from UofT, and I'm currently working as an "
        "AI Solutions Engineer at FGF Brands."
    )]

    assert _introduction_employer(profile, samples) == "FGF Brands"


def test_email_body_removes_hard_wraps_but_keeps_paragraphs():
    body = (
        "Hi Morgan,\r\n"
        "\r\n"
        "I applied for the Backend Engineer role and have experience\r\n"
        "building reliable Python services. This should flow naturally.\r\n"
        "\r\n"
        "Best,\r\n"
        "Test User"
    )

    assert _flowing_email_body(body) == (
        "Hi Morgan,\n\n"
        "I applied for the Backend Engineer role and have experience building reliable "
        "Python services. This should flow naturally.\n\n"
        "Best,\nTest User"
    )

    assert _flowing_email_body("Thanks, Ishav Sohal") == "Thanks,\nIshav Sohal"


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


def test_message_validation_allows_only_the_saved_posting_link():
    job = {
        "url": "https://jobs.example.com/backend",
        "title": "Backend Engineer",
        "location": "Toronto, Ontario",
    }
    body = (
        "Hi Morgan,\n\nI'm Test, a recent Computer Science graduate from Example University. "
        "I applied for the Backend Engineer role in Toronto, Ontario. "
        "I've worked on production Python services, so the role's focus on reliable systems stood out to me. "
        "I understand you work with the engineering team, and I'd value your perspective on the day-to-day work. "
        "If you have a moment, what kinds of problems would someone in this role tackle early on? "
        "I'm especially interested in how the team handles failures and keeps services maintainable as they grow. "
        "No worries if you're not the right person to ask.\n\n"
        "https://jobs.example.com/backend\n\nBest,\nTest User"
    )
    message = {
        "person_id": "person-1",
        "subject": "Backend Engineer application",
        "body_text": body,
        "used_facts": ["The role focuses on reliable systems"],
    }
    recipients = [{"person_id": "person-1", "first_name": "Morgan"}]

    assert _message_errors(job, recipients, [message], "Test User") == {}
    message["body_text"] = body.replace("\nhttps://jobs.example.com/backend", "")
    assert "message does not include the exact job-posting link" in _message_errors(
        job, recipients, [message], "Test User"
    )["person-1"]
    message["body_text"] = body
    message["body_text"] = body + "\nhttps://unrelated.example.com/track"
    assert "message contains a URL" in _message_errors(job, recipients, [message], "Test User")["person-1"]


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/123",
    "https://jobs.example.com/backend?utm_source=email",
    "https://jobs.example.com/backend/apply",
    "http://jobs.example.com/backend",
])
def test_outreach_job_link_omits_unsuitable_urls(url):
    assert _outreach_job_link({"url": url}) is None


def test_generation_prompt_enforces_voice_and_human_wording(monkeypatch):
    captured = {}

    class CapturingLLM:
        def ask(self, prompt, **_kwargs):
            captured["prompt"] = prompt
            return json.dumps([{
                "person_id": "person-1",
                "subject": "Backend Engineer team question",
                "body_text": (
                    "Hi Morgan,\n\nI recently applied for the Backend Engineer role in Toronto, Ontario. "
                    "The focus on reliable Python services caught my attention because I have built and maintained "
                    "production Python systems where clear failure handling mattered. Your work as an engineering "
                    "manager seems close enough to the team for a practical view without assuming you are involved "
                    "in hiring. If you have a moment, I would appreciate hearing which habits help new engineers "
                    "contribute well on this kind of platform team. No worries if you are not the right person to "
                    "ask.\n\nThanks,\nTest User"
                ),
                "used_facts": ["The role focuses on reliable Python services"],
            }])

    monkeypatch.setattr("rolesail.llm.get_client", lambda: CapturingLLM())
    messages = _generate_messages(
        {
            "url": "https://jobs.example.com/backend",
            "title": "Backend Engineer",
            "company": "Example",
            "location": "Toronto, Ontario",
            "full_description": "Build reliable Python services.",
        },
        [{
            "person_id": "person-1",
            "first_name": "Morgan",
            "name": "Morgan Manager",
            "title": "Engineering Manager",
            "candidate_kind": "manager",
            "relevance_reason": "Likely manager for the role's function",
        }],
        {"apollo": {"name": "Example"}, "official_pages": []},
        {
            "personal": {"full_name": "Test User"},
            "resume_facts": {"real_metrics": ["Built production Python systems"]},
            "outreach": {
                "signature": "Test User",
                "writing_samples": ["Sample one", "Sample two", "Sample three"],
            },
        },
    )

    assert len(messages) == 1
    assert "Silently infer the candidate's recurring formality" in captured["prompt"]
    assert "never hard-wrap prose or insert a newline within a paragraph" in captured["prompt"]
    assert "Format the sign-off on exactly two lines" in captured["prompt"]
    assert 'CANDIDATE NAME: "Test User"' in captured["prompt"]
    assert "goal is to earn a thoughtful reply" in captured["prompt"]
    assert "The first prose sentence after the greeting is the hook" in captured["prompt"]
    assert "Do not open with the candidate's biography" in captured["prompt"]
    assert "Do not paste a mini-resume" in captured["prompt"]
    assert "exactly one concise, insightful question" in captured["prompt"]
    assert "Do not ask for a coffee chat, call, meeting, referral" in captured["prompt"]
    assert "real priority, tradeoff, challenge, or decision" in captured["prompt"]
    assert 'Avoid generic questions such as "What qualities do you value?"' in captured["prompt"]
    assert "2-8 word subject" in captured["prompt"]
    assert "REQUIRED INTRODUCTION TEMPLATE" in captured["prompt"]
    assert "scalability, caching, fault tolerance, throughput, or latency" in captured["prompt"]
    assert "State naturally that the candidate applied" in captured["prompt"]
    assert "make the transition causal and conversational" in captured["prompt"]
    assert 'instead of abruptly appending "so I applied."' in captured["prompt"]
    assert "These are suggestions, not templates" in captured["prompt"]
    assert 'Never write "the exact role,"' in captured["prompt"]
    assert 'JOB POSTING LINK: "https://jobs.example.com/backend"' in captured["prompt"]
    assert "the email is incomplete unless it includes that exact URL once" in captured["prompt"]
    assert "This is mandatory, not optional" in captured["prompt"]
    assert "URLs other than the required exact JOB POSTING LINK" in captured["prompt"]
    assert "I hope this email finds you well" in captured["prompt"]
    assert "materially different wording" in captured["prompt"]


def test_rank_people_prefers_same_job_location_within_candidate_kind():
    people = [
        {
            "id": "remote-manager",
            "name": "Very Relevant Remote Manager",
            "title": "Backend Platform Engineering Manager",
            "city": "Vancouver",
            "state": "British Columbia",
            "country": "Canada",
        },
        {
            "id": "local-manager",
            "name": "Local Manager",
            "title": "Engineering Manager",
            "city": "Toronto",
            "state": "Ontario",
            "country": "Canada",
        },
    ]

    ranked = rank_people(
        people,
        "Backend Engineer",
        "Build a Python backend platform",
        "Toronto, Ontario, Canada",
    )

    assert ranked[0]["person_id"] == "local-manager"
    assert "Same city" in ranked[0]["relevance_reason"]


def test_rank_people_does_not_bias_location_for_remote_only_job():
    people = [
        {
            "id": "best-title",
            "name": "Best Title",
            "title": "Backend Platform Engineering Manager",
            "city": "Vancouver",
        },
        {
            "id": "other-title",
            "name": "Other Title",
            "title": "Engineering Manager",
            "city": "Toronto",
        },
    ]

    ranked = rank_people(people, "Backend Engineer", "Python backend platform", "Remote")

    assert ranked[0]["person_id"] == "best-title"


def test_apollo_people_search_accepts_location_filters():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"people": []})

    client = ApolloClient(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client.search_people(
        organization_id="org-1",
        domain="example.com",
        locations=["Toronto, Ontario, Canada"],
    )

    assert captured["person_locations"] == ["Toronto, Ontario, Canada"]


def test_organization_resolution_uses_exact_workday_tenant_fallback():
    class OrganizationsApollo:
        def __init__(self):
            self.queries = []

        def search_organizations(self, name):
            self.queries.append(name)
            if name.lower() == "td":
                return [{"id": "td", "name": "TD", "primary_domain": "td.com"}]
            return [{"id": "wrong", "name": "TD Canada Trust Bank", "primary_domain": None}]

    apollo = OrganizationsApollo()
    organization, domain = _resolve_organization(
        {
            "company": "TD Bank",
            "url": "https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/role",
        },
        apollo,
    )

    assert apollo.queries == ["TD Bank", "td"]
    assert organization["id"] == "td"
    assert domain == "td.com"


def test_apollo_client_redacts_key_and_labels_auth_error():
    def handler(request):
        assert request.headers["x-api-key"] == "secret"
        return httpx.Response(401, json={"error": "bad key"})

    client = ApolloClient(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    with pytest.raises(ApolloError, match="authentication failed") as exc:
        client.health()
    assert "secret" not in str(exc.value)


def test_apollo_email_draft_uses_current_flat_payload():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"emailer_message": {"id": "message-1"}})

    client = ApolloClient(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    draft = client.create_email_draft(
        contact_id="contact-1",
        subject="Hello",
        body_html="<p>Hello</p>",
        email_account_id="mailbox-1",
    )
    assert draft["id"] == "message-1"
    assert captured == {
        "contact_id": "contact-1",
        "subject": "Hello",
        "body_html": "<p>Hello</p>",
    }


def test_outreach_tables_are_available(outreach_db):
    names = {row[0] for row in outreach_db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"outreach_batches", "outreach_recipients", "company_research", "outreach_suppressions"} <= names
    columns = {row[1] for row in outreach_db.execute("PRAGMA table_info(outreach_recipients)")}
    assert {"scheduled_for", "wave", "attempt_count", "last_attempt_at"} <= columns


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
    assert {"scheduled_for", "wave", "attempt_count", "last_attempt_at"} <= columns
