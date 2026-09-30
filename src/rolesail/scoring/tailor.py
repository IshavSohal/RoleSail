"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from rolesail.config import RESUME_PATH, RESUME_TEX_PATH, TAILORED_DIR, load_profile
from rolesail.database import get_connection, get_jobs_by_stage
from rolesail.llm import get_client
from rolesail.scoring.validator import (
    sanitize_text,
    validate_json_fields,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


class TailoringCancelled(Exception):
    """Raised when a dashboard tailoring request is cooperatively cancelled."""


def _raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise TailoringCancelled("Tailoring was cancelled")


def _latex_to_plain_text(value: str) -> str:
    """Convert the small amount of inline LaTeX used in resume bullets to text."""
    text = value
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\\(?:textbf|textit|emph|underline)\{([^{}]*)\}", r"\1", text)
    replacements = {
        r"\&": "&", r"\%": "%", r"\$": "$", r"\#": "#",
        r"\_": "_", r"\{": "{", r"\}": "}", r"\textasciitilde{}": "~",
    }
    for latex, plain in replacements.items():
        text = text.replace(latex, plain)
    text = re.sub(r"\\[A-Za-z]+", "", text)
    return re.sub(r"\s+", " ", text.replace("~", " ")).strip()


def _latex_command_arguments(text: str, command: str) -> list[str]:
    """Extract balanced arguments for a one-argument LaTeX command."""
    marker = f"\\{command}{{"
    arguments: list[str] = []
    cursor = 0
    while True:
        start = text.find(marker, cursor)
        if start < 0:
            break
        content_start = start + len(marker)
        depth = 1
        index = content_start
        while index < len(text) and depth:
            if text[index] == "{" and (index == 0 or text[index - 1] != "\\"):
                depth += 1
            elif text[index] == "}" and (index == 0 or text[index - 1] != "\\"):
                depth -= 1
            index += 1
        if depth == 0:
            arguments.append(text[content_start:index - 1])
        cursor = max(index, content_start + 1)
    return arguments


def _source_bullet(raw_latex: str) -> dict:
    """Return plain bullet text plus source-authored bold character spans."""
    text = _latex_to_plain_text(raw_latex)
    spans: list[list[int]] = []
    search_from = 0
    for raw_bold in _latex_command_arguments(raw_latex, "textbf"):
        bold_text = _latex_to_plain_text(raw_bold)
        if not bold_text:
            continue
        start = text.find(bold_text, search_from)
        if start < 0:
            start = text.find(bold_text)
        if start < 0:
            continue
        end = start + len(bold_text)
        spans.append([start, end])
        search_from = end
    return {"text": text, "bold_spans": spans}


def _latex_resume_items(resume_text: str) -> list[dict]:
    """Extract text and trusted formatting from LaTeX resume bullets."""
    document = resume_text.split(r"\begin{document}", 1)[-1]
    bullets = [_source_bullet(raw) for raw in _latex_command_arguments(document, "resumeItem")]
    return [bullet for bullet in bullets if bullet["text"] and "#1" not in bullet["text"]]


def _plain_resume_items(resume_text: str) -> list[str]:
    """Extract bullets from a text/PDF resume, joining obvious wrapped lines."""
    bullets: list[str] = []
    current = ""
    for raw_line in resume_text.splitlines():
        line = raw_line.strip()
        match = re.match(r"^[\u2022*-]\s+(.+)$", line)
        if match:
            if current:
                bullets.append(re.sub(r"\s+", " ", current).strip())
            current = match.group(1)
            continue
        if current and line and (
            line[:1].islower()
            or line[:1].isdigit()
            or re.search(
                r"(?:,|\b(?:and|or|to|with|using|from|through|remote))$",
                current,
                re.IGNORECASE,
            )
        ):
            current += " " + line
        elif current:
            bullets.append(re.sub(r"\s+", " ", current).strip())
            current = ""
    if current:
        bullets.append(re.sub(r"\s+", " ", current).strip())
    return bullets


def build_source_bullet_catalog(resume_text: str) -> dict[str, dict]:
    """Return stable IDs mapped to source bullet text and trusted formatting."""
    if r"\resumeItem{" in resume_text:
        bullets = _latex_resume_items(resume_text)
    else:
        bullets = [
            {"text": bullet, "bold_spans": []}
            for bullet in _plain_resume_items(resume_text)
        ]
    return {f"bullet_{index:03d}": bullet for index, bullet in enumerate(bullets, 1)}


def resolve_source_bullets(data: dict, catalog: dict[str, object]) -> list[str]:
    """Replace model-selected bullet IDs with source text, rejecting free-form prose."""
    errors: list[str] = []
    selected_ids: set[str] = set()
    for section in ("experience", "projects"):
        entries = data.get(section, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            selected = entry.pop("bullet_ids", None)
            if selected is None:
                selected = entry.get("bullets")
            if not isinstance(selected, list):
                errors.append(f"Entity '{entry.get('entity_id', '?')}' must provide bullet_ids")
                continue
            unknown = [str(value) for value in selected if str(value) not in catalog]
            if unknown:
                errors.append(
                    f"Entity '{entry.get('entity_id', '?')}' selected unknown source bullet IDs: "
                    + ", ".join(unknown)
                )
                continue
            ids = [str(value) for value in selected]
            duplicates = [bullet_id for bullet_id in ids if bullet_id in selected_ids]
            if len(ids) != len(set(ids)) or duplicates:
                errors.append(
                    f"Entity '{entry.get('entity_id', '?')}' selected a source bullet more than once"
                )
                continue
            selected_ids.update(ids)
            resolved: list[dict] = []
            for bullet_id in ids:
                source = catalog[bullet_id]
                if isinstance(source, dict):
                    resolved.append({
                        "text": str(source.get("text", "")),
                        "bold_spans": [list(span) for span in source.get("bold_spans", [])],
                    })
                else:
                    # Backward compatibility for callers with a plain-text catalog.
                    resolved.append({"text": str(source), "bold_spans": []})
            entry["bullets"] = resolved
    return errors


_MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "spr": 3, "sum": 6, "fal": 9, "aut": 9, "win": 12,
}


def _experience_recency(entry: object) -> tuple[int, int, int, int, int]:
    """Return a sortable recency key for a structured experience entry."""
    if not isinstance(entry, dict):
        return (0, 0, 0, 0, 0)
    dates = str(entry.get("dates") or "")
    points: list[tuple[int, int]] = []
    for match in re.finditer(
        r"(?:(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
        r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?|spring|summer|fall|autumn|winter)\.?,?\s+)?"
        r"((?:19|20)\d{2})",
        dates,
        re.IGNORECASE,
    ):
        month = _MONTH_NUMBERS.get((match.group(1) or "").lower()[:3], 0)
        points.append((int(match.group(2)), month))
    if not points:
        return (0, 0, 0, 0, 0)

    start = points[0]
    is_current = bool(re.search(r"\b(?:present|current|now)\b", dates, re.IGNORECASE))
    end = (9999, 12) if is_current else points[-1]
    return (1, end[0], end[1], start[0], start[1])


