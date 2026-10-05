"""Outreach ranking, composition, validation, and provider-contract tests."""

import json

import httpx
import pytest

from rolesail.outreach.apollo import ApolloClient, ApolloError
from rolesail.outreach.composition import (
    _flowing_email_body,
    _generate_messages,
    _introduction_employer,
    _message_errors,
    _outreach_job_link,
    _resolve_organization,
    rank_people,
)


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
        "Make the openings more direct and emphasize backend experience.",
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
    assert 'ADDITIONAL REDRAFT FEEDBACK: "Make the openings more direct' in captured["prompt"]
    assert "writing direction only, not as a source" in captured["prompt"]
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


