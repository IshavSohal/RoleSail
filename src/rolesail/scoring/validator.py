"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven -- no hardcoded personal data. The validator receives
a profile dict (from rolesail.config.load_profile()) and validates against the user's
actual skills, companies, projects, and school.

Validation modes
----------------
strict  -- banned words = hard errors that trigger retries (original behavior)
normal  -- banned words = warnings only; fabrication/structure = errors (default)
lenient -- banned words ignored; only fabrication and required structure checked
"""

import logging
import re
import unicodedata

log = logging.getLogger(__name__)


# ── Universal Constants (not personal data) ───────────────────────────────

BANNED_WORDS: list[str] = [
    "passionate", "dedicated", "committed to",
    "utilizing", "utilize", "harnessing",
    "spearheaded", "spearhead", "orchestrated", "championed", "pioneered",
    "robust", "scalable solutions", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability",
    "strong communicator", "team player", "fast learner", "self-starter", "go-getter",
    "synergy", "cross-functional collaboration", "holistic",
    "transformative", "innovative solutions", "paradigm", "ecosystem",
    "proactive", "detail-oriented", "highly motivated",
    "seamless", "full lifecycle",
    "deep understanding", "extensive experience", "comprehensive knowledge",
    "thrives in", "excels at", "adept at", "well-versed in",
    "i am confident", "i believe", "i am excited",
    "plays a critical role", "instrumental in", "integral part of",
    "strong track record", "eager to", "eager",
    # Cover-letter-specific additions
    "this demonstrates", "this reflects", "i have experience with",
    "furthermore", "additionally", "moreover",
]

LLM_LEAK_PHRASES: list[str] = [
    "i am sorry", "i apologize", "i will try", "let me try",
    "i am at a loss", "i am truly sorry", "apologies for",
    "i keep fabricating", "i will have to admit", "one final attempt",
    "one last time", "if it fails again", "persistent errors",
    "i am having difficulty", "i made an error", "my mistake",
    "here is the corrected", "here is the revised", "here is the updated",
    "here is my", "below is the", "as requested",
    "note:", "disclaimer:", "important:",
    "i have rewritten", "i have removed", "i have fixed",
    "i have replaced", "i have updated", "i have corrected",
    "per your feedback", "based on your feedback", "as per the instructions",
    "the following resume", "the resume below",
    "the following cover letter", "the letter below",
]

# Known fabrication markers: completely unrelated tools/languages.
# Reasonable stretches (K8s, Terraform, Redis, Kafka etc.) are ALLOWED.
FABRICATION_WATCHLIST: set[str] = {
    # Languages with zero relation to the candidate's stack
    "golang", "rust", "ruby",
    "kotlin", "swift", "scala", "matlab",
    # Frameworks for wrong languages
    "spring", "rails", "angular", "svelte",
    # Hard lies: certifications can't be stretched
    "certif", "certified", "pmp", "scrum master", "aws certified",
}

REQUIRED_SECTIONS: set[str] = {"TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}


# ── Helpers ───────────────────────────────────────────────────────────────

def _build_skills_set(profile: dict) -> set[str]:
    """Build the set of allowed skills from the profile's skills_boundary."""
    boundary = profile.get("skills_boundary", {})
    allowed: set[str] = set()
    for category in boundary.values():
        if isinstance(category, (list, set)):
            allowed.update(s.lower().strip() for s in category)
    return allowed


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")   # em dash -> comma
    text = text.replace("\u2013", "-")    # en dash -> hyphen
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    return text.strip()


# ── JSON Field Validation ─────────────────────────────────────────────────

def _canonical_source_text(value: object) -> str:
    """Normalize LaTeX or plain text for conservative source membership checks."""
    text = str(value or "").lower().replace("\\&", "and")
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^]]*\])?", " ", text)
    text = text.replace("&", "and")
    return re.sub(r"[^a-z0-9+#.]+", " ", text).strip()


def _source_contains(source: str, claim: object) -> bool:
    words = _canonical_source_text(claim).split()
    if not words:
        return True
    normalized_source = _canonical_source_text(source)
    return all(word in normalized_source.split() for word in words)