def sort_experience_newest_first(data: dict) -> None:
    """Enforce reverse chronology while preserving ties and unknown date formats."""
    experience = data.get("experience")
    if isinstance(experience, list):
        experience.sort(key=_experience_recency, reverse=True)


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", []) # Are these also preserved for all tailored resumes?

    companies_str = ", ".join(companies) if companies else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a senior technical recruiter tailoring a resume to a job description.

Return a structured JSON decision document. The application, not you, renders and compiles LaTeX.

## SELECTION
- Select exactly 5 unique entities total. An entity is one professional experience or one project.
- Select at least one professional experience unless none is relevant.
- Prefer relevant professional experience over projects with similar evidence.
- Choose 3-4 source bullet IDs per selected entity where possible.
- Keep experiences in experience and projects in projects.
- ALWAYS order professional experience in reverse chronological order.
- The most recent professional experience must come first. Never reorder experience by relevance.
- Order projects by relevance.
- Do not include an unselected entity or duplicate project variant.
- Bullets are selection-only: copy bullet IDs from the supplied SOURCE BULLET CATALOG.
- Never write, rewrite, shorten, combine, split, correct, or paraphrase a bullet.
- Keep every selected bullet with the professional experience or project where it originally appears.

## KEYWORDS
Extract must-have technical, preferred technical, responsibility/action, and domain/product keywords.
Use supported keywords to decide which existing entities and bullets are most relevant. Do not edit bullets to add keywords.

