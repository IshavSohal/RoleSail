"""Safe, persistent cache for company logos used by the dashboard."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import urlparse

import httpx

from rolesail.config import COMPANY_LOGO_DIR

MAX_LOGO_BYTES = 2_000_000
ALLOWED_CONTENT_TYPES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}

# Employer domains are used to backfill logos for jobs saved before logo
# metadata was introduced. Keep aliases here because ATS URLs often belong to
# Greenhouse, Ashby, or Workday rather than to the employer itself.
COMPANY_DOMAINS = {
    "achievers": "achievers.com",
    "adobe": "adobe.com",
    "amazon": "www.amazon.com",
    "anthropic": "anthropic.com",
    "apple": "apple.com",
    "asana": "asana.com",
    "bdo": "bdo.ca",
    "blackberry": "blackberry.com",
    "bmo": "bmo.com",
    "brex": "brex.com",
    "cae": "cae.com",
    "canadian tire": "canadiantire.ca",
    "cibc": "cibc.com",
    "cisco": "cisco.com",
    "coinbase": "coinbase.com",
    "databricks": "databricks.com",
    "discord": "discord.com",
    "figma": "figma.com",
    "fis global": "fisglobal.com",
    "google": "google.com",
    "intact financial": "intactfc.com",
    "intel": "intel.com",
    "konrad": "konrad.com",
    "lyft": "lyft.com",
    "magna international": "magna.com",
    "manulife": "manulife.com",
    "mastercard": "mastercard.com",
    "meta": "meta.com",
    "microsoft": "microsoft.com",
    "motorola solutions": "motorolasolutions.com",
    "notion": "notion.so",
    "nvidia": "nvidia.com",
    "openai": "openai.com",
    "paypal": "paypal.com",
    "pinterest": "pinterest.com",
    "pwc": "pwc.com",
    "rbc": "rbc.com",
    "reddit": "reddit.com",
    "robinhood": "robinhood.com",
    "roblox": "roblox.com",
    "salesforce": "salesforce.com",
    "scale ai": "scale.com",
    "scotiabank": "scotiabank.com",
    "spacex": "spacex.com",
    "stripe": "stripe.com",
    "td bank": "td.com",
    "the walt disney company": "disney.com",
    "veeva systems": "veeva.com",
    "waabi": "waabi.ai",
    "workday": "workday.com",
}

# Some company sites do not expose a dependable browser-friendly favicon:
# Discord's conventional path returns 404, while Pinterest and Stripe serve
# small or inconsistent ICO variants. Prefer Google's normalized PNG for these
# known exceptions, including when an older favicon URL is stored in the DB.
PREFERRED_LOGO_URLS = {
    company: f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    for company, domain in {
        "discord": "discord.com",
        "pinterest": "pinterest.com",
        "stripe": "stripe.com",
    }.items()
}


def company_logo_candidates(company: str, stored_url: str | None = None) -> list[str]:
    """Return stored and domain-based logo sources in preference order."""
    normalized = re.sub(r"\s+", " ", company.casefold()).strip()
    preferred_url = PREFERRED_LOGO_URLS.get(normalized)
    candidates = [preferred_url] if preferred_url else []
    if stored_url:
        candidates.append(stored_url)
    domain = COMPANY_DOMAINS.get(normalized)
    if domain:
        candidates.extend([
            f"https://{domain}/favicon.ico",
            f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
        ])
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _cache_stem(company: str, source_url: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")[:48] or "company"
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def _valid_source_url(source_url: str) -> bool:
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").lower()
    if not (
        parsed.scheme in {"http", "https"}
        and bool(hostname)
        and hostname not in {"localhost", "localhost.localdomain"}
        and not hostname.endswith(".localhost")
    ):
        return False
    try:
        return ipaddress.ip_address(hostname).is_global
    except ValueError:
        return True


def load_company_logo(
    company: str,
    source_url: str,
    *,
    client: httpx.Client | None = None,
) -> tuple[bytes, str] | None:
    """Load a logo from disk, downloading it once when it is not cached yet."""
    if not company or not source_url or not _valid_source_url(source_url):
        return None

    stem = _cache_stem(company, source_url)
    COMPANY_LOGO_DIR.mkdir(parents=True, exist_ok=True)
    for content_type, suffix in ALLOWED_CONTENT_TYPES.items():
        cached = COMPANY_LOGO_DIR / f"{stem}{suffix}"
        if cached.is_file():
            return cached.read_bytes(), content_type

    owns_client = client is None
    client = client or httpx.Client(
        follow_redirects=True,
        timeout=8,
        headers={"User-Agent": "RoleSail/1.0 company-logo-cache"},
    )
    try:
        with client.stream("GET", source_url) as response:
            response.raise_for_status()
            if not _valid_source_url(str(response.url)):
                return None
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            suffix = ALLOWED_CONTENT_TYPES.get(content_type)
            if not suffix:
                return None
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_LOGO_BYTES:
                    return None
                chunks.append(chunk)
        body = b"".join(chunks)
        if not body:
            return None
        target = COMPANY_LOGO_DIR / f"{stem}{suffix}"
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(body)
        temporary.replace(target)
        return body, content_type
    except (httpx.HTTPError, OSError):
        return None
    finally:
        if owns_client:
            client.close()