def _canonical_entity_name(value: object) -> str:
    """Normalize an entity name so cosmetic differences cannot hide a duplicate."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", " ", text).strip()


def validate_json_fields(
    data: dict,
    profile: dict,
    mode: str = "normal",
    original_text: str = "",
) -> dict:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:    Parsed JSON from the LLM (title, skills, experience, projects, education).
        profile: User profile dict from load_profile().
        mode:    Validation strictness — "strict", "normal", or "lenient".
                 strict  → banned words are errors (trigger retries)
                 normal  → banned words are warnings (no retry)
                 lenient → banned words ignored entirely

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Required keys — always checked regardless of mode
    for key in ("keywords", "skills", "experience", "projects", "education"):
        if key not in data or data[key] in (None, "", {}):
            errors.append(f"Missing required field: {key}")
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings}

    keyword_groups = data.get("keywords")
    required_keyword_groups = {
        "must_have_technical",
        "preferred_technical",
        "responsibility_action",
        "domain_product",
    }
    if not isinstance(keyword_groups, dict) or not required_keyword_groups.issubset(keyword_groups):
        errors.append("Keywords must include all four required groups")
    education_entries = data.get("education")
    if not isinstance(education_entries, list) or not education_entries or not all(
        isinstance(entry, dict) and entry.get("institution") and entry.get("degree")
        for entry in education_entries
    ):
        errors.append("Education must be a non-empty list of structured source entries")

    # Collect all text for bulk checks
    all_text_parts: list[str] = []

    # Exactly five source entities, with stable unique IDs.
    experience = data["experience"] if isinstance(data["experience"], list) else []
    projects = data["projects"] if isinstance(data["projects"], list) else []
    entities = experience + projects
    if len(entities) != 5:
        errors.append(f"Expected exactly 5 resume entities, received {len(entities)}")
    entity_ids = [str(entry.get("entity_id", "")).strip() for entry in entities if isinstance(entry, dict)]
    if any(not entity_id for entity_id in entity_ids):
        errors.append("Every selected entity must have an entity_id")
    if len(entity_ids) != len(set(entity_ids)):
        errors.append("Selected entity IDs must be unique")
    if not experience:
        errors.append("At least one professional experience is required")

    for entry in experience:
        if not isinstance(entry, dict) or not entry.get("role") or not entry.get("company"):
            errors.append("Every experience requires role and company fields")
    for entry in projects:
        if not isinstance(entry, dict) or not entry.get("name"):
            errors.append("Every project requires a name field")

    # A project may have multiple source variants, but only one variant can be
    # selected for a tailored resume. This is a hard error in every validation
    # mode; unique entity IDs alone do not prevent duplicate named projects.
    project_names: dict[str, str] = {}
    duplicate_project_names: set[str] = set()
    for entry in projects:
        if not isinstance(entry, dict):
            continue
        display_name = str(entry.get("name") or "").strip()
        canonical_name = _canonical_entity_name(display_name)
        if not canonical_name:
            continue
        if canonical_name in project_names:
            duplicate_project_names.add(project_names[canonical_name])
        else:
            project_names[canonical_name] = display_name
    for name in sorted(duplicate_project_names, key=str.casefold):
        errors.append(f"Duplicate project name: '{name}' is selected more than once")

    for entry in entities:
        bullets = entry.get("bullets", []) if isinstance(entry, dict) else []
        if not isinstance(bullets, list) or not 1 <= len(bullets) <= 4:
            errors.append(f"Entity '{entry.get('entity_id', '?')}' must contain 1-4 bullets")
        elif len(bullets) < 3:
            warnings.append(f"Entity '{entry.get('entity_id', '?')}' contains fewer than 3 bullets")

    # Skills: source-only, using profile boundary plus the uploaded resume itself.
    if isinstance(data["skills"], dict):
        allowed_skills = _build_skills_set(profile)
        skill_values: list[str] = []
        for values in data["skills"].values():
            if isinstance(values, list):
                skill_values.extend(str(value).strip() for value in values)
            else:
                skill_values.extend(value.strip() for value in str(values).split(","))
        skills_text = " ".join(skill_values).lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_text:
                errors.append(f"Fabricated skill: '{fake}'")
        for skill in skill_values:
            if not skill:
                continue
            if skill.lower() not in allowed_skills and original_text and not _source_contains(original_text, skill):
                errors.append(f"Unsupported skill: '{skill}'")

    # Experience/project identity must be grounded in the source resume.
    resume_facts = profile.get("resume_facts", {})
    if experience:
        for entry in experience:
            company = entry.get("company") or entry.get("header", "")
            if original_text and company and not _source_contains(original_text, company):
                errors.append(f"Experience company is not supported by the master resume: '{company}'")
            for field in ("role", "dates"):
                value = entry.get(field)
                if original_text and value and not _source_contains(original_text, value):
                    errors.append(f"Experience {field} is not supported by the master resume: '{value}'")
            for b in entry.get("bullets", []):
                all_text_parts.append(str(b.get("text", "")) if isinstance(b, dict) else str(b))

    # Projects: collect bullets
    if projects:
        for entry in projects:
            name = entry.get("name") or entry.get("header", "")
            if original_text and name and not _source_contains(original_text, name):
                errors.append(f"Project is not supported by the master resume: '{name}'")
            dates = entry.get("dates")
            if original_text and dates and not _source_contains(original_text, dates):
                errors.append(f"Project dates are not supported by the master resume: '{dates}'")
            for b in entry.get("bullets", []):
                all_text_parts.append(str(b.get("text", "")) if isinstance(b, dict) else str(b))

    # Education: preserved school must be present (always enforced)
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school:
        edu = str(data.get("education", ""))
        if not _source_contains(edu, preserved_school):
            errors.append(f"Education '{preserved_school}' missing")

    # All generated numbers must already occur somewhere in the source resume.
    if original_text:
        # LaTeX escapes percent signs (``44\%``), while selected source bullets
        # are converted to plain text (``44%``) before reaching this validator.
        # Normalize the source first so an unchanged metric is not mistaken for
        # a fabricated one.
        normalized_source = original_text.replace(r"\%", "%")
        source_numbers = set(re.findall(r"\b\d[\d.,]*(?:%|[kKmMbB]\+?)?", normalized_source))
        generated_numbers = set(re.findall(r"\b\d[\d.,]*(?:%|[kKmMbB]\+?)?", " ".join(all_text_parts)))
        for number in sorted(generated_numbers - source_numbers):
            errors.append(f"Unsupported metric or number: '{number}'")

    # Bulk text checks
    all_text = " ".join(all_text_parts).lower()

    # LLM self-talk is always an error regardless of mode (indicates broken output)
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Banned filler words — severity depends on mode
    if mode != "lenient":
        found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", all_text)]
        if found_banned:
            msg = f"Banned words: {', '.join(found_banned[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Full Resume Text Validation ───────────────────────────────────────────

def validate_tailored_resume(text: str, profile: dict, original_text: str = "") -> dict:
    """Programmatic validation of a tailored resume against the user's profile.

    Args:
        text: The tailored resume text to validate.
        profile: User profile dict from load_profile().
        original_text: The original base resume text (for fabrication comparison).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})

    # 1. Check required sections exist (flexible matching)
    section_variants: dict[str, list[str]] = {
        "TECHNICAL SKILLS": ["technical skills", "skills", "tech stack", "core skills", "technologies"],
        "EXPERIENCE": ["experience", "work experience", "professional experience"],
        "PROJECTS": ["projects", "personal projects", "key projects", "selected projects"],
        "EDUCATION": ["education", "academic background"],
    }
    for section, variants in section_variants.items():
        if not any(v in text_lower for v in variants):
            errors.append(f"Missing required section: {section} (or variant)")

    # 2. Check name preserved (warn, don't error -- we can inject it)
    full_name = personal.get("full_name", "")
    if full_name and full_name.lower() not in text_lower:
        warnings.append(f"Name '{full_name}' missing -- will be injected")

    # 3. Check companies preserved
    for company in resume_facts.get("preserved_companies", []):
        if company.lower() not in text_lower:
            errors.append(f"Company '{company}' missing -- cannot remove real experience")

    # 4. Check projects preserved
    for project in resume_facts.get("preserved_projects", []):
        if project.lower() not in text_lower:
            warnings.append(f"Project '{project}' not found -- may have been renamed")

    # 5. Check school preserved
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school and preserved_school.lower() not in text_lower:
        errors.append(f"Education '{preserved_school}' missing")

    # 6. Check contact info preserved (warn, don't error -- we can inject)
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    if email and email.lower() not in text_lower:
        warnings.append("Email missing -- will be injected")
    if phone and phone not in text:
        warnings.append("Phone missing -- will be injected")

    # 7. Scan TECHNICAL SKILLS section for fabricated tools
    skills_start = text_lower.find("technical skills")
    skills_end = text_lower.find("technical skills", skills_start) if skills_start != -1 else -1
    if skills_start != -1 and skills_end != -1:
        skills_block = text_lower[skills_start:skills_end]
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_block:
                errors.append(f"FABRICATED SKILL in Technical Skills: '{fake}'")

    # 8. Scan full document for fabrication watchlist items not in original
    if original_text:
        original_lower = original_text.lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in text_lower and fake not in original_lower:
                warnings.append(f"New tool/skill appeared: '{fake}' (not in original)")

    # 9. Em dashes (should be auto-fixed by sanitize_text, but safety net)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 10. Banned words (word-boundary matching)
    found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
    if found_banned:
        errors.append(f"Banned words: {', '.join(found_banned[:5])}")

    # 11. LLM self-talk leak detection
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 12. Duplicate section detection
    for section_name in ["experience", "education", "projects"]:
        count = text_lower.count(f"\n{section_name}\n") + text_lower.count(f"\n{section_name} \n")
        if text_lower.startswith(f"{section_name}\n"):
            count += 1
        if count > 1:
            errors.append(f"Section '{section_name}' appears {count} times.")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── Cover Letter Validation ──────────────────────────────────────────────

def validate_cover_letter(text: str, mode: str = "normal") -> dict:
    """Programmatic validation of a cover letter.

    Args:
        text: The cover letter text to validate.
        mode: Validation strictness — "strict", "normal", or "lenient".
              strict  → banned words are errors (trigger retries); word limit enforced
              normal  → banned words are warnings; word limit is soft (+25 words)
              lenient → banned words ignored; word count not checked

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    # 1. Em dashes — always an error (sanitize_text should have caught these)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 2. Banned words — severity depends on mode
    if mode != "lenient":
        found = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
        if found:
            msg = f"Banned words: {', '.join(found[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    # 3. Word count
    words = len(text.split())
    if mode == "strict" and words > 250:
        errors.append(f"Too long ({words} words). Max 250.")
    elif mode == "normal" and words > 275:
        warnings.append(f"Long ({words} words). Target 250.")
    # lenient: no word count check

    # 4. LLM self-talk — always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 5. Must start with "Dear" — always checked (preamble should have been stripped)
    stripped = text.strip()
    if not stripped.lower().startswith("dear"):
        errors.append("Must start with 'Dear Hiring Manager,'")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}
