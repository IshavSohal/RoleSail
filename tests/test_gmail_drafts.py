"""Gmail draft creation is opt-in and never invokes a send endpoint."""

import base64
import json
import stat
from email import policy
from email.parser import BytesParser

import pytest

from rolesail.database import init_db
from rolesail.outreach import gmail as gmail_module
from rolesail.outreach.gmail import GmailDraftClient
from rolesail.outreach.service import create_gmail_drafts, reset_uncertain_gmail_draft, retry_batch


class FakeGmail:
    email = "personal@gmail.com"

    def __init__(self, failure=None):
        self.created = []
        self.failure = failure

    def create_draft(self, *, recipient_email, subject, body_text):
        if self.failure and recipient_email == "first@example.com":
            raise self.failure
        self.created.append((recipient_email, subject, body_text))
        return f"draft-{len(self.created)}"


@pytest.fixture
def batch_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "rolesail.outreach.service.config.load_profile",
        lambda: {"personal": {"full_name": "Test User"}},
    )
    conn = init_db(tmp_path / "gmail.db")
    conn.execute(
        "INSERT INTO jobs (url, title, company, applied_at) VALUES (?, ?, ?, ?)",
        ("https://jobs.example.com/role", "Engineer", "Example", "2026-09-12T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO outreach_batches (id, job_url, status, created_at, updated_at) "
        "VALUES ('batch-1', 'https://jobs.example.com/role', 'ready_for_review', 'now', 'now')"
    )
    for index, name in enumerate(("first", "second"), start=1):
        conn.execute(
            "INSERT INTO outreach_recipients "
            "(id, batch_id, apollo_person_id, email, email_status, subject, body_text, status, created_at, updated_at) "
            "VALUES (?, 'batch-1', ?, ?, 'verified', 'Original', 'Original body', 'ready', 'now', 'now')",
            (f"recipient-{index}", f"person-{index}", f"{name}@example.com"),
        )
    conn.commit()
    return conn


def _edits(*ids):
    return [
        {
            "id": item_id,
            "subject": f"Engineer application {item_id}",
            "body_text": "Hi Morgan,\n\nI'm Test, a Computer Science graduate from Example University "
                         "with experience in backend systems.",
        }
        for item_id in ids
    ]


def test_creates_unsent_gmail_drafts_and_is_idempotent(batch_db):
    gmail = FakeGmail()
    result = create_gmail_drafts(
        "batch-1", _edits("recipient-1", "recipient-2"),
        confirmed_account="personal@gmail.com", conn=batch_db, gmail=gmail,
    )
    assert result["status"] == "drafted"
    assert {item["status"] for item in result["recipients"]} == {"drafted"}
    assert {item["id"]: item["gmail_draft_id"] for item in result["recipients"]} == {
        "recipient-1": "draft-1", "recipient-2": "draft-2"
    }
    assert all(item["scheduled_for"] is None and item["sent_at"] is None for item in result["recipients"])
    assert len(gmail.created) == 2

    repeated = create_gmail_drafts(
        "batch-1", _edits("recipient-1", "recipient-2"),
        confirmed_account="personal@gmail.com", conn=batch_db, gmail=gmail,
    )
    assert repeated["status"] == "drafted"
    assert len(gmail.created) == 2


def test_gmail_draft_creation_removes_manual_hard_wraps(batch_db):
    gmail = FakeGmail()
    edits = [{
        "id": "recipient-1",
        "subject": "Engineer application",
        "body_text": "Hi Morgan,\n\nI'm Test, a Computer Science graduate from Example University "
                     "with experience\nin backend systems. I applied for the Engineer role.",
    }]

    result = create_gmail_drafts(
        "batch-1", edits, confirmed_account="personal@gmail.com", conn=batch_db, gmail=gmail,
    )

    expected = ("Hi Morgan,\n\nI'm Test, a Computer Science graduate from Example University "
                "with experience in backend systems. I applied for the Engineer role.")
    assert gmail.created[0][2] == expected
    recipient = next(item for item in result["recipients"] if item["id"] == "recipient-1")
    assert recipient["body_text"] == expected


def test_gmail_draft_creation_rechecks_edited_introduction(batch_db, monkeypatch):
    monkeypatch.setattr(
        "rolesail.outreach.service.config.load_profile",
        lambda: {"personal": {"full_name": "Ishav Sohal"}},
    )
    gmail = FakeGmail()
    bad_intro = (
        "Hi Morgan,\n\nI'm Ishav, a recent Computer Science graduate from the University of Toronto "
        "with AI infrastructure and backend systems."
    )
    edit = {"id": "recipient-1", "subject": "Engineer application", "body_text": bad_intro}

    with pytest.raises(ValueError, match="biographical sentence must begin exactly"):
        create_gmail_drafts(
            "batch-1", [edit], confirmed_account="personal@gmail.com", conn=batch_db, gmail=gmail,
        )

    assert gmail.created == []
    row = batch_db.execute(
        "SELECT status, body_text FROM outreach_recipients WHERE id = 'recipient-1'"
    ).fetchone()
    assert (row["status"], row["body_text"]) == ("ready", "Original body")

    edit["body_text"] = bad_intro.replace("with AI infrastructure", "with experience in AI infrastructure")
    result = create_gmail_drafts(
        "batch-1", [edit], confirmed_account="personal@gmail.com", conn=batch_db, gmail=gmail,
    )
    assert result["status"] == "drafted"
    assert len(gmail.created) == 1


def test_account_confirmation_and_legacy_schedule_guard(batch_db):
    gmail = FakeGmail()
    with pytest.raises(ValueError, match="connected Gmail address"):
        create_gmail_drafts(
            "batch-1", _edits("recipient-1"),
            confirmed_account="work@example.com", conn=batch_db, gmail=gmail,
        )
    assert not gmail.created
    batch_db.execute(
        "UPDATE outreach_recipients SET status = 'scheduled', scheduled_for = '2026-09-14T14:00:00+00:00' "
        "WHERE id = 'recipient-2'"
    )
    batch_db.commit()
    with pytest.raises(ValueError, match="Cancel the remaining Apollo sends"):
        create_gmail_drafts(
            "batch-1", _edits("recipient-1"),
            confirmed_account="personal@gmail.com", conn=batch_db, gmail=gmail,
        )
    assert not gmail.created


def test_failed_gmail_draft_cannot_fall_back_to_apollo_sending(batch_db):
    class Rejected(Exception):
        resp = type("Response", (), {"status": 403})()

    result = create_gmail_drafts(
        "batch-1", _edits("recipient-1"),
        confirmed_account="personal@gmail.com", conn=batch_db, gmail=FakeGmail(failure=Rejected("no access")),
    )
    assert result["status"] == "failed"
    assert {item["id"]: item["status"] for item in result["recipients"]} == {
        "recipient-1": "failed", "recipient-2": "excluded"
    }
    with pytest.raises(ValueError, match="Gmail draft creation"):
        retry_batch("batch-1", conn=batch_db)


def test_retry_must_use_same_gmail_account(batch_db):
    batch_db.execute(
        "UPDATE outreach_recipients SET gmail_account_email = 'personal@gmail.com' WHERE id = 'recipient-2'"
    )
    batch_db.commit()
    other_gmail = FakeGmail()
    other_gmail.email = "other@gmail.com"
    with pytest.raises(ValueError, match="another account"):
        create_gmail_drafts(
            "batch-1", _edits("recipient-1"),
            confirmed_account="other@gmail.com", conn=batch_db, gmail=other_gmail,
        )
    assert not other_gmail.created


def test_ambiguous_failure_is_not_retried_automatically(batch_db):
    gmail = FakeGmail(failure=TimeoutError("connection lost"))
    result = create_gmail_drafts(
        "batch-1", _edits("recipient-1", "recipient-2"),
        confirmed_account="personal@gmail.com", conn=batch_db, gmail=gmail,
    )
    assert result["status"] == "drafting"
    statuses = {item["id"]: item["status"] for item in result["recipients"]}
    assert statuses == {"recipient-1": "drafting", "recipient-2": "drafted"}
    with pytest.raises(ValueError, match="not ready"):
        create_gmail_drafts(
            "batch-1", _edits("recipient-1"),
            confirmed_account="personal@gmail.com", conn=batch_db, gmail=gmail,
        )
    assert len(gmail.created) == 1

    batch_db.execute("UPDATE outreach_recipients SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id = 'recipient-1'")
    batch_db.commit()
    with pytest.raises(ValueError, match="Confirm"):
        reset_uncertain_gmail_draft("recipient-1", confirmed_no_draft=False, conn=batch_db)
    reset = reset_uncertain_gmail_draft("recipient-1", confirmed_no_draft=True, conn=batch_db)
    assert reset["status"] == "partial_failed"
    gmail.failure = None
    retried = create_gmail_drafts(
        "batch-1", _edits("recipient-1"),
        confirmed_account="personal@gmail.com", conn=batch_db, gmail=gmail,
    )
    assert retried["status"] == "drafted"
    assert len(gmail.created) == 2


def test_client_builds_flowing_html_draft_with_plain_text_alternative_without_sending():
    calls = []

    class Request:
        def execute(self):
            return {"id": "gmail-draft-id"}

    class Drafts:
        def create(self, *, userId, body):
            calls.append((userId, body))
            return Request()

    class Service:
        def users(self):
            return self

        def drafts(self):
            return Drafts()

    client = GmailDraftClient.__new__(GmailDraftClient)
    client.email = "personal@gmail.com"
    client._service = Service()
    assert client.create_draft(
        recipient_email="person@example.com", subject="Engineer role",
        body_text="Hi Pat,\n\nA short note about A&B <systems>.\nAnother sentence.\n\nThanks,\nIshav",
    ) == "gmail-draft-id"
    assert len(calls) == 1
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(calls[0][1]["message"]["raw"]))
    assert message["To"] == "person@example.com"
    assert message["From"] == "personal@gmail.com"
    assert message["Subject"] == "Engineer role"
    assert message.get_content_type() == "multipart/alternative"
    plain, rich = message.get_payload()
    assert plain.get_content_type() == "text/plain"
    assert plain.get_content().startswith("Hi Pat,\n\nA short note about A&B <systems>.")
    assert rich.get_content_type() == "text/html"
    assert rich.get_content() == (
        "<div>Hi Pat,</div><div><br></div>"
        "<div>A short note about A&amp;B &lt;systems&gt;.<br>Another sentence.</div>"
        "<div><br></div><div>Thanks,<br>Ishav</div>\n"
    )


