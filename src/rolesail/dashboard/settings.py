"""Dashboard profile, search, and resume settings services."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from rolesail import config

MAX_RESUME_BYTES = 1_000_000
_settings_write_lock = threading.Lock()
_DELETE_SETTING = {"__rolesail_delete__": True}

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

