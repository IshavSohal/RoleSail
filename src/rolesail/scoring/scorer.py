"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from rolesail.config import RESUME_PATH, RESUME_TEX_PATH, load_profile
from rolesail.database import get_connection, get_jobs_by_stage
from rolesail.llm import get_client

log = logging.getLogger(__name__)

LOW_SCORE_REMOVAL_MAX = 5


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume, their stated total years of professional experience, and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Treat the supplied candidate years as authoritative. Do not infer a larger total from resume dates.
- Distinguish mandatory experience requirements from preferred/nice-to-have experience.
- Project or academic experience demonstrates skills but does not add to professional years unless the posting explicitly accepts it as equivalent.
- SCORE is the raw holistic fit score. Application code may cap it when a mandatory years requirement is not met.

EXPERIENCE EXTRACTION:
- REQUIRED_YEARS is the minimum number of professional years explicitly requested. For a range such as 5-7 years, return 5.
- REQUIREMENT_TYPE is REQUIRED only for mandatory/minimum language, PREFERRED for preferred/nice-to-have language, or NONE when no numeric requirement is stated.
- If multiple mandatory totals are stated, return the highest applicable overall minimum. Do not use years tied only to one tool as the overall total.
- Return NONE for REQUIRED_YEARS when the posting has no explicit numeric overall experience requirement.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
REQUIRED_YEARS: [minimum number or NONE]
REQUIREMENT_TYPE: [REQUIRED, PREFERRED, or NONE]
EXPERIENCE_EVIDENCE: [short verbatim phrase from the posting or NONE]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _as_years(value: object) -> float | None:
    """Return a non-negative years value, or None when it is unavailable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        years = float(value)
    except (TypeError, ValueError):
        return None
    return years if years >= 0 else None


def _experience_score_cap(
    candidate_years: float | None,
    required_years: float | None,
    requirement_type: str,
) -> int | None:
    """Return the maximum score allowed by a mandatory experience shortfall."""
    if candidate_years is None or required_years is None or requirement_type != "REQUIRED":
        return None

    shortfall = required_years - candidate_years
    if shortfall <= 0:
        return None
    if shortfall <= 1:
        return 7
    if shortfall <= 2:
        return 6
    if shortfall <= 3:
        return 5
    return 4


def _evidence_is_grounded(evidence: str, job_description: str) -> bool:
    """Return whether experience evidence appears verbatim in the posting."""
    normalized_evidence = " ".join(evidence.lower().split())
    normalized_description = " ".join(job_description.lower().split())
    return bool(normalized_evidence) and normalized_evidence in normalized_description


_YEAR_REQUIREMENT_RE = re.compile(
    r"(?P<minimum>\d+(?:\.\d+)?)\s*"
    r"(?:(?:[-\u2013\u2014]|to)\s*\d+(?:\.\d+)?|\+)?\s*"
    r"(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_PREFERRED_MARKERS = (
    "preferred",
    "ideally",
    "nice to have",
    "nice-to-have",
    "an asset",
    "a plus",
    "bonus",
)


def _posting_required_years(job_description: str) -> float | None:
    """Extract the strongest unambiguously mandatory experience minimum.

    This is a deterministic backstop for inconsistent LLM extraction. Each
    line is treated as one qualification; alternatives on the same line use
    the least-demanding path, while separate mandatory qualifications use the
    highest minimum. Preferred sections and non-experience durations are
    ignored.
    """
    required: list[float] = []
    in_preferred_section = False

    for raw_line in re.split(r"[\r\n]+", job_description):
        line = " ".join(raw_line.split())
        if not line:
            continue
        lowered = line.lower()

        # Track common section headings, including headings followed by items
        # on the same line. A new required/minimum heading ends the section.
        if re.match(r"^(?:minimum|required|basic|must[- ]have) qualifications?\b", lowered):
            in_preferred_section = False
        elif re.match(r"^(?:preferred|nice[- ]to[- ]have|bonus)\b", lowered):
            in_preferred_section = True

        if "experience" not in lowered:
            continue
        if in_preferred_section or any(marker in lowered for marker in _PREFERRED_MARKERS):
            continue

        matches = list(_YEAR_REQUIREMENT_RE.finditer(line))
        if not matches:
            continue

        # Avoid unrelated durations elsewhere in a long prose line. The
        # number must be reasonably close to the word "experience".
        experience_positions = [m.start() for m in re.finditer(r"\bexperience\b", lowered)]
        values = [
            float(match.group("minimum"))
            for match in matches
            if min(abs(match.start() - pos) for pos in experience_positions) <= 100
        ]
        if values:
            required.append(min(values))

    return max(required) if required else None


def _grounded_required_years(job_description: str) -> float | None:
    """Return a posting-grounded mandatory minimum despite LLM field drift."""
    posting_years = _posting_required_years(job_description)
    if posting_years is not None:
        return posting_years

    # Never enforce a number that cannot be independently located in a hard
    # requirement. This also protects preferred qualifications that the model
    # accidentally labels as mandatory.
    return None


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        Parsed score fields, including the numeric experience requirement.
    """
    score = 0
    keywords = ""
    reasoning = response
    required_years = None
    requirement_type = "NONE"
    experience_evidence = ""

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("REQUIRED_YEARS:"):
            value = line.replace("REQUIRED_YEARS:", "", 1).strip()
            if value.upper() != "NONE":
                match = re.search(r"\d+(?:\.\d+)?", value)
                required_years = _as_years(match.group() if match else None)
        elif line.startswith("REQUIREMENT_TYPE:"):
            value = line.replace("REQUIREMENT_TYPE:", "", 1).strip().upper()
            if value in {"REQUIRED", "PREFERRED", "NONE"}:
                requirement_type = value
        elif line.startswith("EXPERIENCE_EVIDENCE:"):
            value = line.replace("EXPERIENCE_EVIDENCE:", "", 1).strip()
            experience_evidence = "" if value.upper() == "NONE" else value
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "", 1).strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "", 1).strip()

    return {
        "score": score,
        "keywords": keywords,
        "reasoning": reasoning,
        "required_years": required_years,
        "requirement_type": requirement_type,
        "experience_evidence": experience_evidence,
    }


