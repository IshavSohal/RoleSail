"""RoleSail configuration: paths, platform detection, user data."""

import os
import platform
import re
import shutil
import sqlite3
from pathlib import Path

# User data directory — all user-specific files live here. ROLESAIL_DIR is
# preferred; APPLYPILOT_DIR remains supported during the rename transition.
_LEGACY_DIR_ENV = os.environ.get("APPLYPILOT_DIR")
APP_DIR = Path(
    os.environ.get("ROLESAIL_DIR")
    or _LEGACY_DIR_ENV
    or Path.home() / ".rolesail"
)
LEGACY_APP_DIR = Path(_LEGACY_DIR_ENV or Path.home() / ".applypilot")

# Core paths
DB_PATH = APP_DIR / "rolesail.db"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_TEX_PATH = APP_DIR / "resume.tex"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"
GMAIL_TOKEN_PATH = APP_DIR / "gmail_oauth_token.json"
GMAIL_ACCOUNT_PATH = APP_DIR / "gmail_account.json"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"
COMPANY_LOGO_DIR = APP_DIR / "company_logos"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def migrate_legacy_app_dir(
    app_dir: Path = APP_DIR,
    legacy_app_dir: Path = LEGACY_APP_DIR,
) -> bool:
    """Copy legacy ApplyPilot data into RoleSail without deleting the source.

    Existing RoleSail files always win. The legacy database is copied to the
    new filename so it remains usable after the package rename.
    """
    migrated = False

    if app_dir != legacy_app_dir and legacy_app_dir.is_dir():
        for source in legacy_app_dir.rglob("*"):
            destination = app_dir / source.relative_to(legacy_app_dir)
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif (
                source.is_file()
                and source.name not in {"applypilot.db", "applypilot.db-shm", "applypilot.db-wal"}
                and not destination.exists()
            ):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                migrated = True

    legacy_db = (
        legacy_app_dir / "applypilot.db"
        if app_dir != legacy_app_dir
        else app_dir / "applypilot.db"
    )
    new_db = app_dir / "rolesail.db"
    if legacy_db.is_file() and not new_db.exists():
        new_db.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(legacy_db) as source, sqlite3.connect(new_db) as destination:
                source.backup(destination)
        except sqlite3.DatabaseError:
            # Preserve even an unreadable legacy file so recovery remains possible.
            new_db.unlink(missing_ok=True)
            shutil.copy2(legacy_db, new_db)
        migrated = True

    return migrated


def ensure_dirs() -> bool:
    """Migrate legacy data, create required directories, and report migration."""
    migrated = migrate_legacy_app_dir()
    for d in [
        APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, COMPANY_LOGO_DIR,
        CHROME_WORKER_DIR, APPLY_WORKER_DIR,
    ]:
        d.mkdir(parents=True, exist_ok=True)
    return migrated


def load_profile() -> dict:
    """Load user profile from ~/.rolesail/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `rolesail init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_search_config() -> dict:
    """Load search configuration from ~/.rolesail/searches.yaml."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}

REMOTE_LOCATION_TERMS = ("remote", "anywhere", "work from home", "wfh", "distributed")
# ISO country codes that are also US state abbreviations need special care.  In
# particular, several international job APIs return Indian locations as
# ``City, IN``; treating that suffix as Indiana admitted those jobs through the
# North American allowlist.  Prefer the country interpretation unless the text
# also contains an explicit US marker or the full state name.
AMBIGUOUS_COUNTRY_CODE_US_REGIONS = {
    "IN": "indiana",
}
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
CANADA_PROVINCE_CODES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
}
CANADA_PROVINCE_NAMES = {
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "nova scotia", "ontario",
    "prince edward island", "quebec", "saskatchewan",
    "northwest territories", "nunavut", "yukon",
}


def _has_region_code(location: str, codes: set[str]) -> bool:
    """Match state/province codes only in comma-delimited location segments."""
    return any(
        re.search(rf"(?:^|,\s*){re.escape(code)}(?:\s*,|$)", location, re.IGNORECASE)
        for code in codes
    )


def location_is_allowed(location: str | None, search_cfg: dict | None = None) -> bool:
    """Apply country-aware location preferences to a job location string."""
    search_cfg = search_cfg or load_search_config()
    if not location:
        return bool(search_cfg.get("accept_unknown_locations", True))

    text = str(location).strip()
    lowered = text.lower()
    reject = search_cfg.get("location_reject_non_remote", [])
    if any(str(pattern).lower() in lowered for pattern in reject):
        return False

    allowed_countries = {
        str(country).lower().strip()
        for country in search_cfg.get("allowed_countries", [])
    }
    is_remote = any(term in lowered for term in REMOTE_LOCATION_TERMS)
    # A country allowlist is a hard boundary.  Remote jobs with no eligible
    # country signal must opt in explicitly instead of bypassing that boundary.
    accept_remote_anywhere = search_cfg.get(
        "accept_remote_anywhere",
        not bool(allowed_countries),
    )
    if is_remote and accept_remote_anywhere:
        return True

    if allowed_countries:
        allow_us = bool(allowed_countries & {"united states", "usa", "us", "u.s."})
        allow_canada = bool(allowed_countries & {"canada", "can"})
        explicit_us = bool(
            re.search(r"\b(?:united states(?: of america)?|usa|u\.s\.|us)\b", lowered)
        )
        for code, us_region in AMBIGUOUS_COUNTRY_CODE_US_REGIONS.items():
            if (
                re.search(rf"(?:^|,\s*){re.escape(code)}\s*$", text, re.IGNORECASE)
                and us_region not in lowered
                and not explicit_us
            ):
                return False
        if allow_us and (
            explicit_us
            or _has_region_code(text, US_STATE_CODES)
            or any(re.search(rf"\b{re.escape(name)}\b", lowered) for name in US_STATE_NAMES)
        ):
            return True
        if allow_canada and (
            re.search(r"\b(?:canada|can)\b", lowered)
            or _has_region_code(text, CANADA_PROVINCE_CODES)
            or any(re.search(rf"\b{re.escape(name)}\b", lowered) for name in CANADA_PROVINCE_NAMES)
        ):
            return True

    accept = search_cfg.get("location_accept", [])
    return any(str(pattern).lower() in lowered for pattern in accept)


def location_filter_is_mandatory(search_cfg: dict | None = None) -> bool:
    """Return whether a country allowlist makes location filtering non-optional.

    Source-specific filter switches are useful for loose, pattern-based searches,
    but an explicit country allowlist is a hard boundary. Discovery adapters must
    not allow their local switch to bypass it.
    """
    if search_cfg is None:
        search_cfg = load_search_config()
    countries = search_cfg.get("allowed_countries", [])
    return isinstance(countries, list) and any(
        isinstance(country, str) and country.strip() for country in countries
    )


def load_env():
    """Load environment variables from ~/.rolesail/.env and CWD .env.

    CWD values override ~/.rolesail/.env so a project-local .env can
    update LLM_MODEL / API keys without editing the home config.
    """
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Project-local .env wins over home config when both define a key.
    load_dotenv(override=True)


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Claude Code CLI + Chrome
    """
    load_env()

    has_llm = any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL"))
    if not has_llm:
        return 1

    has_claude = shutil.which("claude") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_claude and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")):
        missing.append("LLM API key — run [bold]rolesail init[/bold] or set GEMINI_API_KEY")
    if required >= 3:
        if not shutil.which("claude"):
            missing.append("Claude Code CLI — install from [bold]https://claude.ai/code[/bold]")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