def test_connect_stores_account_and_token_privately(tmp_path, monkeypatch):
    class Credentials:
        refresh_token = "refresh-token"

        def to_json(self):
            return json.dumps({"refresh_token": self.refresh_token})

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            assert path == str(tmp_path / "client.json")
            assert scopes == [gmail_module.GMAIL_COMPOSE_SCOPE]
            return cls()

        def run_local_server(self, **kwargs):
            assert kwargs["access_type"] == "offline"
            return Credentials()

    class ProfileRequest:
        def execute(self):
            return {"emailAddress": "Personal@Gmail.com"}

    class Service:
        def users(self):
            return self

        def getProfile(self, **kwargs):
            assert kwargs == {"userId": "me"}
            return ProfileRequest()

    monkeypatch.setattr(gmail_module.config, "GMAIL_TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(gmail_module.config, "GMAIL_ACCOUNT_PATH", tmp_path / "account.json")
    monkeypatch.setattr(gmail_module, "_google_modules", lambda: (Credentials, Flow, lambda *args, **kwargs: Service()))
    (tmp_path / "client.json").write_text("{}", encoding="utf-8")

    assert gmail_module.connect_gmail(tmp_path / "client.json") == "personal@gmail.com"
    assert gmail_module.connected_account() == "personal@gmail.com"
    assert stat.S_IMODE((tmp_path / "token.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "account.json").stat().st_mode) == 0o600