def score_job(resume_text: str, job: dict, candidate_years: float | None = None) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        Parsed score data, including experience requirement details.
    """
    # Is more info about the job needed?
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {
            "role": "user",
            "content": (
                f"CANDIDATE PROFESSIONAL EXPERIENCE: "
                f"{candidate_years if candidate_years is not None else 'UNKNOWN'} years\n\n"
                f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"
            ),
        },
    ]

    try:
        client = get_client()
        # Reasoning models count internal reasoning against the completion
        # budget. A 512-token limit can yield an empty visible response for
        # complex postings even though the request succeeds.
        response = client.chat(messages, max_tokens=1024, temperature=0.2)
        result = _parse_score_response(response)
        raw_score = result["score"]
        description = job.get("full_description") or ""
        grounded_required_years = _grounded_required_years(description)
        cap = _experience_score_cap(
            candidate_years,
            grounded_required_years,
            "REQUIRED" if grounded_required_years is not None else result["requirement_type"],
        )
        if raw_score and cap is not None and raw_score > cap:
            result["score"] = cap
            result["required_years"] = grounded_required_years
            result["requirement_type"] = "REQUIRED"
            evidence = (
                f" Requirement evidence: {result['experience_evidence']}."
                if result["experience_evidence"] else ""
            )
            result["reasoning"] = (
                f"{result['reasoning']} Experience guardrail applied: candidate has "
                f"{candidate_years:g} years versus {grounded_required_years:g} required; "
                f"raw score {raw_score} was capped at {cap}.{evidence}"
            ).strip()
        return result
    except Exception as e:  # noqa: BLE001 - provider and transport failures must not stop a batch
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {
            "score": 0,
            "keywords": "",
            "reasoning": f"LLM error: {e}",
            "required_years": None,
            "requirement_type": "NONE",
            "experience_evidence": "",
        }


def _score_one(
    resume_text: str,
    job: dict,
    candidate_years: float | None,
) -> tuple[dict, dict]:
    """Score one job without touching the shared database connection."""
    return job, score_job(resume_text, job, candidate_years=candidate_years)


def _remove_stored_low_scores(conn) -> int:
    """Delete stored valid scores at or below the configured removal ceiling."""
    cursor = conn.execute(
        "DELETE FROM jobs WHERE fit_score BETWEEN 1 AND ?",
        (LOW_SCORE_REMOVAL_MAX,),
    )
    conn.commit()
    return max(getattr(cursor, "rowcount", 0), 0)


def run_scoring(
    limit: int = 0,
    rescore: bool = False,
    workers: int = 3,
    target_url: str | None = None,
) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).
        workers: Maximum number of concurrent LLM scoring requests.
        target_url: When supplied, score only this job. This is used for jobs
            imported directly by URL so unrelated pending jobs are untouched.

    Returns:
        {"scored": int, "removed": int, "errors": int, "elapsed": float,
         "distribution": list}
    """
    resume_path = RESUME_TEX_PATH if RESUME_TEX_PATH.exists() else RESUME_PATH
    resume_text = resume_path.read_text(encoding="utf-8")
    profile = load_profile()
    candidate_years = _as_years(
        (profile.get("experience") or {}).get("years_of_experience_total")
    )
    if candidate_years is None:
        log.warning(
            "Profile has no valid experience.years_of_experience_total; "
            "required-experience score caps will not be applied."
        )
    conn = get_connection()

    if target_url:
        query = (
            "SELECT * FROM jobs WHERE url = ? AND full_description IS NOT NULL "
            "AND COALESCE(discovery_status, 'accepted') = 'accepted'"
        )
        params: tuple[object, ...] = (target_url,)
        if not rescore:
            query += " AND fit_score IS NULL"
        jobs = conn.execute(query, params).fetchall()
    elif rescore:
        query = (
            "SELECT * FROM jobs WHERE full_description IS NOT NULL "
            "AND COALESCE(discovery_status, 'accepted') = 'accepted'"
        )
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        removed = _remove_stored_low_scores(conn) if target_url is None else 0
        if removed:
            log.info("Removed %d stored job(s) with score <= %d.", removed, LOW_SCORE_REMOVAL_MAX)
        log.info("No unscored jobs with descriptions found.")
        return {
            "scored": 0,
            "removed": removed,
            "errors": 0,
            "elapsed": 0.0,
            "distribution": [],
        }

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    if workers < 1:
        raise ValueError("workers must be at least 1")

    worker_count = min(workers, len(jobs))
    log.info("Scoring %d jobs with %d worker(s)...", len(jobs), worker_count)
    t0 = time.time()
    completed = 0
    removed = 0
    errors = 0

    # Initialize the singleton before threads start so client construction is
    # deterministic. Its limiter coordinates request starts across all workers.
    get_client()
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="score") as pool:
        futures = [
            pool.submit(_score_one, resume_text, job, candidate_years)
            for job in jobs
        ]
        for future in as_completed(futures):
            job, result = future.result()
            completed += 1

            if result["score"] == 0:
                errors += 1

            # Only the coordinating thread writes to SQLite. Valid low scores
            # are removed immediately; score 0 is an error, not a fit rating.
            if target_url is None and 1 <= result["score"] <= LOW_SCORE_REMOVAL_MAX:
                conn.execute("DELETE FROM jobs WHERE url = ?", (job["url"],))
                removed += 1
            else:
                now = datetime.now(UTC).isoformat()
                conn.execute(
                    "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
                    (
                        result["score"],
                        f"{result['keywords']}\n{result['reasoning']}",
                        now,
                        job["url"],
                    ),
                )
            conn.commit()

            log.info(
                "[%d/%d] score=%d  %s",
                completed,
                len(jobs),
                result["score"],
                job.get("title", "?")[:60],
            )

    # A normal scoring run processes only pending jobs, so also remove any
    # valid low scores left by older versions or interrupted runs.
    if target_url is None:
        removed += _remove_stored_low_scores(conn)

    elapsed = time.time() - t0
    log.info(
        "Done: %d scored, %d removed in %.1fs (%.1f jobs/sec)",
        completed,
        removed,
        elapsed,
        completed / elapsed if elapsed > 0 else 0,
    )

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": completed,
        "removed": removed,
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