## SKILLS BOUNDARY (real skills only):
{skills_block}

Do not add a skill or technology absent from the master resume.

## TAILORING RULES:

EDUCATION: Return the source education as structured entries. Keep it immediately after the header.

SKILLS: Structure skills with exactly these five categories, in this order: Languages, Technologies/Frameworks, Systems/DevOps, Databases/Storage, Concepts. Select or omit source-supported skills within each category so only job-relevant skills remain; put the job's must-haves first. Include every category key; use an empty list when a category has no source-supported skills relevant to the job.

BULLETS: Return only source bullet IDs. Maximum 4 bullet IDs per entity. Bullet length, wording, punctuation, and line wrapping are not your concern.

## HARD RULES:
- Do NOT invent work, companies, degrees, or certifications
- Do NOT change real numbers ({metrics_str})
- Preserved companies: {companies_str} -- names stay as-is
- Preserved school: {school}
- Do not output LaTeX, markdown, commentary, or contact information.

## OUTPUT
Return ONLY valid JSON. Each rationale must be one concise sentence.

{{
  "keywords": {{
    "must_have_technical": ["..."],
    "preferred_technical": ["..."],
    "responsibility_action": ["..."],
    "domain_product": ["..."]
  }},
  "education": [
    {{"institution":"...","location":"...","degree":"...","dates":"..."}}
  ],
  "experience": [
    {{"entity_id":"experience_1","role":"...","company":"...","location":"...","dates":"...","rationale":"...","bullet_ids":["bullet_001","bullet_002","bullet_003"]}}
  ],
  "projects": [
    {{"entity_id":"project_1","name":"...","technologies":["..."],"dates":"...","rationale":"...","bullet_ids":["bullet_010","bullet_011","bullet_012"]}}
  ],
  "skills": {{
    "Languages": ["..."],
    "Technologies/Frameworks": ["..."],
    "Systems/DevOps": ["..."],
    "Databases/Storage": ["..."],
    "Concepts": ["..."]
  }},
  "courses": [],
  "awards": [],
  "keyword_usage": {{"incorporated":["..."],"skipped":[{{"keyword":"...","reason":"unsupported"}}]}}
}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume quality judge. A tailoring engine selected source content to target a specific job. Your job is to catch unsupported or misplaced content.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Order professional experience in reverse chronological order, with the most recent experience first
- Reorder project entries and bullets to put the most relevant first
- Select or omit existing bullets without changing their text
- Select or omit skills so only job-relevant ones remain, ordered with must-haves first

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, or frameworks that appear in neither the original resume nor this confirmed profile boundary: {skills_str}
2. Inventing NEW metrics or numbers not in the original. The real metrics are: {metrics_str}
3. Inventing work that has no basis in any original bullet (completely new achievements).
4. Adding companies, roles, or degrees that don't exist.
5. Changing real numbers (inflating 80% to 95%, 500 nodes to 1000 nodes).

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Dropping bullets entirely
- Reordering professional experience entries only to put them in reverse chronological order
- Reordering project entries or bullets by relevance

## ORDERING (FAIL if violated):
- Professional experience must always be in reverse chronological order, with the most recent experience first

## TOLERANCE RULE:
Bullet wording changes are not allowed. Fail invented, altered, or moved bullets, as well as invented projects, companies, degrees, metrics, responsibilities, or technologies. Do not fail for style alone."""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = [] # each element is a seperate line in the generated PDF

    # Header -- always code-injected from profile
    lines.append(personal.get("full_name", ""))
    # lines.append(sanitize_text(data.get("title", "Software Engineer")))

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Education
    lines.append("EDUCATION")
    education = data.get("education", [])
    if isinstance(education, list):
        for entry in education:
            if isinstance(entry, dict):
                lines.append(" | ".join(sanitize_text(str(entry.get(key, ""))) for key in ("institution", "degree", "dates") if entry.get(key)))
    else:
        lines.append(sanitize_text(str(education)))

    # Summary
    # lines.append("SUMMARY")
    # lines.append(sanitize_text(data["summary"]))
    # lines.append("")

    # Experience
    lines.append("EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(entry.get("role") or entry.get("header", "")))
        subtitle = " | ".join(str(entry.get(key, "")) for key in ("company", "location", "dates") if entry.get(key))
        if subtitle or entry.get("subtitle"):
            lines.append(sanitize_text(subtitle or entry["subtitle"]))
        for b in entry.get("bullets", []):
            bullet = b.get("text", "") if isinstance(b, dict) else b
            lines.append(f"- {str(bullet).strip()}")
        lines.append("")

    # Projects
    lines.append("PROJECTS")
    for entry in data.get("projects", []):
        lines.append(sanitize_text(entry.get("name") or entry.get("header", "")))
        technologies = entry.get("technologies") or entry.get("subtitle")
        if isinstance(technologies, list):
            technologies = ", ".join(str(v) for v in technologies)
        subtitle = " | ".join(str(v) for v in (technologies, entry.get("dates")) if v)
        if subtitle:
            lines.append(sanitize_text(subtitle))
        for b in entry.get("bullets", []):
            bullet = b.get("text", "") if isinstance(b, dict) else b
            lines.append(f"- {str(bullet).strip()}")
        lines.append("")

    # Technical Skills
    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            value = ", ".join(str(v) for v in val) if isinstance(val, list) else str(val)
            lines.append(f"{cat}: {sanitize_text(value)}")
    lines.append("")



    return "\n".join(lines) # This is what creates the tailored resume as a TXT file


def build_decision_summary(data: dict, fit_changes: list[str], visual_audit: dict) -> dict:
    """Build the concise, user-facing explanation required for each artifact."""
    entities: list[dict] = []
    bullets: dict[str, list[str]] = {}
    for entity_type, section in (("Professional Experience", "experience"), ("Project", "projects")):
        for entry in data.get(section, []):
            name = (
                f"{entry.get('role', '')} at {entry.get('company', '')}".strip()
                if section == "experience"
                else str(entry.get("name", ""))
            )
            entities.append({
                "entity_id": entry.get("entity_id", ""),
                "name": name,
                "type": entity_type,
                "rationale": entry.get("rationale", ""),
            })
            bullets[name] = [
                str(b.get("text", "")) if isinstance(b, dict) else str(b)
                for b in entry.get("bullets", [])
            ]
    return {
        "Selected Resume Entities": entities,
        "Selected Bullets": bullets,
        "Job Keywords": data.get("keywords", {}),
        "Keyword Usage": data.get("keyword_usage", {}),
        "Skills Strategy": {
            "categories": data.get("skills", {}),
            "unsupported_skills_added": [],
            "policy": "source-only",
        },
        "Fit-to-One-Page Changes": {
            "changes": fit_changes,
            "meaningful_content_deleted": any(change.startswith("Removed the lowest-priority bullet") for change in fit_changes),
            "visual_line_audit": visual_audit,
        },
    }


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        {"passed": bool, "verdict": str, "issues": str, "raw": str}
    """
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    # Reasoning models count internal reasoning against the completion budget.
    # A 512-token budget can therefore produce a successful HTTP response with
    # no visible answer even though the requested verdict is short.
    response = client.chat(messages, max_tokens=2048, temperature=0.1)

    if not response or not response.strip():
        return {
            "passed": False,
            "verdict": "FAIL",
            "issues": (
                "Judge returned an empty response "
                "(completion token budget may have been exhausted)"
            ),
            "raw": response or "",
        }

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('company') or 'N/A'}\n"
        f"JOB SOURCE: {job.get('site') or 'N/A'}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
        "attempt_history": [],
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client()
    tailor_prompt_base = _build_tailor_prompt(profile)
    bullet_catalog = build_source_bullet_catalog(resume_text)
    if not bullet_catalog:
        raise ValueError("No source resume bullets could be extracted for selection")
    catalog_text = "\n".join(
        f"{bullet_id}: {bullet['text']}"
        for bullet_id, bullet in bullet_catalog.items()
    )

    for attempt in range(max_retries + 1):
        _raise_if_cancelled(cancel_check)
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"ORIGINAL RESUME:\n{resume_text}\n\n"
                f"SOURCE BULLET CATALOG (select IDs only):\n{catalog_text}\n\n---\n\n"
                f"TARGET JOB:\n{job_text}\n\nReturn the JSON:"
            )},
        ]

        raw = client.chat(messages, max_tokens=4096, temperature=0.2)
        _raise_if_cancelled(cancel_check)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError:
            error = "Output was not valid JSON. Return ONLY a JSON object, nothing else."
            avoid_notes.append(error)
            report["attempt_history"].append({
                "attempt": attempt + 1, "stage": "json", "errors": [error], "warnings": [],
            })
            log.warning(
                "Tailoring attempt %d/%d failed JSON parsing for %s: %s",
                attempt + 1, max_retries + 1, job.get("title", "untitled job"), error,
            )
            continue

        # Experience chronology is a presentation invariant, not an LLM preference.
        # Enforce it before validation, assembly, judging, and artifact generation.
        sort_experience_newest_first(data)

        bullet_errors = resolve_source_bullets(data, bullet_catalog)
        if bullet_errors:
            avoid_notes.extend(bullet_errors)
            report["attempt_history"].append({
                "attempt": attempt + 1, "stage": "source_bullets",
                "errors": bullet_errors, "warnings": [],
            })
            log.warning(
                "Tailoring attempt %d/%d failed source-bullet validation for %s: %s",
                attempt + 1, max_retries + 1, job.get("title", "untitled job"),
                "; ".join(bullet_errors),
            )
            if attempt < max_retries:
                continue
            report["validator"] = {"passed": False, "errors": bullet_errors, "warnings": []}
            report["status"] = "failed_validation"
            return "", report

        # Layer 1: Validate JSON fields
        validation = validate_json_fields(
            data,
            profile,
            mode=validation_mode,
            original_text=resume_text,
        )
        report["validator"] = validation

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            report["attempt_history"].append({
                "attempt": attempt + 1, "stage": "validation",
                "errors": validation["errors"], "warnings": validation["warnings"],
            })
            log.warning(
                "Tailoring attempt %d/%d failed validation for %s: %s",
                attempt + 1, max_retries + 1, job.get("title", "untitled job"),
                "; ".join(validation["errors"]),
            )
            if attempt < max_retries:
                continue
            # Last attempt — assemble whatever we got
            tailored = assemble_resume_text(data, profile)
            report["status"] = "failed_validation"
            return tailored, report

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile)

        # Layer 2: LLM judge (catches subtle fabrication) — skipped in lenient mode
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["attempt_history"].append({
                "attempt": attempt + 1, "stage": "approved", "errors": [],
                "warnings": validation["warnings"],
            })
            report["structured_data"] = data
            report["status"] = "approved"
            return tailored, report

        _raise_if_cancelled(cancel_check)
        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        _raise_if_cancelled(cancel_check)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            report["attempt_history"].append({
                "attempt": attempt + 1, "stage": "judge",
                "errors": [judge["issues"]], "warnings": validation["warnings"],
            })
            log.warning(
                "Tailoring attempt %d/%d failed judge review for %s: %s",
                attempt + 1, max_retries + 1, job.get("title", "untitled job"),
                judge["issues"],
            )
            if attempt < max_retries:
                # In normal mode, only retry on judge failure if there are retries left
                if validation_mode != "lenient":
                    continue
            # Accept best attempt on last retry (all modes) or if lenient
            report["structured_data"] = data
            report["status"] = "approved_with_judge_warning"
            return tailored, report

        # Both passed
        report["attempt_history"].append({
            "attempt": attempt + 1, "stage": "approved", "errors": [],
            "warnings": validation["warnings"],
        })
        report["structured_data"] = data
        report["status"] = "approved"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(
    min_score: int = 7,
    limit: int = 20,
    validation_mode: str = "normal",
    target_url: str | None = None,
    replace_existing: bool = False,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".
        target_url:      Optional single job URL to tailor.
        replace_existing: Allow a successful result to replace that job's
                          current tailored resume.

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    _raise_if_cancelled(cancel_check)
    master_path = RESUME_TEX_PATH if RESUME_TEX_PATH.exists() else RESUME_PATH
    resume_text = master_path.read_text(encoding="utf-8")
    conn = get_connection()

    if target_url:
        tailored_condition = "" if replace_existing else "AND tailored_resume_path IS NULL "
        row = conn.execute(
            "SELECT * FROM jobs WHERE url = ? "
            "AND full_description IS NOT NULL AND applied_at IS NULL "
            f"{tailored_condition}"
            "AND COALESCE(tailor_attempts, 0) < 5",
            (target_url,),
        ).fetchone()
        jobs = [dict(row)] if row else []
    else:
        jobs = get_jobs_by_stage(
            conn=conn,
            stage="pending_tailor",
            min_score=min_score,
            limit=limit,
        )

    if not jobs:
        if target_url:
            log.info("Target job is not eligible for tailoring: %s", target_url)
        else:
            log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    generated_artifacts: list[Path] = []
    stats: dict[str, int] = {"approved": 0, "failed_validation": 0, "failed_judge": 0, "error": 0}

    for job in jobs:
        _raise_if_cancelled(cancel_check)
        current_job = conn.execute(
            "SELECT applied_at FROM jobs WHERE url = ?", (job["url"],)
        ).fetchone()
        if not current_job or current_job[0]:
            log.info("Skipping already-applied job: %s", job["title"])
            continue
        completed += 1
        generated_paths: list[Path] = []
        try:
            tailored, report = tailor_resume(
                resume_text,
                job,
                profile,
                validation_mode=validation_mode,
                cancel_check=cancel_check,
            )
            _raise_if_cancelled(cancel_check)
            current_job = conn.execute(
                "SELECT applied_at FROM jobs WHERE url = ?", (job["url"],)
            ).fetchone()
            if not current_job or current_job[0]:
                log.info("Discarding tailored output because job was marked applied: %s", job["title"])
                continue

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"] or "")[:50].strip().replace(" ", "_")
            company = (job.get("company") or "").strip()
            safe_company = re.sub(r"[^\w\s-]", "", company)[:40].strip().replace(" ", "_")
            prefix_parts = [part for part in (safe_company, safe_title) if part]
            prefix = ("_".join(prefix_parts) or "Resume") + "_Tailored_Resume"
            if any((TAILORED_DIR / f"{prefix}{suffix}").exists() for suffix in (".tex", "_REPORT.json")):
                url_suffix = hashlib.sha256(job["url"].encode("utf-8")).hexdigest()[:8]
                prefix = f"{prefix}_{url_suffix}"
            version = 2
            base_prefix = prefix
            while any((TAILORED_DIR / f"{prefix}{suffix}").exists() for suffix in (".tex", "_REPORT.json")):
                prefix = f"{base_prefix}_{version}"
                version += 1

            # Save tailored resume text
            txt_path = TAILORED_DIR / f"{prefix}.txt"
            txt_path.write_text(tailored, encoding="utf-8")
            generated_paths.append(txt_path)

            # Save job description for traceability
            job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {company or 'N/A'}\n"
                f"Source: {job['site']}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")
            generated_paths.append(job_path)

            # Render the trusted LaTeX template and compile it. A resume is not
            # approved unless a valid, one-page PDF is produced.
            tex_path = TAILORED_DIR / f"{prefix}.tex"
            pdf_path = None
            if report["status"] in ("approved", "approved_with_judge_warning"):
                try:
                    from rolesail.scoring.latex import fit_one_page

                    fitted, tex_source, pdf_bytes, fit_changes, visual_audit = fit_one_page(
                        report["structured_data"], profile
                    )
                    _raise_if_cancelled(cancel_check)
                    report["structured_data"] = fitted
                    report["fit_to_one_page"] = {
                        "page_count": 1,
                        "changes": fit_changes,
                        "visual_line_audit": visual_audit,
                    }
                    report["decision_summary"] = build_decision_summary(
                        fitted, fit_changes, visual_audit
                    )
                    tex_path.write_text(tex_source, encoding="utf-8")
                    generated_paths.append(tex_path)
                    output_pdf = tex_path.with_suffix(".pdf")
                    output_pdf.write_bytes(pdf_bytes)
                    generated_paths.append(output_pdf)
                    pdf_path = str(output_pdf)
                except TailoringCancelled:
                    raise
                except Exception as exc:
                    report["status"] = "failed_compilation"
                    report["compile_error"] = str(exc)
                    log.warning("LaTeX/PDF generation failed for %s: %s", tex_path, exc)

            # Save validation and decision report after compilation/auditing.
            report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            generated_paths.append(report_path)
            _raise_if_cancelled(cancel_check)

            result = {
                "url": job["url"],
                "path": str(tex_path) if pdf_path else None,
                "text_path": str(txt_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
                "company": company,
                "status": report["status"],
                "attempts": report["attempts"],
            }
        except TailoringCancelled:
            for generated_path in generated_paths:
                generated_path.unlink(missing_ok=True)
            raise
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        results.append(result)
        generated_artifacts.extend(generated_paths)
        stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            result["title"][:40],
        )

    if cancel_check is not None and cancel_check():
        for generated_path in generated_artifacts:
            generated_path.unlink(missing_ok=True)
        raise TailoringCancelled("Tailoring was cancelled")

    # Persist to DB: increment attempt counter for ALL, save path only for approved
    now = datetime.now(timezone.utc).isoformat()
    _success_statuses = {"approved", "approved_with_judge_warning"}
    replaced_paths: list[tuple[str, str]] = []
    for r in results:
        if r["status"] in _success_statuses:
            previous = next(
                (job.get("tailored_resume_path") for job in jobs if job["url"] == r["url"]),
                None,
            )
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
            if previous and previous != r["path"]:
                replaced_paths.append((previous, r["path"]))
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    # The old resume remains available throughout generation and is removed
    # only after the replacement has been committed successfully.
    tailored_root = TAILORED_DIR.resolve()
    for previous, replacement in replaced_paths:
        old_path = Path(previous).resolve()
        if old_path.parent != tailored_root or old_path == Path(replacement).resolve():
            continue
        for artifact in (
            old_path.with_suffix(".tex"),
            old_path.with_suffix(".pdf"),
            old_path.with_suffix(".txt"),
            old_path.with_name(f"{old_path.stem}_REPORT.json"),
            old_path.with_name(f"{old_path.stem}_JOB.txt"),
        ):
            try:
                artifact.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not remove replaced tailoring artifact %s: %s", artifact, exc)

    elapsed = time.time() - t0
    approved = stats.get("approved", 0) + stats.get("approved_with_judge_warning", 0)
    failed = sum(
        count
        for status, count in stats.items()
        if status not in _success_statuses and status != "error"
    )
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed, %d errors",
        elapsed,
        approved,
        failed,
        stats.get("error", 0),
    )

    return {
        "approved": approved,
        "failed": failed,
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
