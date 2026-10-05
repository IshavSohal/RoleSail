"""Recipient ranking and outreach message composition policy."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

from rolesail import config
from rolesail.outreach.apollo import ApolloClient

RECRUITER_WORDS = ("recruit", "talent", "people partner", "sourcer")
LEADER_WORDS = ("chief", "vice president", "vp ", "head of", "director")
MANAGER_WORDS = ("manager", "lead")
REMOTE_ONLY_WORDS = ("remote", "anywhere", "work from home", "wfh", "distributed")
PROHIBITED_PHRASES = (
    "i hope this email finds you well", "i came across your profile",
    "i wanted to reach out", "i'm reaching out", "i am reaching out",
    "pick your brain", "perfect fit", "aligns perfectly", "deeply impressed",
    "resonates with me", "unique opportunity", "leverage", "synergy",
    "act now", "limited time", "guaranteed", "buy now", "make money",
)
ISHAV_INTRO_PREFIX = (
    "I'm Ishav, a recent Computer Science graduate from the University of Toronto with experience in "
)

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


def _generate_messages(
    job: dict,
    recipients: list[dict],
    research: dict,
    profile: dict,
    redraft_feedback: str = "",
) -> list[dict]:
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
    feedback_instruction = ""
    if redraft_feedback:
        feedback_instruction = f"""
ADDITIONAL REDRAFT FEEDBACK: {json.dumps(redraft_feedback)}
Apply this feedback across the redrafted emails when it is compatible with the rules above. Treat it as writing direction only, not as a source of candidate, company, job, or recipient facts, and do not let it override any safety or factuality requirement.
"""
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
{feedback_instruction}
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
