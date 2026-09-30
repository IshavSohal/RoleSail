"""Local HTTP server for the interactive RoleSail dashboard."""

from __future__ import annotations

import copy
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from rolesail import config
from rolesail.dashboard_data import applied_view, load_dashboard_jobs
from rolesail.database import get_connection

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 32768
MAX_SETTINGS_BYTES = 262144
MAX_RESUME_BYTES = 1_000_000
# JSON may expand control-heavy text to six bytes per source byte.
MAX_RESUME_REQUEST_BYTES = 7_000_000
MAX_URL_LENGTH = 2048
MAX_TAILORING_QUEUE_SIZE = 100
TAILORING_HISTORY_SIZE = 20
WEB_DIST_DIR = Path(__file__).with_name("web_dist")
EXTERNAL_EMPLOYER_DOMAINS = {
    "konrad.com": {
        "name": "Konrad",
        "greenhouse_board": "konradgroup",
    },
    "salesforce.com": {
        "name": "Salesforce",
        "workday_employer": "salesforce",
    },
}
_settings_write_lock = threading.Lock()
_DELETE_SETTING = {"__rolesail_delete__": True}


class TailoringQueueFullError(RuntimeError):
    """Raised when the session-only tailoring queue reaches its bound."""


def _external_employer(url: str) -> dict | None:
    """Return canonical employer metadata for a recognized careers domain."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    employer = EXTERNAL_EMPLOYER_DOMAINS.get(hostname)
    if employer:
        return employer

    if hostname == "jobs.ashbyhq.com":
        segments = [unquote(segment).strip() for segment in parsed.path.split("/") if segment]
        if len(segments) >= 2 and segments[0]:
            board = segments[0]
            try:
                from rolesail.discovery.ats import load_ashby_companies

                configured = next(
                    (
                        company
                        for company in load_ashby_companies().values()
                        if str(company.get("board") or "").casefold() == board.casefold()
                    ),
                    None,
                )
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                configured = None
            return {
                "name": configured.get("name", board) if configured else board,
                "ashby_board": board,
                "provisional": True,
            }
    return None


def _backfill_external_employer_metadata(conn: sqlite3.Connection, url: str) -> None:
    """Normalize a direct upload and recover metadata from its backing ATS."""
    employer = _external_employer(url)
    if not employer:
        return

    name = employer["name"]
    if employer.get("provisional"):
        conn.execute(
            "UPDATE jobs SET company = COALESCE(company, ?), "
            "site = COALESCE(company, ?) WHERE url = ?",
            (name, name, url),
        )
        conn.commit()
        return

    conn.execute(
        "UPDATE jobs SET company = ?, site = ? WHERE url = ?",
        (name, name, url),
    )
    board = employer.get("greenhouse_board")
    job_id_match = re.search(r"_(\d+)(?:[/?#]|$)", url)
    if not board or not job_id_match:
        conn.commit()
        return

    try:
        from rolesail.discovery.greenhouse import fetch_company_jobs

        job = next(
            (
                candidate
                for candidate in fetch_company_jobs(board)
                if str(candidate.get("id")) == job_id_match.group(1)
            ),
            None,
        )
    except Exception:  # The page scrape is still usable if the ATS lookup fails.
        log.exception("Could not recover %s metadata from Greenhouse", name)
        conn.commit()
        return

    if job:
        location = job.get("location") or {}
        location_name = location.get("name") if isinstance(location, dict) else location
        conn.execute(
            "UPDATE jobs SET title = COALESCE(?, title), "
            "location = COALESCE(?, location) WHERE url = ?",
            (job.get("title"), location_name, url),
        )
    conn.commit()


def _is_nonnegative_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _deep_merge(existing: dict, incoming: dict) -> dict:
    """Merge editable settings while retaining keys unknown to the UI."""
    merged = copy.deepcopy(existing)
    for key, value in incoming.items():
        if value == _DELETE_SETTING:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 settings file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically replace a binary user-data file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _tectonic_executable() -> str | None:
    """Locate a usable Tectonic binary, including ~/.local/bin."""
    found = shutil.which("tectonic")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "tectonic"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return None


def _prepare_tex_for_tectonic(content: str) -> str:
    """Disable pdfTeX-only glyph mapping that breaks Tectonic's XeTeX engine."""
    prepared: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(r"\input{glyphtounicode}") or stripped.startswith(
            r"\pdfgentounicode"
        ):
            prepared.append(f"% {line}  % disabled for Tectonic/XeTeX")
        else:
            prepared.append(line)
    if content.endswith("\n"):
        prepared.append("")
    return "\n".join(prepared)


_VERBATIM_ENVIRONMENTS = {"verbatim", "Verbatim", "lstlisting", "minted"}
_BEGIN_ENVIRONMENT_RE = re.compile(r"\\begin\{([^{}]+)\}")
_END_ENVIRONMENT_RE = re.compile(r"\\end\{([^{}]+)\}")


def remove_latex_comments(content: str) -> tuple[str, int]:
    """Remove TeX comments without changing escaped percent signs.

    Newlines are retained so removing an inline comment cannot accidentally join
    tokens from adjacent lines. Common verbatim-style environments and inline
    ``\\verb`` expressions are preserved because percent signs are literal there.
    """
    cleaned: list[str] = []
    comments_removed = 0
    verbatim_environment: str | None = None

    for line in content.splitlines(keepends=True):
        if verbatim_environment is not None:
            cleaned.append(line)
            if any(
                match.group(1) == verbatim_environment
                for match in _END_ENVIRONMENT_RE.finditer(line)
            ):
                verbatim_environment = None
            continue

        comment_at: int | None = None
        index = 0
        while index < len(line):
            if line.startswith(r"\verb", index):
                delimiter_at = index + len(r"\verb")
                if delimiter_at < len(line) and line[delimiter_at] == "*":
                    delimiter_at += 1
                if delimiter_at < len(line):
                    delimiter = line[delimiter_at]
                    if not delimiter.isspace() and delimiter not in "\r\n":
                        closing_at = line.find(delimiter, delimiter_at + 1)
                        if closing_at >= 0:
                            index = closing_at + 1
                            continue

            if line[index] == "%":
                backslashes = 0
                before = index - 1
                while before >= 0 and line[before] == "\\":
                    backslashes += 1
                    before -= 1
                if backslashes % 2 == 0:
                    comment_at = index
                    break
            index += 1

        code = line if comment_at is None else line[:comment_at]
        if comment_at is not None:
            comments_removed += 1
            if line.endswith("\r\n"):
                newline = "\r\n"
            elif line.endswith("\n"):
                newline = "\n"
            elif line.endswith("\r"):
                newline = "\r"
            else:
                newline = ""
            code = code.rstrip(" \t") + newline
        cleaned.append(code)

        for match in _BEGIN_ENVIRONMENT_RE.finditer(code):
            if match.group(1) in _VERBATIM_ENVIRONMENTS:
                verbatim_environment = match.group(1)
                break

    return "".join(cleaned), comments_removed


def _compile_latex_resume(content: str) -> bytes:
    """Compile LaTeX with Tectonic and return the generated PDF."""
    executable = _tectonic_executable()
    if not executable:
        raise ValueError(
            "Tectonic is not installed. Install it from "
            "https://github.com/tectonic-typesetting/tectonic/releases "
            "or run: snap install tectonic"
        )

    with tempfile.TemporaryDirectory(prefix="rolesail-tex-") as directory:
        work_dir = Path(directory)
        source = work_dir / "resume.tex"
        source.write_text(_prepare_tex_for_tectonic(content), encoding="utf-8")
        command = [
            executable,
            "--outdir",
            str(work_dir),
            str(source),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("LaTeX compilation timed out after 5 minutes") from exc

        pdf_path = work_dir / "resume.pdf"
        if result.returncode != 0 or not pdf_path.exists():
            lines = []
            for part in (result.stdout, result.stderr):
                if not part:
                    continue
                for line in part.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("note: downloading"):
                        continue
                    lines.append(stripped)
            detail = "\n".join(lines)[-4000:] or "Tectonic did not produce a PDF"
            raise ValueError(f"LaTeX compilation failed:\n{detail}")

        pdf = pdf_path.read_bytes()
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("Tectonic produced an invalid PDF")
        return pdf


def load_dashboard_settings() -> dict:
    """Load settings for the browser without exposing the stored password."""
    profile = copy.deepcopy(config.load_profile())
    searches = copy.deepcopy(config.load_search_config())
    if not isinstance(profile, dict) or not isinstance(searches, dict):
        raise ValueError("Profile and search settings must contain objects")
    from rolesail.outreach.service import DEFAULT_SCHEDULE
    outreach = profile.setdefault("outreach", {})
    if isinstance(outreach, dict):
        configured_schedule = outreach.get("schedule")
        outreach["schedule"] = {
            **DEFAULT_SCHEDULE,
            **(configured_schedule if isinstance(configured_schedule, dict) else {}),
        }

    personal = profile.get("personal")
    password_configured = False
    if isinstance(personal, dict):
        password_configured = bool(personal.pop("password", ""))

    return {
        "profile": profile,
        "searches": searches,
        "password_configured": password_configured,
    }


def load_dashboard_resume(resume_format: str = "txt") -> dict:
    """Return the requested source resume for the Profile view."""
    paths = {
        "txt": config.RESUME_PATH,
        "tex": config.RESUME_TEX_PATH,
    }
    if resume_format not in paths:
        raise ValueError("Resume format must be txt or tex")
    path = paths[resume_format]
    if not path.exists():
        result = {
            "exists": False,
            "filename": path.name,
            "format": resume_format,
            "content": "",
        }
        if resume_format == "tex":
            result["pdf_available"] = False
        return result
    content = path.read_text(encoding="utf-8")
    return {
        "exists": True,
        "filename": path.name,
        "format": resume_format,
        "content": content,
        **(
            {"pdf_available": config.RESUME_PDF_PATH.exists()}
            if resume_format == "tex"
            else {}
        ),
    }


def save_dashboard_resume(
    filename: object,
    content: object,
    remove_comments: object = False,
) -> dict:
    """Validate and atomically save an uploaded text or LaTeX resume.

    LaTeX uploads are compiled with Tectonic first. On compile failure, neither
    resume.tex nor resume.pdf is replaced, so the previous PDF remains.
    """
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("Resume filename is required")
    clean_name = filename.strip()
    suffix = Path(clean_name).suffix.lower()
    if Path(clean_name).name != clean_name or suffix not in {".txt", ".tex"}:
        raise ValueError("Resume must be a .txt or .tex file")
    if not isinstance(content, str):
        raise ValueError("Resume content must be text")
    if "\x00" in content:
        raise ValueError("Resume contains invalid null characters")
    if not content.strip():
        raise ValueError("Resume cannot be empty")
    if len(content.encode("utf-8")) > MAX_RESUME_BYTES:
        raise ValueError("Resume must be 1 MB or smaller")
    if not isinstance(remove_comments, bool):
        raise ValueError("Remove comments must be a boolean")

    comments_removed = 0
    if suffix == ".tex" and remove_comments:
        content, comments_removed = remove_latex_comments(content)

    pdf = _compile_latex_resume(content) if suffix == ".tex" else None
    with _settings_write_lock:
        target = config.RESUME_PATH if suffix == ".txt" else config.RESUME_TEX_PATH
        _atomic_write(target, content)
        if pdf is not None:
            _atomic_write_bytes(config.RESUME_PDF_PATH, pdf)
    result = load_dashboard_resume(suffix.removeprefix("."))
    if suffix == ".tex":
        result["comments_removed"] = comments_removed
    return result


def _validate_profile(profile: object) -> dict:
    if not isinstance(profile, dict):
        raise ValueError("Profile must be a JSON object")
    for section in (
        "personal",
        "work_authorization",
        "compensation",
        "experience",
        "skills_boundary",
        "resume_facts",
        "eeo_voluntary",
        "availability",
        "outreach",
    ):
        if section in profile and not isinstance(profile[section], dict):
            raise ValueError(f"Profile section '{section}' must be an object")

    for section, keys in {
        "skills_boundary": ("programming_languages", "frameworks", "tools"),
        "resume_facts": (
            "preserved_companies",
            "preserved_projects",
            "real_metrics",
        ),
    }.items():
        values = profile.get(section, {})
        for key in keys:
            if key in values and not isinstance(values[key], list):
                raise ValueError(f"Profile field '{section}.{key}' must be a list")
            if key in values and not all(
                isinstance(item, str) for item in values[key]
            ):
                raise ValueError(
                    f"Profile field '{section}.{key}' must contain only text"
                )

    outreach = profile.get("outreach", {})
    if "writing_samples" in outreach:
        samples = outreach["writing_samples"]
        if not isinstance(samples, list) or not all(isinstance(item, str) for item in samples):
            raise ValueError("Profile field 'outreach.writing_samples' must contain only text")
        if len(samples) > 10 or any(len(item) > 5000 for item in samples):
            raise ValueError("Provide no more than 10 writing samples of at most 5,000 characters each")
    if "signature" in outreach and not isinstance(outreach["signature"], str):
        raise ValueError("Profile field 'outreach.signature' must be text")
    schedule = outreach.get("schedule", {})
    if schedule and not isinstance(schedule, dict):
        raise ValueError("Profile field 'outreach.schedule' must be an object")
    if isinstance(schedule, dict):
        timezone_name = schedule.get("timezone")
        if timezone_name is not None:
            if not isinstance(timezone_name, str):
                raise ValueError("Outreach schedule timezone must be text")
            try:
                ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("Outreach schedule timezone is unknown") from exc
        weekdays = schedule.get("weekdays")
        if weekdays is not None and (
            not isinstance(weekdays, list)
            or not weekdays
            or any(not isinstance(day, int) or isinstance(day, bool) or day < 0 or day > 6 for day in weekdays)
        ):
            raise ValueError("Outreach schedule weekdays must contain integers from 0 through 6")
        for key in (
            "first_wave_size",
            "second_wave_delay_business_days",
            "min_spacing_minutes",
            "daily_limit",
        ):
            value = schedule.get(key)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                raise ValueError(f"Outreach schedule field '{key}' must be a positive integer")
        parsed_times = {}
        for key in ("send_window_start", "send_window_end"):
            value = schedule.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise ValueError(f"Outreach schedule field '{key}' must use HH:MM")
            parsed_times[key] = value
        if (
            parsed_times.get("send_window_start")
            and parsed_times.get("send_window_end")
            and parsed_times["send_window_start"] >= parsed_times["send_window_end"]
        ):
            raise ValueError("Outreach send window must end after it starts")

    for section, key in (
        ("compensation", "salary_expectation"),
        ("compensation", "salary_range_min"),
        ("compensation", "salary_range_max"),
        ("experience", "years_of_experience_total"),
    ):
        values = profile.get(section, {})
        if key not in values or values[key] == "":
            continue
        value = values[key]
        if isinstance(value, str):
            try:
                number = float(value)
            except ValueError as exc:
                raise ValueError(
                    f"Profile field '{section}.{key}' must be a number"
                ) from exc
            if not math.isfinite(number) or number < 0:
                raise ValueError(
                    f"Profile field '{section}.{key}' must be a non-negative number"
                )
            values[key] = int(number) if number.is_integer() else number
        elif not _is_nonnegative_finite_number(value):
            raise ValueError(
                f"Profile field '{section}.{key}' must be a non-negative number"
            )
    return profile


def _validate_searches(searches: object) -> dict:
    if not isinstance(searches, dict):
        raise ValueError("Search preferences must be a JSON object")

    queries = searches.get("queries", [])
    if not isinstance(queries, list):
        raise ValueError("Search queries must be a list")
    for query in queries:
        if (
            not isinstance(query, dict)
            or not isinstance(query.get("query"), str)
            or not query["query"].strip()
        ):
            raise ValueError("Each search query must include a title")
        tier = query.get("tier", 1)
        if isinstance(tier, str) and tier.isdigit():
            tier = int(tier)
            query["tier"] = tier
        if isinstance(tier, bool) or not isinstance(tier, int) or tier not in (1, 2, 3):
            raise ValueError("Search query tiers must be 1, 2, or 3")

    locations = searches.get("locations", [])
    if not isinstance(locations, list):
        raise ValueError("Search locations must be a list")
    for location in locations:
        if (
            not isinstance(location, dict)
            or not isinstance(location.get("location"), str)
            or not location["location"].strip()
        ):
            raise ValueError("Each search location must include a location")
        if isinstance(location.get("remote"), str):
            remote = location["remote"].strip().lower()
            if remote in {"true", "false"}:
                location["remote"] = remote == "true"
        if "remote" in location and not isinstance(location["remote"], bool):
            raise ValueError("Location remote values must be true or false")

    defaults = searches.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("Search defaults must be an object")
    for key in ("distance", "hours_old", "results_per_site"):
        if key not in defaults:
            continue
        value = defaults.get(key)
        if value == _DELETE_SETTING:
            continue
        if isinstance(value, str) and value.strip():
            try:
                number = float(value)
            except ValueError as exc:
                raise ValueError(
                    f"Search default '{key}' must be a non-negative number"
                ) from exc
            if not math.isfinite(number) or number < 0:
                raise ValueError(
                    f"Search default '{key}' must be a non-negative number"
                )
            value = int(number) if number.is_integer() else number
            defaults[key] = value
        if not _is_nonnegative_finite_number(value):
            raise ValueError(f"Search default '{key}' must be a non-negative number")

    for key in (
        "accept_remote_anywhere",
        "accept_unknown_locations",
        "greenhouse_location_filter",
        "ashby_location_filter",
        "lever_location_filter",
        "bigtech_location_filter",
    ):
        if key in searches and not isinstance(searches[key], bool):
            raise ValueError(f"Search preference '{key}' must be true or false")

    for key in (
        "allowed_countries",
        "location_accept",
        "location_reject_non_remote",
        "include_titles",
        "exclude_titles",
        "priority_titles",
        "boards",
    ):
        if key in searches and not isinstance(searches[key], list):
            raise ValueError(f"Search preference '{key}' must be a list")
        if key in searches and not all(
            isinstance(item, str) for item in searches[key]
        ):
            raise ValueError(
                f"Search preference '{key}' must contain only text"
            )
    return searches


def save_dashboard_profile(profile: object) -> dict:
    """Validate and atomically save profile settings."""
    incoming = _validate_profile(profile)
    with _settings_write_lock:
        existing = config.load_profile()
        merged = _deep_merge(existing, incoming)
        incoming_personal = incoming.get("personal", {})
        existing_personal = existing.get("personal", {})
        if isinstance(incoming_personal, dict):
            password = incoming_personal.get("password")
            merged_personal = merged.setdefault("personal", {})
            if password:
                if not isinstance(password, str):
                    raise ValueError("Password must be text")
                merged_personal["password"] = password
            elif isinstance(existing_personal, dict) and "password" in existing_personal:
                merged_personal["password"] = existing_personal["password"]
            else:
                merged_personal.pop("password", None)
        _atomic_write(
            config.PROFILE_PATH,
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        )
    return load_dashboard_settings()


def save_dashboard_searches(searches: object) -> dict:
    """Validate and atomically save job-search preferences."""
    incoming = _validate_searches(searches)
    with _settings_write_lock:
        existing = config.load_search_config()
        merged = _deep_merge(existing, incoming)
        _atomic_write(
            config.SEARCH_CONFIG_PATH,
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        )
    return load_dashboard_settings()


def normalize_job_url(raw_url: str) -> str:
    """Validate and normalize an externally supplied job URL."""
    if not isinstance(raw_url, str):
        raise ValueError("URL must be a string")
    raw_url = raw_url.strip()
    if not raw_url:
        raise ValueError("Enter a job URL")
    if len(raw_url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("URL must include a valid hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def _amazon_job_id(url: str) -> str | None:
    """Return the numeric job ID for an Amazon Jobs detail URL."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname != "amazon.jobs" and not hostname.endswith(".amazon.jobs"):
        return None
    match = re.search(r"/jobs/(\d+)(?:/|$)", parsed.path, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _external_workday_job_id(url: str) -> tuple[str, str] | None:
    """Return the Workday employer key and requisition ID for a vanity URL."""
    employer = _external_employer(url)
    employer_key = employer.get("workday_employer") if employer else None
    if not employer_key:
        return None
    match = re.search(r"/jobs/(JR\d+)(?:/|$)", urlparse(url).path, re.IGNORECASE)
    if not match:
        return None
    return employer_key, match.group(1).upper()


def load_dashboard_company_logo(
    raw_url: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[bytes, str] | None:
    """Return a job's cached company logo, downloading it on first use."""
    url = normalize_job_url(raw_url)
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT company, company_logo FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row or not row["company"]:
        return None
    from rolesail.company_logos import company_logo_candidates, load_company_logo

    for source_url in company_logo_candidates(row["company"], row["company_logo"]):
        logo = load_company_logo(row["company"], source_url)
        if logo:
            if source_url != row["company_logo"]:
                conn.execute(
                    "UPDATE jobs SET company_logo = ? WHERE company = ? "
                    "AND (company_logo IS NULL OR company_logo = ?)",
                    (source_url, row["company"], row["company_logo"]),
                )
                conn.commit()
            return logo
    return None


def import_external_job(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Insert an external job URL and return its import state."""
    url = normalize_job_url(raw_url)
    conn = conn or get_connection()
    existing = conn.execute(
        "SELECT url, title, strategy, full_description, detail_scraped_at, "
        "detail_error, fit_score "
        "FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if existing:
        display_title = existing["title"]
        employer = _external_employer(url)
        if employer and employer.get("provisional"):
            company = employer["name"]
            placeholder = "Imported job from jobs.ashbyhq.com"
            if display_title == placeholder:
                display_title = f"Imported job from {company}"
            conn.execute(
                "UPDATE jobs SET company = CASE "
                "WHEN company IS NULL OR company = 'jobs.ashbyhq.com' THEN ? "
                "ELSE company END, site = CASE WHEN site = 'jobs.ashbyhq.com' "
                "THEN ? ELSE site END, title = CASE "
                "WHEN title = 'Imported job from jobs.ashbyhq.com' THEN ? "
                "ELSE title END WHERE url = ?",
                (company, company, display_title, url),
            )
            conn.commit()
        should_enrich = not bool(existing["full_description"])
        should_retry_score = (
            existing["strategy"] == "external_upload"
            and bool(existing["full_description"])
            and existing["fit_score"] == 0
        )
        if should_enrich or should_retry_score:
            conn.execute(
                "UPDATE jobs SET detail_scraped_at = CASE WHEN ? THEN NULL ELSE detail_scraped_at END, "
                "detail_error = CASE WHEN ? THEN NULL ELSE detail_error END, fit_score = NULL, "
                "score_reasoning = NULL, scored_at = NULL, discovery_status = 'accepted', "
                "discovery_rejection_reason = NULL "
                "WHERE url = ?",
                (should_enrich, should_enrich, url),
            )
            conn.commit()
        return {
            "created": False,
            "url": url,
            "title": display_title,
            "status": (
                "pending"
                if should_enrich
                else "scoring"
                if should_retry_score
                else job_import_status(url, conn)["status"]
            ),
            "enrichment_pending": should_enrich or should_retry_score,
        }

    hostname = urlparse(url).hostname or "external"
    employer = _external_employer(url)
    company = employer["name"] if employer else None
    site = company or hostname
    title = f"Imported job from {company or hostname}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, strategy, discovered_at, application_url) "
        "VALUES (?, ?, ?, ?, 'external_upload', ?, ?)",
        (url, title, company, site, now, url),
    )
    conn.commit()
    return {
        "created": True,
        "url": url,
        "title": title,
        "status": "pending",
        "enrichment_pending": True,
    }


def job_import_status(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Return the current enrichment status for an imported URL."""
    url = normalize_job_url(raw_url)
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT url, title, site, strategy, full_description, detail_scraped_at, "
        "detail_error, fit_score "
        "FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"url": url, "status": "missing"}
    if row["detail_error"]:
        status = "error"
    elif row["detail_scraped_at"] and row["full_description"]:
        from rolesail.config import get_tier

        needs_score = row["strategy"] == "external_upload" and get_tier() >= 2
        status = "scoring" if needs_score and row["fit_score"] is None else "complete"
    elif row["detail_scraped_at"]:
        status = "partial"
    else:
        status = "pending"
    return {
        "url": url,
        "status": status,
        "title": row["title"],
        "site": row["site"],
        "score": row["fit_score"],
        "error": row["detail_error"],
    }


def mark_job_applied(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Mark an existing dashboard job as manually applied."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT title, applied_at FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"updated": False, "url": url, "status": "missing"}

    applied_at = row["applied_at"] or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET applied_at = ?, "
        "apply_status = COALESCE(apply_status, 'manually_applied') WHERE url = ?",
        (applied_at, url),
    )
    conn.commit()
    from rolesail.outreach.service import enqueue_for_job
    outreach = enqueue_for_job(url, conn, reapplied=not bool(row["applied_at"]))
    has_email_draft = conn.execute(
        "SELECT 1 FROM outreach_recipients r "
        "JOIN outreach_batches b ON b.id = r.batch_id "
        "WHERE b.job_url = ? AND "
        "(r.gmail_draft_id IS NOT NULL OR r.apollo_message_id IS NOT NULL) LIMIT 1",
        (url,),
    ).fetchone() is not None
    return {
        "updated": True,
        "url": url,
        "title": row["title"],
        "status": "applied",
        "applied_at": applied_at,
        "applied_tab": applied_view(applied_at, has_email_draft),
        "outreach": outreach,
    }


def unmark_job_applied(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Return an applied dashboard job to the active queue."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT title FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"updated": False, "url": url, "status": "missing"}

    conn.execute(
        "UPDATE jobs SET applied_at = NULL, apply_status = NULL WHERE url = ?",
        (url,),
    )
    conn.commit()
    from rolesail.outreach.service import cancel_for_job
    cancel_for_job(url, conn)
    return {
        "updated": True,
        "url": url,
        "title": row["title"],
        "status": "active",
        "applied_at": None,
    }


def load_tailored_artifact(
    raw_url: str,
    kind: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[Path, bytes, str]:
    """Load a generated artifact for a job without accepting filesystem paths."""
    if kind not in {"tex", "pdf", "report"}:
        raise ValueError("Artifact kind must be tex, pdf, or report")
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT tailored_resume_path FROM jobs WHERE url = ?",
        (raw_url,),
    ).fetchone()
    if not row or not row["tailored_resume_path"]:
        raise FileNotFoundError("Tailored resume not found")
    tex_path = Path(row["tailored_resume_path"]).resolve()
    tailored_root = config.TAILORED_DIR.resolve()
    if tex_path.parent != tailored_root:
        raise PermissionError("Stored artifact path is outside the tailored resume directory")
    paths = {
        "tex": tex_path,
        "pdf": tex_path.with_suffix(".pdf"),
        "report": tex_path.with_name(f"{tex_path.stem}_REPORT.json"),
    }
    path = paths[kind]
    if not path.is_file():
        raise FileNotFoundError(f"Tailored {kind} artifact not found")
    content_types = {
        "tex": "application/x-tex; charset=utf-8",
        "pdf": "application/pdf",
        "report": "application/json; charset=utf-8",
    }
    return path, path.read_bytes(), content_types[kind]


def clear_tailored_resume(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Remove tailored-resume artifacts for a job and clear its tailored DB fields."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT tailored_resume_path FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"cleared": False, "url": url, "status": "missing"}
    stored = (row["tailored_resume_path"] or "").strip()
    if not stored:
        return {"cleared": False, "url": url, "status": "not_tailored"}

    stored_path = Path(stored).resolve()
    tailored_root = config.TAILORED_DIR.resolve()
    if stored_path.parent != tailored_root:
        raise PermissionError("Stored artifact path is outside the tailored resume directory")

    siblings = (
        stored_path.with_suffix(".tex"),
        stored_path.with_suffix(".pdf"),
        stored_path.with_suffix(".txt"),
        stored_path.with_name(f"{stored_path.stem}_REPORT.json"),
        stored_path.with_name(f"{stored_path.stem}_JOB.txt"),
    )
    deleted_files: list[str] = []
    for path in siblings:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted_files.append(str(path))

    conn.execute(
        "UPDATE jobs SET tailored_resume_path = NULL, tailored_at = NULL WHERE url = ?",
        (url,),
    )
    conn.commit()
    return {
        "cleared": True,
        "url": url,
        "status": "cleared",
        "deleted_files": deleted_files,
    }


def delete_job(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Delete a job posting from the dashboard database."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT title FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"deleted": False, "url": url, "status": "missing"}

    conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
    conn.commit()
    return {
        "deleted": True,
        "url": url,
        "title": row["title"],
        "status": "deleted",
    }


def _enrich_external_amazon_job(conn: sqlite3.Connection, url: str) -> bool:
    """Use Amazon's public JSON endpoint for a complete manual import."""
    job_id = _amazon_job_id(url)
    if not job_id:
        return False

    from rolesail.discovery.greenhouse import (
        _normalize_description,
        fetch_amazon_job,
    )

    job = fetch_amazon_job(job_id)
    if not job or not job.get("content_is_full"):
        return False
    full_description = _normalize_description(job.get("content"))
    if len(full_description) < 200:
        return False

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET title = COALESCE(?, title), company = 'Amazon', "
        "salary = COALESCE(?, salary), description = ?, location = COALESCE(?, location), "
        "site = 'Amazon', full_description = ?, application_url = COALESCE(?, ?), "
        "posted_at = COALESCE(?, posted_at), detail_scraped_at = ?, detail_error = NULL "
        "WHERE url = ?",
        (
            job.get("title"),
            job.get("salary"),
            full_description[:500],
            job.get("location"),
            full_description,
            job.get("application_url"),
            url,
            job.get("posted_at"),
            now,
            url,
        ),
    )
    conn.commit()
    return True


def _enrich_external_workday_job(conn: sqlite3.Connection, url: str) -> bool:
    """Resolve a recognized vanity careers URL through its Workday API."""
    identity = _external_workday_job_id(url)
    if not identity:
        return False
    employer_key, job_id = identity

    from rolesail.discovery.workday import (
        load_employers,
        strip_html,
        workday_detail,
        workday_search,
    )

    employer = load_employers().get(employer_key)
    if not employer:
        return False
    search = workday_search(employer, job_id, limit=20)
    posting = next(
        (
            candidate
            for candidate in search.get("jobPostings", [])
            if job_id in candidate.get("bulletFields", [])
            or job_id in candidate.get("externalPath", "").upper()
        ),
        None,
    )
    if not posting or not posting.get("externalPath"):
        return False

    info = workday_detail(employer, posting["externalPath"]).get(
        "jobPostingInfo", {}
    )
    if str(info.get("jobReqId", "")).upper() != job_id:
        return False
    full_description = strip_html(info.get("jobDescription", ""))
    if len(full_description) < 200:
        return False

    locations = [info.get("location"), *(info.get("additionalLocations") or [])]
    location = "; ".join(
        str(item).strip() for item in locations if str(item or "").strip()
    ) or None
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET title = COALESCE(?, title), company = ?, site = ?, "
        "description = ?, location = COALESCE(?, location), full_description = ?, "
        "application_url = COALESCE(?, ?), posted_at = COALESCE(?, posted_at), "
        "detail_scraped_at = ?, detail_error = NULL WHERE url = ?",
        (
            info.get("title") or posting.get("title"),
            employer["name"],
            employer["name"],
            full_description[:500],
            location,
            full_description,
            info.get("externalUrl"),
            url,
            info.get("startDate"),
            now,
            url,
        ),
    )
    conn.commit()
    return True


def enrich_external_job(url: str) -> None:
    """Enrich and, when configured, score one imported URL."""
    from rolesail.enrichment.detail import scrape_site_batch

    conn = get_connection()
    row = conn.execute(
        "SELECT title, site, full_description, detail_error FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return

    from rolesail.usage import usage_context
    try:
        if not row["full_description"] or row["detail_error"]:
            with usage_context(stage="enrich"):
                enriched = _enrich_external_workday_job(conn, url)
                if not enriched:
                    enriched = _enrich_external_amazon_job(conn, url)
                if not enriched:
                    scrape_site_batch(
                        conn,
                        row["site"] or "external",
                        [(url, row["title"])],
                        delay=0,
                    )
    except Exception as exc:
        log.exception("External job enrichment failed for %s", url)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? WHERE url = ?",
            (str(exc)[:500], now, url),
        )
        conn.commit()
        return

    _backfill_external_employer_metadata(conn, url)

    location_row = conn.execute(
        "SELECT location FROM jobs WHERE url = ? AND strategy = 'external_upload'",
        (url,),
    ).fetchone()
    if location_row:
        location = location_row["location"]
        if location and not config.location_is_allowed(location):
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE jobs SET discovery_status = 'rejected', "
                "discovery_rejection_reason = 'outside_allowed_countries', "
                "discovery_checked_at = ? WHERE url = ?",
                (now, url),
            )
            conn.commit()
            return
        conn.execute(
            "UPDATE jobs SET discovery_status = 'accepted', "
            "discovery_rejection_reason = NULL WHERE url = ? "
            "AND (discovery_rejection_reason IS NULL "
            "OR discovery_rejection_reason = 'outside_allowed_countries')",
            (url,),
        )
        conn.commit()

    from rolesail.config import get_tier

    if get_tier() < 2:
        return

    try:
        from rolesail.scoring.scorer import run_scoring

        with usage_context(stage="score"):
            run_scoring(target_url=url, workers=1)
    except Exception as exc:
        # Scoring configuration/runtime failures should not make a successful
        # detail scrape look like an enrichment failure.
        log.exception("External job scoring failed for %s", url)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET fit_score = 0, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (f"Scoring error: {str(exc)[:500]}", now, url),
        )
        conn.commit()


def _execute_discovery(server: DashboardHTTPServer, workers: int) -> None:
    """Run discovery (then enrich + score when LLM is configured)."""
    from rolesail.config import get_tier
    from rolesail.pipeline import _run_discover, _run_enrich, _run_score

    try:
        result = _run_discover(workers=workers)
        if get_tier() >= 2:
            enrich_result = _run_enrich(workers=workers)
            if isinstance(enrich_result, dict) and str(
                enrich_result.get("status", "")
            ).startswith("error"):
                raise RuntimeError(enrich_result["status"])
            score_result = _run_score()
            if isinstance(score_result, dict) and str(
                score_result.get("status", "")
            ).startswith("error"):
                raise RuntimeError(score_result["status"])
            result = {**result, "scored": True}
        else:
            result = {**result, "scored": False}
    except Exception as exc:
        log.exception("Dashboard discovery failed")
        with server.discovery_lock:
            server.discovery_state = {
                **server.discovery_state,
                "status": "error",
                "error": str(exc)[:500],
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        return

    with server.discovery_lock:
        server.discovery_state = {
            **server.discovery_state,
            "status": "complete",
            "result": result,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }


def start_discovery(server: DashboardHTTPServer, workers: int = 3) -> dict:
    """Start one background discovery run, rejecting overlapping runs."""
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ValueError("Workers must be an integer between 1 and 8")

    with server.discovery_lock:
        if server.discovery_state["status"] == "running":
            return dict(server.discovery_state)
        server.discovery_state = {
            "status": "running",
            "workers": workers,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        state = dict(server.discovery_state)

    server.discovery_pool.submit(_execute_discovery, server, workers)
    return state


def _execute_dashboard_pipeline(server: DashboardHTTPServer, run_id: str, workers: int) -> None:
    """Run the dashboard's one-click discover/enrich/score workflow."""
    from rolesail.pipeline import run_pipeline
    from rolesail.usage import update_run

    try:
        result = run_pipeline(
            stages=["discover", "enrich", "score"],
            workers=workers,
            score_workers=workers,
            run_id=run_id,
        )
        errors = result.get("errors") or {}
        status = "partial" if errors else "complete"
        update_run(run_id, status=status, result=result,
                   error=json.dumps(errors) if errors else None)
    except Exception as exc:
        log.exception("Dashboard pipeline failed")
        update_run(run_id, status="error", error=str(exc))


def start_dashboard_pipeline(server: DashboardHTTPServer, workers: int = 3) -> dict:
    """Start one persistent discover/enrich/score run."""
    from rolesail.usage import create_run, get_run

    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ValueError("Workers must be an integer between 1 and 8")
    with server.pipeline_lock:
        latest = get_run()
        if latest and latest["status"] == "running":
            raise RuntimeError("A pipeline run is already in progress")
        run = create_run(["discover", "enrich", "score"])
        server.pipeline_pool.submit(_execute_dashboard_pipeline, server, run["id"], workers)
        return run


def _tailoring_target_error(
    target_url: str,
    replace_existing: bool = False,
) -> str | None:
    """Return why a queued job cannot be tailored, or None when eligible."""
    row = get_connection().execute(
        "SELECT applied_at, full_description, tailored_resume_path, "
        "COALESCE(tailor_attempts, 0) AS tailor_attempts "
        "FROM jobs WHERE url = ?",
        (target_url,),
    ).fetchone()
    if not row:
        return "Job not found"
    if row["applied_at"]:
        return "This job is already marked as applied"
    if not row["full_description"]:
        return "This job needs a full description before tailoring"
    if row["tailored_resume_path"] and not replace_existing:
        return "This job already has a tailored resume"
    if row["tailor_attempts"] >= 5:
        return "This job has reached the tailoring attempt limit"
    return None


def _tailoring_status_locked(server: DashboardHTTPServer) -> dict:
    """Build a JSON-safe queue snapshot while ``tailoring_lock`` is held."""
    if server.tailoring_current is not None:
        status = "running"
    elif server.tailoring_queue:
        status = "queued"
    else:
        status = "idle"

    queued = []
    for position, request in enumerate(server.tailoring_queue, start=1):
        queued.append({**copy.deepcopy(request), "queue_position": position})

    return {
        "status": status,
        "current": copy.deepcopy(server.tailoring_current),
        "queued": queued,
        "queue_length": len(queued),
        "recent": copy.deepcopy(list(server.tailoring_recent)),
    }


def tailoring_status(server: DashboardHTTPServer) -> dict:
    """Return the current session-only tailoring queue state."""
    with server.tailoring_lock:
        return _tailoring_status_locked(server)


def _run_tailoring_request(request: dict) -> tuple[str, dict | None, str | None]:
    """Execute one request and convert stale individual jobs into skips."""
    target_url = request["target_url"]
    if target_url:
        reason = _tailoring_target_error(
            target_url,
            request.get("replace_existing", False),
        )
        if reason:
            return "skipped", {"reason": reason}, None

    try:
        from rolesail.scoring.tailor import TailoringCancelled, run_tailoring
        from rolesail.usage import usage_context

        with usage_context(stage="tailor"):
            result = run_tailoring(
                min_score=request["min_score"],
                limit=request["limit"],
                validation_mode=request["validation_mode"],
                target_url=target_url,
                replace_existing=request.get("replace_existing", False),
                cancel_check=lambda: request.get("cancel_requested", False),
            )
        return "complete", result, None
    except TailoringCancelled:
        return "cancelled", None, None
    except Exception as exc:
        log.exception("Dashboard tailoring request failed")
        return "error", None, str(exc)[:500]


def _drain_tailoring_queue(server: DashboardHTTPServer) -> None:
    """Process queued tailoring requests in FIFO order on one worker thread."""
    while True:
        with server.tailoring_lock:
            if server.tailoring_stopping or not server.tailoring_queue:
                server.tailoring_processor_running = False
                server.tailoring_current = None
                return

            request = server.tailoring_queue.popleft()
            request["status"] = "running"
            request["started_at"] = datetime.now(timezone.utc).isoformat()
            server.tailoring_current = request

        try:
            status, result, error = _run_tailoring_request(request)
        except Exception as exc:
            log.exception("Dashboard tailoring queue worker failed")
            status, result, error = "error", None, str(exc)[:500]
        finished_at = datetime.now(timezone.utc).isoformat()

        with server.tailoring_lock:
            request["status"] = status
            request["result"] = result
            request["error"] = error
            request["finished_at"] = finished_at
            server.tailoring_recent.appendleft(copy.deepcopy(request))
            server.tailoring_current = None


def start_tailoring(
    server: DashboardHTTPServer,
    min_score: int = 7,
    limit: int = 20,
    validation_mode: str = "normal",
    target_url: str | None = None,
    replace_existing: bool = False,
) -> dict:
    """Enqueue a tailoring request for the dashboard's single FIFO worker."""
    if not isinstance(min_score, int) or isinstance(min_score, bool) or not 1 <= min_score <= 10:
        raise ValueError("Minimum score must be an integer between 1 and 10")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("Tailoring limit must be an integer between 1 and 100")
    if validation_mode not in {"strict", "normal", "lenient"}:
        raise ValueError("Validation mode must be strict, normal, or lenient")
    if not isinstance(replace_existing, bool):
        raise ValueError("Replace existing must be a boolean")
    if replace_existing and target_url is None:
        raise ValueError("Replace existing is only supported for a single job")
    if target_url is not None:
        if not isinstance(target_url, str) or not target_url.strip():
            raise ValueError("Job URL is required")
        target_url = target_url.strip()
        reason = _tailoring_target_error(target_url, replace_existing)
        if reason:
            raise ValueError(reason)

    with server.tailoring_lock:
        if server.tailoring_stopping:
            raise RuntimeError("The tailoring queue is shutting down")

        outstanding = [
            request
            for request in ([server.tailoring_current] + list(server.tailoring_queue))
            if request is not None
        ]
        if target_url:
            duplicate = next(
                (request for request in outstanding if request["target_url"] == target_url),
                None,
            )
            if duplicate:
                position = 0
                if duplicate["status"] == "queued":
                    position = list(server.tailoring_queue).index(duplicate) + 1
                return {
                    **copy.deepcopy(duplicate),
                    "deduplicated": True,
                    "queue_position": position,
                }
        elif any(request["kind"] == "batch" for request in outstanding):
            raise RuntimeError("A bulk tailoring request is already active or queued")

        if len(server.tailoring_queue) >= MAX_TAILORING_QUEUE_SIZE:
            raise TailoringQueueFullError("The tailoring queue is full")

        request = {
            "id": uuid.uuid4().hex,
            "kind": "job" if target_url else "batch",
            "status": "queued",
            "min_score": min_score,
            "limit": limit,
            "validation_mode": validation_mode,
            "target_url": target_url,
            "replace_existing": replace_existing,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "cancel_requested": False,
        }
        server.tailoring_queue.append(request)
        queue_position = len(server.tailoring_queue)
        should_start_processor = not server.tailoring_processor_running
        if should_start_processor:
            server.tailoring_processor_running = True
        response = {
            **copy.deepcopy(request),
            "deduplicated": False,
            "queue_position": queue_position,
        }

    if should_start_processor:
        try:
            server.tailoring_pool.submit(_drain_tailoring_queue, server)
        except RuntimeError:
            with server.tailoring_lock:
                server.tailoring_processor_running = False
                try:
                    server.tailoring_queue.remove(request)
                except ValueError:
                    pass
            raise
    return response


def cancel_tailoring(server: DashboardHTTPServer, target_url: str) -> dict:
    """Cancel an outstanding single-job tailoring request."""
    if not isinstance(target_url, str) or not target_url.strip():
        raise ValueError("Job URL is required")
    target_url = target_url.strip()

    with server.tailoring_lock:
        current = server.tailoring_current
        if current is not None and current.get("target_url") == target_url:
            current["cancel_requested"] = True
            return {
                "id": current["id"],
                "url": target_url,
                "status": "cancelling",
            }

        queued = next(
            (request for request in server.tailoring_queue if request.get("target_url") == target_url),
            None,
        )
        if queued is not None:
            server.tailoring_queue.remove(queued)
            queued["status"] = "cancelled"
            queued["finished_at"] = datetime.now(timezone.utc).isoformat()
            server.tailoring_recent.appendleft(copy.deepcopy(queued))
            return {
                "id": queued["id"],
                "url": target_url,
                "status": "cancelled",
            }

    raise ValueError("No active tailoring request was found for this job")


def _run_outreach_dispatcher(stop_event: threading.Event, wake_event: threading.Event) -> None:
    """Run the restart-safe local outreach dispatcher."""
    from rolesail.outreach.service import (
        dispatch_due_outreach,
        recover_outreach_dispatcher,
        refresh_inflight_outreach,
    )

    try:
        recover_outreach_dispatcher()
    except Exception:
        log.exception("Could not recover scheduled outreach")
    while not stop_event.is_set():
        try:
            refresh_inflight_outreach()
            dispatch_due_outreach()
        except Exception:
            log.exception("Scheduled outreach dispatcher iteration failed")
        wake_event.wait(30)
        wake_event.clear()


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded localhost server with a bounded enrichment pool."""

    daemon_threads = True

    def __init__(self, server_address, handler_class):
        super().__init__(server_address, handler_class)
        from rolesail.usage import recover_interrupted_runs
        recover_interrupted_runs()
        self.enrichment_pool = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="rolesail-enrich",
        )
        self.discovery_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rolesail-discovery",
        )
        self.tailoring_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rolesail-tailoring",
        )
        self.pipeline_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rolesail-pipeline",
        )
        self.outreach_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rolesail-outreach",
        )
        self.pipeline_lock = threading.Lock()
        self.discovery_lock = threading.Lock()
        self.discovery_state = {
            "status": "idle",
            "workers": None,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        self.tailoring_lock = threading.Lock()
        self.tailoring_queue: deque[dict] = deque()
        self.tailoring_current: dict | None = None
        self.tailoring_recent: deque[dict] = deque(maxlen=TAILORING_HISTORY_SIZE)
        self.tailoring_processor_running = False
        self.tailoring_stopping = False
        self.render_lock = threading.Lock()
        self.outreach_dispatcher_stop = threading.Event()
        self.outreach_dispatcher_wake = threading.Event()
        self.outreach_dispatcher_thread: threading.Thread | None = None
        if os.environ.get("OUTREACH_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            from rolesail.outreach.service import prepare_batch, recover_reapplied_batches
            recover_reapplied_batches(get_connection())
            rows = get_connection().execute(
                "SELECT id FROM outreach_batches WHERE status IN ('queued', 'preparing')"
            ).fetchall()
            get_connection().execute(
                "UPDATE outreach_batches SET status = 'queued' WHERE status = 'preparing'"
            )
            get_connection().commit()
            for row in rows:
                self.outreach_pool.submit(prepare_batch, row["id"])
            self.outreach_dispatcher_thread = threading.Thread(
                target=_run_outreach_dispatcher,
                args=(self.outreach_dispatcher_stop, self.outreach_dispatcher_wake),
                name="rolesail-outreach-dispatcher",
                daemon=True,
            )
            self.outreach_dispatcher_thread.start()

    def server_close(self) -> None:
        self.outreach_dispatcher_stop.set()
        self.outreach_dispatcher_wake.set()
        if self.outreach_dispatcher_thread:
            self.outreach_dispatcher_thread.join(timeout=2)
        self.enrichment_pool.shutdown(wait=False, cancel_futures=True)
        self.discovery_pool.shutdown(wait=False, cancel_futures=True)
        with self.tailoring_lock:
            self.tailoring_stopping = True
            self.tailoring_queue.clear()
        self.tailoring_pool.shutdown(wait=False, cancel_futures=True)
        self.pipeline_pool.shutdown(wait=False, cancel_futures=True)
        self.outreach_pool.shutdown(wait=False, cancel_futures=True)
        super().server_close()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve the dashboard and its external-job API."""

    server: DashboardHTTPServer

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _read_json(self, max_bytes: int = MAX_REQUEST_BYTES) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > max_bytes:
            raise ValueError("Invalid request size")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("Content-Type must be application/json")
        payload = json.loads(self.rfile.read(content_length))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

    def _validate_origin(self) -> None:
        self._validate_local_host()
        origin = self.headers.get("Origin")
        if not origin:
            return
        origin_host = urlparse(origin).netloc.lower()
        request_host = self.headers.get("Host", "").lower()
        if not origin_host or origin_host != request_host:
            raise PermissionError("Cross-origin settings updates are not allowed")

    def _validate_local_host(self) -> None:
        request_host = self.headers.get("Host", "")
        hostname = urlparse(f"//{request_host}").hostname
        bound_host = str(self.server.server_address[0]).lower()
        allowed = {"127.0.0.1", "localhost", "::1", bound_host}
        if not hostname or hostname.lower() not in allowed:
            raise PermissionError("Settings are only available from localhost")

    def _send_web_asset(self, relative_path: str, *, shell: bool = False) -> None:
        """Serve a file from the packaged Vite build without path traversal."""
        try:
            relative = Path(relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(relative_path)
            root = WEB_DIST_DIR.resolve(strict=True)
            target = (root / relative).resolve(strict=True)
            target.relative_to(root)
            if not target.is_file():
                raise FileNotFoundError(relative_path)
        except (FileNotFoundError, RuntimeError, ValueError):
            self._send_json(404, {"error": "Dashboard asset not found"})
            return

        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type += "; charset=utf-8"
        cache_control = "no-cache" if shell else "public, max-age=31536000, immutable"
        self._send_bytes(200, target.read_bytes(), content_type, cache_control)

    def _send_spa_shell(self) -> None:
        self._send_web_asset("index.html", shell=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_spa_shell()
            return

        if parsed.path.startswith("/assets/"):
            self._send_web_asset(parsed.path.removeprefix("/"))
            return

        if parsed.path == "/api/jobs":
            try:
                self._validate_local_host()
                self._send_json(200, {"jobs": load_dashboard_jobs(get_connection())})
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except sqlite3.Error as exc:
                log.exception("Could not load dashboard jobs")
                self._send_json(500, {"error": f"Could not load jobs: {exc}"})
            return

        if parsed.path == "/api/jobs/company-logo":
            raw_url = parse_qs(parsed.query).get("url", [""])[0]
            try:
                logo = load_dashboard_company_logo(raw_url)
                if not logo:
                    self._send_json(404, {"error": "Company logo unavailable"})
                    return
                body, content_type = logo
                self._send_bytes(200, body, content_type)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/jobs/status":
            raw_url = parse_qs(parsed.query).get("url", [""])[0]
            try:
                self._send_json(200, job_import_status(raw_url))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/outreach":
            try:
                self._validate_local_host()
                from rolesail.outreach.gmail import connected_account
                from rolesail.outreach.service import get_batch, refresh_delivery_statuses
                query = parse_qs(parsed.query)
                identifier = query.get("batch_id", query.get("job_url", [""]))[0]
                batch = get_batch(identifier)
                if not batch:
                    self._send_json(404, {"error": "Outreach batch not found"})
                    return
                if batch["status"] == "sending":
                    batch = refresh_delivery_statuses(identifier)
                self._send_json(200, {"batch": batch, "gmail_account": connected_account()})
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except (ValueError, RuntimeError) as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/discovery/status":
            with self.server.discovery_lock:
                state = dict(self.server.discovery_state)
            self._send_json(200, state)
            return

        if parsed.path == "/api/tailoring/status":
            self._send_json(200, tailoring_status(self.server))
            return

        if parsed.path == "/api/pipeline/status":
            from rolesail.usage import get_run
            run_id = parse_qs(parsed.query).get("run_id", [None])[0]
            self._send_json(200, {"run": get_run(run_id)})
            return

        if parsed.path == "/api/usage/summary":
            from rolesail.usage import usage_summary
            query = parse_qs(parsed.query)
            self._send_json(200, usage_summary(
                query.get("run_id", [None])[0],
                stage=query.get("stage", [None])[0],
                provider=query.get("provider", [None])[0],
                model=query.get("model", [None])[0],
            ))
            return

        if parsed.path == "/api/usage/history":
            try:
                from rolesail.usage import usage_history
                query = parse_qs(parsed.query)
                self._send_json(200, {"entries": usage_history(
                    run_id=query.get("run_id", [None])[0],
                    stage=query.get("stage", [None])[0],
                    provider=query.get("provider", [None])[0],
                    model=query.get("model", [None])[0],
                    limit=int(query.get("limit", ["100"])[0]),
                )})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/settings/pricing":
            try:
                self._validate_local_host()
                from rolesail.usage import load_pricing
                self._send_json(200, load_pricing())
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            return

        if parsed.path == "/api/settings":
            try:
                self._validate_local_host()
                self._send_json(200, load_dashboard_settings())
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except (FileNotFoundError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/resume/pdf":
            try:
                self._validate_local_host()
                if (
                    not config.RESUME_TEX_PATH.exists()
                    or not config.RESUME_PDF_PATH.exists()
                ):
                    self._send_json(404, {"error": "Compiled LaTeX resume not found"})
                    return
                self._send_bytes(
                    200,
                    config.RESUME_PDF_PATH.read_bytes(),
                    "application/pdf",
                )
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except OSError as exc:
                self._send_json(500, {"error": f"Could not read resume PDF: {exc}"})
            return

        if parsed.path == "/api/resume":
            try:
                self._validate_local_host()
                resume_format = parse_qs(parsed.query).get("format", ["txt"])[0]
                self._send_json(200, load_dashboard_resume(resume_format))
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except (OSError, UnicodeError) as exc:
                self._send_json(500, {"error": f"Could not read resume: {exc}"})
            return

        if parsed.path == "/api/jobs/artifact":
            try:
                self._validate_local_host()
                query = parse_qs(parsed.query)
                path, body, content_type = load_tailored_artifact(
                    query.get("url", [""])[0],
                    query.get("kind", [""])[0],
                )
                disposition = query.get("disposition", ["download"])[0]
                if disposition not in {"inline", "download"}:
                    raise ValueError("Disposition must be inline or download")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                header_disposition = "inline" if disposition == "inline" else "attachment"
                self.send_header("Content-Disposition", f'{header_disposition}; filename="{path.name}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except (FileNotFoundError, OSError) as exc:
                self._send_json(404, {"error": str(exc)})
            return

        # BrowserRouter routes are handled by the SPA. API typos must remain
        # JSON 404s rather than receiving HTML.
        if not parsed.path.startswith("/api/") and parsed.path in {"/profile"}:
            self._send_spa_shell()
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {
            "/api/jobs",
            "/api/jobs/applied",
            "/api/jobs/delete",
            "/api/jobs/tailored/clear",
            "/api/discovery",
            "/api/tailoring",
            "/api/tailoring/job",
            "/api/tailoring/cancel",
            "/api/pipeline",
            "/api/outreach/prepare",
            "/api/outreach/preview",
            "/api/outreach/approve",
            "/api/outreach/gmail-drafts",
            "/api/outreach/reset-gmail-draft",
            "/api/outreach/redraft",
            "/api/outreach/retry",
            "/api/outreach/cancel",
            "/api/outreach/cancel-pending",
            "/api/outreach/clear",
            "/api/outreach/suppress",
        }:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            if path.startswith("/api/outreach/"):
                self._validate_origin()
            payload = self._read_json()
            if path == "/api/discovery":
                result = start_discovery(self.server, payload.get("workers", 3))
            elif path == "/api/pipeline":
                result = start_dashboard_pipeline(self.server, payload.get("workers", 3))
            elif path == "/api/tailoring":
                result = start_tailoring(
                    self.server,
                    payload.get("min_score", 7),
                    payload.get("limit", 20),
                    payload.get("validation_mode", "normal"),
                )
            elif path == "/api/tailoring/job":
                result = start_tailoring(
                    self.server,
                    min_score=1,
                    limit=1,
                    validation_mode=payload.get("validation_mode", "normal"),
                    target_url=payload.get("url"),
                    replace_existing=payload.get("replace_existing", False),
                )
            elif path == "/api/tailoring/cancel":
                result = cancel_tailoring(self.server, payload.get("url", ""))
            elif path == "/api/jobs/applied":
                applied = payload.get("applied", True)
                if not isinstance(applied, bool):
                    raise ValueError("Applied must be a boolean")
                result = (
                    mark_job_applied(payload.get("url", ""))
                    if applied
                    else unmark_job_applied(payload.get("url", ""))
                )
            elif path == "/api/jobs/delete":
                result = delete_job(payload.get("url", ""))
            elif path == "/api/jobs/tailored/clear":
                result = clear_tailored_resume(payload.get("url", ""))
            elif path.startswith("/api/outreach/"):
                from rolesail.outreach.service import (
                    approve_batch,
                    cancel_batch,
                    cancel_pending,
                    clear_cancelled_batch,
                    create_gmail_drafts,
                    enqueue_for_job,
                    prepare_batch,
                    preview_batch_schedule,
                    redraft_batch,
                    reset_uncertain_gmail_draft,
                    retry_batch,
                    suppress_recipient,
                )
                from rolesail.outreach.service import enabled as outreach_enabled

                if path == "/api/outreach/prepare":
                    identifier = payload.get("batch_id") or ""
                    if not identifier:
                        job_url = payload.get("job_url") or ""
                        if not outreach_enabled():
                            raise ValueError(
                                "Employee outreach is disabled. Set OUTREACH_ENABLED=true and restart RoleSail."
                            )
                        batch = enqueue_for_job(job_url)
                        if not batch:
                            raise ValueError(
                                "Outreach can only be prepared for a job marked as applied"
                            )
                        identifier = batch["id"]
                    self.server.outreach_pool.submit(prepare_batch, identifier)
                    result = {"status": "queued", "id": identifier}
                elif path == "/api/outreach/preview":
                    result = {
                        "schedule": preview_batch_schedule(
                            payload.get("batch_id", ""), payload.get("recipient_ids", [])
                        )
                    }
                elif path == "/api/outreach/approve":
                    result = approve_batch(
                        payload.get("batch_id", ""),
                        payload.get("recipients", []),
                        confirmed=payload.get("confirmed") is True,
                    )
                elif path == "/api/outreach/gmail-drafts":
                    result = create_gmail_drafts(
                        payload.get("batch_id", ""),
                        payload.get("recipients", []),
                        confirmed_account=payload.get("confirmed_account", ""),
                    )
                elif path == "/api/outreach/reset-gmail-draft":
                    result = reset_uncertain_gmail_draft(
                        payload.get("recipient_id", ""),
                        confirmed_no_draft=payload.get("confirmed_no_draft") is True,
                    )
                elif path == "/api/outreach/redraft":
                    result = redraft_batch(payload.get("batch_id", ""))
                elif path == "/api/outreach/retry":
                    result = retry_batch(payload.get("batch_id", ""))
                elif path == "/api/outreach/cancel":
                    result = cancel_batch(payload.get("batch_id", ""))
                elif path == "/api/outreach/cancel-pending":
                    result = cancel_pending(payload.get("batch_id", ""))
                elif path == "/api/outreach/clear":
                    result = clear_cancelled_batch(payload.get("batch_id", ""))
                else:
                    result = suppress_recipient(
                        payload.get("recipient_id", ""), payload.get("reason", "user")
                    )
                if path in {
                    "/api/outreach/approve",
                    "/api/outreach/retry",
                    "/api/outreach/cancel-pending",
                }:
                    self.server.outreach_dispatcher_wake.set()
            else:
                result = import_external_job(payload.get("url", ""))
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except TailoringQueueFullError as exc:
            self._send_json(429, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            log.exception("Could not import external job")
            self._send_json(500, {"error": f"Database error: {exc}"})
            return

        if path in {"/api/discovery", "/api/tailoring", "/api/tailoring/job", "/api/pipeline"}:
            self._send_json(202, result)
            return

        if path.startswith("/api/outreach/"):
            if path == "/api/outreach/preview":
                self._send_json(200, result)
            else:
                self._send_json(202 if path == "/api/outreach/prepare" else 200, {"batch": result})
            return

        if path == "/api/tailoring/cancel":
            self._send_json(200, result)
            return

        if path == "/api/jobs/applied":
            if result["updated"]:
                batch = result.get("outreach")
                if batch and batch.get("status") in {"queued", "failed"}:
                    from rolesail.outreach.service import prepare_batch
                    self.server.outreach_pool.submit(prepare_batch, batch["id"])
                self._send_json(200, result)
            else:
                self._send_json(404, {"error": "Job not found"})
            return

        if path == "/api/jobs/delete":
            if result["deleted"]:
                self._send_json(200, result)
            else:
                self._send_json(404, {"error": "Job not found"})
            return

        if path == "/api/jobs/tailored/clear":
            if result["cleared"]:
                self._send_json(200, result)
            elif result["status"] == "missing":
                self._send_json(404, {"error": "Job not found"})
            else:
                self._send_json(404, {"error": "Tailored resume not found"})
            return

        if result.get("enrichment_pending"):
            self.server.enrichment_pool.submit(enrich_external_job, result["url"])
            if result["created"]:
                self._send_json(201, result)
            else:
                result["message"] = "This job is already in the dashboard; enrichment was retried"
                self._send_json(202, result)
        else:
            result["message"] = "This job is already in the dashboard"
            self._send_json(200, result)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if path not in {
            "/api/settings/profile",
            "/api/settings/searches",
            "/api/resume",
            "/api/settings/pricing",
        }:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            self._validate_origin()
            max_bytes = (
                MAX_RESUME_REQUEST_BYTES
                if path == "/api/resume"
                else MAX_SETTINGS_BYTES
            )
            payload = self._read_json(max_bytes)
            if path == "/api/settings/profile":
                result = save_dashboard_profile(payload.get("profile"))
            elif path == "/api/resume":
                result = save_dashboard_resume(
                    payload.get("filename"),
                    payload.get("content"),
                    payload.get("remove_comments", False),
                )
            elif path == "/api/settings/pricing":
                from rolesail.usage import save_pricing
                result = save_pricing(payload.get("overrides"))
            else:
                result = save_dashboard_searches(payload.get("searches"))
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        except (
            FileNotFoundError,
            ValueError,
            json.JSONDecodeError,
            yaml.YAMLError,
        ) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except OSError as exc:
            log.exception("Could not save dashboard settings")
            self._send_json(500, {"error": f"Could not save settings: {exc}"})
            return

        self._send_json(200, result)

    def log_message(self, format: str, *args) -> None:
        log.debug("Dashboard: " + format, *args)


def serve_dashboard(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Run the interactive dashboard until interrupted."""
    server = DashboardHTTPServer((host, port), DashboardRequestHandler)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"RoleSail dashboard: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
