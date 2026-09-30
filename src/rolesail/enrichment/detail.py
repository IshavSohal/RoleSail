"""Detail page enrichment: scrapes full descriptions and apply URLs.

For each job URL in the database, navigates to the detail page and extracts:
  - full_description: the complete job posting text
  - application_url: the "Apply" button/link URL

Three-tier extraction cascade (cheapest first):
  Tier 1: JSON-LD JobPosting structured data (0 tokens)
  Tier 2: Deterministic CSS pattern matching (0 tokens)
  Tier 3: LLM-assisted extraction (1 LLM call)
"""

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from rolesail import config
from rolesail.database import init_db
from rolesail.discovery.filters import reconcile_unscored_jobs
from rolesail.llm import get_client

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Sites that block scraping -- skip detail extraction entirely
SKIP_DETAIL_SITES = {"glassdoor", "google", "Workopolis"}

# Module-level proxy config (set from CLI or caller)
_PROXY_CONFIG: dict | None = None


def set_proxy(proxy_str: str | None):
    """Set proxy config from an external caller."""
    global _PROXY_CONFIG
    if proxy_str:
        from rolesail.discovery.jobspy import parse_proxy
        _PROXY_CONFIG = parse_proxy(proxy_str)


# -- URL resolution ----------------------------------------------------------

def _load_base_urls() -> dict[str, str | None]:
    """Load site base URLs from config/sites.yaml."""
    from rolesail.config import load_base_urls
    return load_base_urls()


def resolve_url(raw_url: str, site: str) -> str | None:
    """Resolve a stored URL to an absolute URL."""
    if not raw_url:
        return None

    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url

    if site == "WelcomeToTheJungle":
        return None

    if site == "Randstad Canada" and "/" not in raw_url:
        return f"https://www.randstad.ca/jobs/search/{raw_url}"

    if site == "4DayWeek" and raw_url in ("/", "/jobs"):
        return None

    base = _load_base_urls().get(site)
    if not base:
        return None

    if ";jsessionid=" in raw_url:
        raw_url = raw_url.split(";jsessionid=")[0]

    return urljoin(base, raw_url)


def normalize_application_url(application_url: str | None, job_url: str) -> str:
    """Keep SuccessFactors applications on the live job-detail page.

    Direct ``/talentcommunity/apply/<id>/`` links rely on browser state created
    by the job page. Opening one from the dashboard can instead fall back to
    the employer's careers homepage, so the stable external entry point is the
    posting itself.
    """
    if not application_url:
        return job_url

    parsed_apply = urlparse(application_url)
    parsed_job = urlparse(job_url)
    if (
        parsed_apply.netloc.lower() == parsed_job.netloc.lower()
        and parsed_apply.path.lower().startswith("/talentcommunity/apply/")
    ):
        return job_url

    return application_url


def resolve_all_urls(conn: sqlite3.Connection) -> dict:
    """Resolve all relative URLs in the database. Returns stats."""
    rows = conn.execute("SELECT url, site FROM jobs").fetchall()
    resolved = 0
    failed = 0
    already_absolute = 0

    for row in rows:
        url, site = row[0], row[1]
        if url.startswith("http://") or url.startswith("https://"):
            already_absolute += 1
            continue

        new_url = resolve_url(url, site)
        if new_url and new_url != url:
            try:
                conn.execute("UPDATE jobs SET url = ? WHERE url = ?", (new_url, url))
                resolved += 1
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
                resolved += 1
        else:
            failed += 1

    # Also resolve relative application_urls
    app_resolved = 0
    rows = conn.execute(
        "SELECT url, site, application_url FROM jobs "
        "WHERE application_url IS NOT NULL AND application_url != '' "
        "AND application_url NOT LIKE 'http%'"
    ).fetchall()
    for row in rows:
        url, site, app_url = row[0], row[1], row[2]
        new_app = resolve_url(app_url, site)
        if new_app and new_app != app_url:
            conn.execute("UPDATE jobs SET application_url = ? WHERE url = ?", (new_app, url))
            app_resolved += 1

    conn.commit()
    return {"resolved": resolved, "failed": failed, "already_absolute": already_absolute,
            "app_resolved": app_resolved}


def resolve_wttj_urls(conn: sqlite3.Connection) -> int:
    """Re-fetch WTTJ Algolia API to get proper detail URLs and fix slug-as-title.
    Returns count of URLs updated."""
    wttj_jobs = conn.execute(
        "SELECT url, title FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchall()

    if not wttj_jobs:
        return 0

    algolia_data: dict = {}

    def capture_algolia(response):
        if "algolia.net" in response.url and "/queries" in response.url:
            try:
                algolia_data["response"] = json.loads(response.text())
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)
        page.on("response", capture_algolia)
        page.goto(
            "https://www.welcometothejungle.com/en/jobs?query=developer&refinementList%5Bremote%5D%5B%5D=fulltime",
            timeout=60000,
        )
        page.wait_for_load_state("networkidle")
        browser.close()

    if not algolia_data.get("response"):
        log.warning("WTTJ: No Algolia response captured")
        return 0

    results = algolia_data["response"].get("results", [])
    slug_map: dict = {}
    for rs in results:
        for hit in rs.get("hits", []):
            slug = hit.get("slug", "")
            org = hit.get("organization", {})
            org_slug = org.get("slug", "") if isinstance(org, dict) else ""
            name = hit.get("name", "")
            if slug and org_slug:
                detail_url = f"https://www.welcometothejungle.com/en/companies/{org_slug}/jobs/{slug}"
                slug_map[slug] = {"url": detail_url, "name": name}

    updated = 0
    for row in wttj_jobs:
        old_url, old_title = row[0], row[1]
        slug = old_url.split("_DFNS_")[0] if "_DFNS_" in old_url else old_url
        match = slug_map.get(slug) or slug_map.get(old_url)
        if match:
            try:
                conn.execute(
                    "UPDATE jobs SET url = ?, title = ? WHERE url = ?",
                    (match["url"], match["name"] or old_title, old_url),
                )
                updated += 1
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM jobs WHERE url = ?", (old_url,))
                updated += 1
        else:
            for s, data in slug_map.items():
                if s in old_url or old_url in s:
                    try:
                        conn.execute(
                            "UPDATE jobs SET url = ?, title = ? WHERE url = ?",
                            (data["url"], data["name"] or old_title, old_url),
                        )
                        updated += 1
                    except sqlite3.IntegrityError:
                        conn.execute("DELETE FROM jobs WHERE url = ?", (old_url,))
                        updated += 1
                    break

    conn.commit()
    return updated


# -- Detail page intelligence ------------------------------------------------

def collect_detail_intelligence(page) -> dict:
    """Collect signals from a detail page. Lighter than discovery -- no API interception."""
    intel: dict = {
        "json_ld": [],
        "apple_hydration": None,
        "meta_sections": None,
        "microsoft_details": None,
        "page_title": "",
        "page_icon": None,
        "final_url": "",
    }

    intel["page_title"] = page.title()
    intel["final_url"] = page.url

    try:
        icon = page.query_selector(
            'link[rel~="icon"][href], link[rel="apple-touch-icon"][href]'
        )
        if icon:
            href = icon.get_attribute("href")
            if href:
                intel["page_icon"] = urljoin(intel["final_url"], href)
    except Exception:
        pass

    for el in page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(el.inner_text())
            intel["json_ld"].append(data)
        except Exception:
            pass

    if "jobs.apple.com" in intel["final_url"]:
        try:
            intel["apple_hydration"] = page.evaluate(
                "() => window.__staticRouterHydrationData || null"
            )
        except Exception:
            pass

    if "metacareers.com" in intel["final_url"]:
        try:
            # Meta's JobPosting JSON-LD stores only the introductory paragraph
            # in `description`. The rest of the posting is rendered as sibling
            # sections (responsibilities, qualifications, compensation, etc.).
            # Select the direct children spanning the first responsibilities
            # heading through EEO so navigation and the apply-card are omitted.
            intel["meta_sections"] = page.evaluate(
                """() => {
                    const headings = [...document.querySelectorAll("h2")];
                    const responsibilities = headings.find((heading) =>
                        heading.textContent.trim().endsWith("Responsibilities")
                    );
                    const eeo = headings.find((heading) =>
                        heading.textContent.trim() === "Equal Employment Opportunity"
                    );
                    if (!responsibilities || !eeo) return null;

                    const ancestors = (element) => {
                        const result = [];
                        while (element) {
                            result.push(element);
                            element = element.parentElement;
                        }
                        return result;
                    };
                    const eeoAncestors = new Set(ancestors(eeo));
                    const container = ancestors(responsibilities).find((element) =>
                        eeoAncestors.has(element)
                    );
                    if (!container) return null;

                    const directChild = (element) => {
                        while (element && element.parentElement !== container) {
                            element = element.parentElement;
                        }
                        return element;
                    };
                    const children = [...container.children];
                    const start = children.indexOf(directChild(responsibilities));
                    const end = children.indexOf(directChild(eeo));
                    if (start < 0 || end < start) return null;
                    return children.slice(start, end + 1)
                        .map((element) => element.innerText.trim())
                        .filter(Boolean);
                }"""
            )
        except Exception:
            pass

    if "apply.careers.microsoft.com" in intel["final_url"]:
        job_id_match = re.search(r"/job/(\d+)", intel["final_url"])
        if job_id_match:
            try:
                intel["microsoft_details"] = page.evaluate(
                    """async ({positionId}) => {
                        const params = new URLSearchParams({
                            position_id: positionId,
                            domain: "microsoft.com",
                            hl: "en",
                        });
                        const response = await fetch(`/api/pcsx/position_details?${params}`);
                        if (!response.ok) return null;
                        const payload = await response.json();
                        return payload?.data || null;
                    }""",
                    {"positionId": job_id_match.group(1)},
                )
            except PlaywrightError:
                pass

    return intel


def _apple_jobs_data(payload: object) -> dict | None:
    """Locate Apple's job-detail object in its router hydration payload."""
    if not isinstance(payload, dict):
        return None
    jobs_data = (
        payload.get("loaderData", {})
        .get("jobDetails", {})
        .get("jobsData")
    )
    return jobs_data if isinstance(jobs_data, dict) else None


def extract_from_apple_hydration(intel: dict) -> dict | None:
    """Assemble every Apple job-description section from page hydration data."""
    jobs_data = _apple_jobs_data(intel.get("apple_hydration"))
    if not jobs_data:
        return None

    # Prefer the selected localization when present. Some postings put fields
    # only in the localized `posting` object, so merge both representations.
    posting: dict = {}
    localizations = jobs_data.get("localizations")
    if isinstance(localizations, dict):
        locale = jobs_data.get("selectedLocale")
        localized = localizations.get(locale) if locale else None
        if not isinstance(localized, dict) and localizations:
            localized = next(
                (value for value in localizations.values() if isinstance(value, dict)),
                None,
            )
        if isinstance(localized, dict) and isinstance(localized.get("posting"), dict):
            posting.update(localized["posting"])
    posting.update(jobs_data)

    section_fields = (
        ("Summary", ("jobSummary", "summary")),
        ("Description", ("jobDescription", "description")),
        ("Responsibilities", ("responsibilities", "responsibility")),
        ("Minimum Qualifications", ("minimumQualifications",)),
        ("Preferred Qualifications", ("preferredQualifications",)),
    )
    sections: list[str] = []
    seen: set[str] = set()
    for heading, fields in section_fields:
        value = next((posting.get(field) for field in fields if posting.get(field)), None)
        cleaned = clean_description(value) if isinstance(value, str) else ""
        if cleaned and cleaned not in seen:
            sections.append(f"{heading}\n{cleaned}")
            seen.add(cleaned)

    description = "\n\n".join(sections)
    if len(description) < 50:
        return None
    return {
        "full_description": description,
        "application_url": intel.get("final_url") or None,
        "title": posting.get("postingTitle"),
        "company": "Apple",
        "location": None,
        "posted_at": posting.get("postDateInGMT") or posting.get("postingDateMeta"),
    }


def extract_from_microsoft_details(intel: dict) -> dict | None:
    """Extract Microsoft's complete description from its position-details API."""
    details = intel.get("microsoft_details")
    if not isinstance(details, dict):
        return None

    description = clean_description(details.get("jobDescription", ""))
    if len(description) < 50:
        return None

    posted_at = None
    if details.get("postedTs"):
        try:
            posted_at = datetime.fromtimestamp(
                int(details["postedTs"]), UTC
            ).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            pass

    locations = details.get("locations") or []
    location = details.get("location") or "; ".join(str(item) for item in locations)
    return {
        "full_description": description,
        "application_url": intel.get("final_url") or None,
        "title": details.get("name"),
        "company": "Microsoft",
        "location": location or None,
        "posted_at": posted_at,
    }


def extract_from_meta_details(intel: dict) -> dict | None:
    """Assemble Meta's intro and every rendered job-description section."""
    final_url = intel.get("final_url") or ""
    if "metacareers.com" not in final_url:
        return None

    posting = next(
        (
            candidate
            for item in intel.get("json_ld", [])
            if (candidate := _find_job_posting(item))
        ),
        None,
    )
    if not posting:
        return None

    sections: list[str] = []
    description = clean_description(posting.get("description", ""))
    if description:
        sections.append(description)

    rendered_sections = intel.get("meta_sections")
    if isinstance(rendered_sections, list):
        sections.extend(
            cleaned
            for value in rendered_sections
            if isinstance(value, str) and (cleaned := clean_description(value))
        )
    else:
        # Keep a structured-data fallback if Meta changes its DOM. The live
        # page currently exposes richer, separately headed sections.
        for heading, field in (
            ("Responsibilities", "responsibilities"),
            ("Qualifications", "qualifications"),
        ):
            value = posting.get(field)
            cleaned = (
                clean_description(value.replace("&nbsp;", "\n"))
                if isinstance(value, str)
                else ""
            )
            if cleaned:
                sections.append(f"{heading}\n{cleaned}")

    full_description = "\n\n".join(dict.fromkeys(sections))
    if len(full_description) < 50:
        return None

    result = {
        "full_description": full_description,
        # Meta starts its authenticated application flow from the job page.
        "application_url": final_url,
    }
    result.update(extract_job_metadata(intel))
    return result


# -- Tier 1: JSON-LD extraction -----------------------------------------------

def _find_job_posting(data):
    """Find the first JobPosting object in a JSON-LD structure."""
    if isinstance(data, dict):
        if data.get("@type") == "JobPosting":
            return data
        if "@graph" in data and isinstance(data["@graph"], list):
            for item in data["@graph"]:
                result = _find_job_posting(item)
                if result:
                    return result
    elif isinstance(data, list):
        for item in data:
            result = _find_job_posting(item)
            if result:
                return result
    return None


def _job_location(posting: dict) -> str | None:
    """Normalize a JobPosting location into readable text."""
    locations = posting.get("jobLocation") or posting.get("applicantLocationRequirements") or []
    if isinstance(locations, dict):
        locations = [locations]

    labels: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address", location)
        if not isinstance(address, dict):
            continue
        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]
        label = ", ".join(str(part) for part in parts if part)
        if label and label not in labels:
            labels.append(label)

    job_location_type = posting.get("jobLocationType")
    if job_location_type == "TELECOMMUTE" and "Remote" not in labels:
        labels.insert(0, "Remote")
    return "; ".join(labels) or None


def _organization_logo(organization: object) -> str | None:
    """Return a schema.org Organization logo URL in its common forms."""
    if not isinstance(organization, dict):
        return None
    logo = organization.get("logo")
    if isinstance(logo, str):
        return logo.strip() or None
    if isinstance(logo, dict):
        value = logo.get("url") or logo.get("contentUrl")
        return value.strip() if isinstance(value, str) and value.strip() else None
    return None


def extract_job_metadata(intel: dict) -> dict:
    """Extract title, company, and location from JSON-LD or page metadata."""
    for ld in intel.get("json_ld", []):
        posting = _find_job_posting(ld)
        if not posting:
            continue
        organization = posting.get("hiringOrganization") or {}
        company = organization.get("name") if isinstance(organization, dict) else None
        company_logo = _organization_logo(organization) or intel.get("page_icon")
        if company_logo and intel.get("final_url"):
            company_logo = urljoin(intel["final_url"], company_logo)
        return {
            "title": posting.get("title") or posting.get("name"),
            "company": company,
            "company_logo": company_logo,
            "location": _job_location(posting),
            "posted_at": posting.get("datePosted"),
        }

    page_title = (intel.get("page_title") or "").strip()
    final_url = intel.get("final_url") or ""
    hostname = (urlparse(final_url).hostname or "").lower()
    if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
        # Greenhouse's current job-board pages do not consistently expose
        # JobPosting JSON-LD, but their document title is stable and contains
        # both values: "Job Application for <role> at <company>".
        greenhouse_title = re.fullmatch(
            r"Job Application for\s+(.+)\s+at\s+(.+)",
            page_title,
            flags=re.IGNORECASE,
        )
        if greenhouse_title:
            return {
                "title": greenhouse_title.group(1).strip(),
                "company": greenhouse_title.group(2).strip(),
                "company_logo": intel.get("page_icon"),
                "location": None,
                "posted_at": None,
            }

    if hostname == "jobs.ashbyhq.com":
        # Ashby pages are client-rendered and do not always expose JobPosting
        # JSON-LD, but their document title follows "<role> @ <company>".
        ashby_title = re.fullmatch(r"(.+?)\s+@\s+(.+)", page_title)
        if ashby_title:
            return {
                "title": ashby_title.group(1).strip(),
                "company": ashby_title.group(2).strip(),
                "company_logo": intel.get("page_icon"),
                "location": None,
                "posted_at": None,
            }

    return {
        "title": re.split(r"\s+[|–—]\s+", page_title, maxsplit=1)[0] or None,
        "company": None,
        "company_logo": intel.get("page_icon"),
        "location": None,
        "posted_at": None,
    }


def extract_from_json_ld(intel: dict) -> dict | None:
    """Extract description and apply URL from JSON-LD JobPosting.
    Returns description, application URL, and available posting metadata."""

    for ld in intel.get("json_ld", []):
        posting = _find_job_posting(ld)
        if not posting:
            continue

        desc = posting.get("description", "")
        if not desc:
            continue

        desc_clean = clean_description(desc)
        if len(desc_clean) < 50:
            continue

        apply_url = None
        if posting.get("directApply"):
            apply_url = posting.get("url")
        if not apply_url:
            contact = posting.get("applicationContact")
            if isinstance(contact, dict):
                apply_url = contact.get("url")
        if not apply_url:
            apply_url = posting.get("url")

        result = {
            "full_description": desc_clean,
            "application_url": apply_url,
        }
        result.update(extract_job_metadata({
            "json_ld": [posting],
            "final_url": intel.get("final_url"),
            "page_icon": intel.get("page_icon"),
        }))
        return result

    return None


# -- Tier 2: Deterministic pattern matching ----------------------------------

APPLY_SELECTORS = [
    'a[href*="apply"]',
    'a[data-testid*="apply"]',
    'a[class*="apply"]',
    'a[aria-label*="pply"]',
    'button[data-testid*="apply"]',
    'a#apply_button',
    '.postings-btn-wrapper a',
    'a.ashby-job-posting-apply-button',
    '#grnhse_app a[href*="apply"]',
    'a[data-qa="btn-apply"]',
    'a[class*="btn-apply"]',
    'a[class*="apply-btn"]',
    'a[class*="apply-button"]',
]

DESCRIPTION_SELECTORS = [
    '#job-description',
    '#job_description',
    '#jobDescriptionText',
    '.job-description',
    '.job_description',
    '[class*="job-description"]',
    '[class*="jobDescription"]',
    '[data-testid*="description"]',
    '[data-testid="job-description"]',
    '.posting-page .posting-categories + div',
    '#content .posting-page',
    '#app_body .content',
    '#grnhse_app .content',
    '.ashby-job-posting-description',
    '[class*="posting-description"]',
    '[class*="job-detail"]',
    '[class*="jobDetail"]',
    '[class*="job-content"]',
    '[class*="job-body"]',
    '[role="main"] article',
    'main article',
    'article[class*="job"]',
    '.job-posting-content',
]


def extract_apply_url_deterministic(page) -> str | None:
    """Try known CSS patterns for apply buttons/links."""
    for sel in APPLY_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                href = el.get_attribute("href")
                if href and href != "#":
                    return urljoin(page.url, href)
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "button":
                    parent_href = el.evaluate("el => el.parentElement?.querySelector('a')?.href || null")
                    if parent_href:
                        return parent_href
                    return page.url
        except Exception:
            continue

    try:
        links = page.query_selector_all("a")
        for link in links:
            text = link.inner_text().strip().lower()
            if "apply" in text and len(text) < 50:
                href = link.get_attribute("href")
                if href and href != "#" and "javascript:" not in href:
                    return urljoin(page.url, href)
    except Exception:
        pass

    return None


def extract_description_deterministic(page) -> str | None:
    """Try known CSS patterns for the job description block."""
    for sel in DESCRIPTION_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if len(text) >= 100:
                    return clean_description(text)
        except Exception:
            continue

    return None


# -- Tier 3: LLM extraction -------------------------------------------------

DETAIL_EXTRACT_PROMPT = """You are extracting job details from a single job posting page.

PAGE URL: {url}
PAGE TITLE: {title}

Find TWO things in the HTML below:
1. The full job description text (responsibilities, requirements, etc.)
2. The URL of the "Apply" button/link

Rules:
- For description: extract the FULL text. Include all sections (About, Responsibilities, Requirements, etc.)
- For apply URL: find the href of the link/button that starts the application process
- If you cannot find one, set it to null

Return ONLY valid JSON:
{{"full_description": "the complete job description text here", "application_url": "https://..." or null}}

No explanation, no markdown. Keep reasoning under 20 words.

HTML:
{content}"""


def extract_main_content(page) -> str:
    """Extract the main content area, stripped of navigation noise."""
    for sel in ["main", "article", '[role="main"]', "#content", ".content"]:
        try:
            el = page.query_selector(sel)
            if el:
                text_len = len(el.inner_text().strip())
                if text_len > 200:
                    html = el.inner_html()
                    if len(html) < 50000:
                        return clean_content_html(html)
        except Exception:
            continue

    try:
        html = page.evaluate("""
            () => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('nav, header, footer, script, style, noscript, svg, iframe').forEach(el => el.remove());
                return clone.innerHTML;
            }
        """)
        return clean_content_html(html[:50000])
    except Exception:
        return ""


def clean_content_html(html: str) -> str:
    """Clean detail page HTML for LLM consumption."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("script, style, noscript, svg, iframe, nav, header, footer"):
        tag.decompose()

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in ("id", "href", "class", "role", "aria-label", "data-testid", "name", "for", "type"):
                if attr == "class":
                    classes = val if isinstance(val, list) else val.split()
                    kept = [c for c in classes if len(c) < 30 and not re.match(r"^[a-z]{1,2}-\d+$", c)]
                    if kept:
                        new_attrs["class"] = " ".join(kept[:3])
                else:
                    new_attrs[attr] = val
            elif attr.startswith("data-") or attr.startswith("aria-"):
                new_attrs[attr] = val
        tag.attrs = new_attrs

    return str(soup)


def extract_with_llm(page, url: str) -> dict:
    """Send focused HTML to LLM for extraction. Fallback tier."""
    content = extract_main_content(page)
    if not content:
        return {"full_description": None, "application_url": None}

    title = ""
    try:
        title = page.title()
    except Exception:
        pass

    prompt = DETAIL_EXTRACT_PROMPT.format(
        url=url,
        title=title,
        content=content[:30000],
    )

    try:
        client = get_client()
        t0 = time.time()
        raw = client.ask(prompt, temperature=0.0, max_tokens=4096)
        elapsed = time.time() - t0
        log.info("LLM: %d chars in, %.1fs", len(prompt), elapsed)

        from rolesail.discovery.smartextract import extract_json
        result = extract_json(raw)
        desc = result.get("full_description")
        apply_url = result.get("application_url")

        if desc:
            desc = clean_description(desc)

        return {"full_description": desc, "application_url": apply_url}
    except Exception as e:
        log.error("LLM ERROR: %s", e)
        return {"full_description": None, "application_url": None}


# -- Description cleaning ---------------------------------------------------

def clean_description(text: str) -> str:
    """Convert HTML description to clean readable text."""
    if not text:
        return ""

    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for li in soup.find_all("li"):
            li.insert_before("- ")
        text = soup.get_text()

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def reset_incomplete_apple_descriptions(conn: sqlite3.Connection) -> int:
    """Requeue Apple rows previously enriched from search summaries only."""
    cursor = conn.execute(
        "UPDATE jobs SET full_description = NULL, detail_scraped_at = NULL "
        "WHERE (site = 'Apple' OR strategy = 'apple_careers') "
        "AND full_description IS NOT NULL "
        "AND full_description NOT LIKE '%Minimum Qualifications%' "
        "AND full_description NOT LIKE '%Preferred Qualifications%'"
    )
    conn.commit()
    return cursor.rowcount


def reset_incomplete_amazon_descriptions(conn: sqlite3.Connection) -> int:
    """Requeue Amazon rows saved before qualification fields were assembled."""
    cursor = conn.execute(
        "UPDATE jobs SET full_description = NULL, detail_scraped_at = NULL "
        "WHERE (site = 'Amazon' OR strategy = 'amazon_careers') "
        "AND full_description IS NOT NULL "
        "AND (full_description NOT LIKE '%Basic Qualifications%' "
        "OR full_description NOT LIKE '%Preferred Qualifications%')"
    )
    conn.commit()
    return cursor.rowcount


def reset_incomplete_microsoft_descriptions(conn: sqlite3.Connection) -> int:
    """Requeue Microsoft rows captured from JSON-LD without the Overview."""
    cursor = conn.execute(
        "UPDATE jobs SET full_description = NULL, detail_scraped_at = NULL "
        "WHERE (site = 'Microsoft' OR strategy = 'microsoft_careers') "
        "AND full_description IS NOT NULL "
        "AND full_description NOT LIKE '%Overview%'"
    )
    conn.commit()
    return cursor.rowcount


def reset_incomplete_meta_descriptions(conn: sqlite3.Connection) -> int:
    """Requeue Meta rows captured from the introductory JSON-LD field only."""
    cursor = conn.execute(
        "UPDATE jobs SET full_description = NULL, detail_scraped_at = NULL "
        "WHERE (site = 'Meta' OR strategy = 'meta_careers') "
        "AND full_description IS NOT NULL "
        "AND (full_description NOT LIKE '%Responsibilities%' "
        "OR full_description NOT LIKE '%Minimum Qualifications%')"
    )
    conn.commit()
    return cursor.rowcount


# -- Orchestration -----------------------------------------------------------

SITE_DELAYS = {
    "RemoteOK": 3.0,
    "WelcomeToTheJungle": 2.0,
    "Job Bank Canada": 1.5,
    "CareerJet Canada": 3.0,
    "Hacker News Jobs": 1.0,
    "BuiltIn Remote": 2.0,
}

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
PERMANENT_FAILURES = {404, 410, 451}


def scrape_detail_page(page, url: str) -> dict:
    """Full cascade for one detail page."""
    result: dict = {
        "full_description": None,
        "application_url": None,
        "status": "error",
        "tier_used": None,
        "error": None,
        "title": None,
        "company": None,
        "company_logo": None,
        "location": None,
        "posted_at": None,
    }
    t0 = time.time()

    try:
        resp = page.goto(url, timeout=45000)
        if resp and resp.status in PERMANENT_FAILURES:
            result["error"] = f"HTTP {resp.status}"
            result["elapsed"] = time.time() - t0
            return result
        if resp and resp.status == 403:
            result["error"] = "HTTP 403"
            result["elapsed"] = time.time() - t0
            return result
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
    except Exception as e:
        err_str = str(e)
        if "timeout" in err_str.lower():
            result["error"] = "timeout"
        else:
            result["error"] = err_str[:200]
        result["elapsed"] = time.time() - t0
        return result

    intel = collect_detail_intelligence(page)
    result.update(extract_job_metadata(intel))

    # Meta's JSON-LD `description` is only the intro. Prefer the complete set
    # of rendered sections collected from the job detail container.
    meta_result = extract_from_meta_details(intel)
    if meta_result and meta_result.get("full_description"):
        result.update(meta_result)
        result["tier_used"] = 1
        result["status"] = "ok"
        result["elapsed"] = time.time() - t0
        return result

    # Microsoft's JSON-LD omits the Overview. Prefer the complete PCS/X API
    # payload used to render the page.
    microsoft_result = extract_from_microsoft_details(intel)
    if microsoft_result and microsoft_result.get("full_description"):
        result.update(microsoft_result)
        result["tier_used"] = 1
        result["status"] = "ok"
        result["elapsed"] = time.time() - t0
        return result

    # Apple exposes each description section separately in its router payload.
    # Run this before JSON-LD, which may contain only the Summary section.
    apple_result = extract_from_apple_hydration(intel)
    if apple_result and apple_result.get("full_description"):
        result.update(apple_result)
        result["tier_used"] = 1
        result["status"] = "ok"
        result["elapsed"] = time.time() - t0
        return result

    # Tier 1: JSON-LD
    json_ld_result = extract_from_json_ld(intel)
    if json_ld_result and json_ld_result.get("full_description"):
        result.update(json_ld_result)
        result["tier_used"] = 1
        if not result.get("application_url"):
            apply = extract_apply_url_deterministic(page)
            if apply:
                result["application_url"] = apply
        result["status"] = "ok" if result.get("application_url") else "partial"
        result["elapsed"] = time.time() - t0
        return result

    # Tier 2: Deterministic CSS
    desc = extract_description_deterministic(page)
    apply = extract_apply_url_deterministic(page)

    if desc:
        result["full_description"] = desc
        result["application_url"] = apply
        result["tier_used"] = 2
        result["status"] = "ok" if apply else "partial"
        result["elapsed"] = time.time() - t0
        return result

    tier2_apply = apply

    # Tier 3: LLM
    llm_result = extract_with_llm(page, url)
    result["full_description"] = llm_result.get("full_description")
    result["application_url"] = llm_result.get("application_url") or tier2_apply
    result["tier_used"] = 3

    if result.get("full_description"):
        result["status"] = "ok" if result.get("application_url") else "partial"
    elif result.get("application_url"):
        result["status"] = "partial"
    else:
        result["status"] = "error"
        result["error"] = "no data extracted"

    result["elapsed"] = time.time() - t0
    return result


def scrape_site_batch(
    conn: sqlite3.Connection | None,
    site: str,
    jobs: list[tuple],
    delay: float = 2.0,
    max_jobs: int | None = None,
) -> dict:
    """Process all jobs for one site using shared browser context.

    If conn is None, creates its own DB connection.
    """
    stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    if max_jobs:
        jobs = jobs[:max_jobs]

    if not jobs:
        return stats

    own_conn = conn is None
    if own_conn:
        conn = init_db()

    now = datetime.now(timezone.utc).isoformat()

    try:
        with sync_playwright() as p:
            launch_opts: dict = {"headless": True}
            if _PROXY_CONFIG:
                launch_opts["proxy"] = _PROXY_CONFIG["playwright"]
            browser = p.chromium.launch(**launch_opts)
            context = browser.new_context(user_agent=UA)
            page = context.new_page()

            for i, (url, title) in enumerate(jobs):
                log.info("[%d/%d] %s", i + 1, len(jobs), title[:50] if title else url[:50])

                result = scrape_detail_page(page, url)
                stats["processed"] += 1

                tier = result.get("tier_used")
                status = result["status"]
                elapsed = result.get("elapsed", 0)

                if tier:
                    stats["tiers"][tier] = stats["tiers"].get(tier, 0) + 1

                tier_str = f"T{tier}" if tier else "--"
                desc_len = len(result.get("full_description") or "")
                apply_str = "yes" if result.get("application_url") else "no"
                err_str = f" | err={result.get('error')}" if result.get("error") else ""

                log.info("  %s | %s | desc=%s chars | apply=%s | %.1fs%s",
                         status, tier_str, f"{desc_len:,}", apply_str, elapsed, err_str)

                if status in ("ok", "partial"):
                    stats[status] += 1
                    enriched_location = result.get("location")
                    existing_location = conn.execute(
                        "SELECT location FROM jobs WHERE url = ?",
                        (url,),
                    ).fetchone()
                    effective_location = enriched_location or (
                        existing_location[0] if existing_location else None
                    )
                    location_allowed = config.location_is_allowed(effective_location)
                    is_external_ashby = (
                        (urlparse(url).hostname or "").lower() == "jobs.ashbyhq.com"
                    )
                    conn.execute(
                        "UPDATE jobs SET full_description = ?, application_url = ?, "
                        "detail_scraped_at = ?, detail_error = NULL, "
                        "title = CASE WHEN strategy = 'external_upload' "
                        "THEN COALESCE(?, title) ELSE title END, "
                        "company = CASE WHEN strategy = 'external_upload' "
                        "AND ? = 1 THEN COALESCE(?, company) "
                        "ELSE COALESCE(company, ?) END, "
                        "company_logo = COALESCE(company_logo, ?), "
                        "location = CASE WHEN strategy = 'external_upload' "
                        "THEN COALESCE(?, location) ELSE location END, "
                        "posted_at = COALESCE(posted_at, ?), "
                        "discovery_status = CASE WHEN strategy = 'external_upload' AND ? = 0 "
                        "THEN 'rejected' ELSE discovery_status END, "
                        "discovery_rejection_reason = CASE "
                        "WHEN strategy = 'external_upload' AND ? = 0 THEN 'outside_allowed_countries' "
                        "ELSE discovery_rejection_reason END, "
                        "discovery_checked_at = CASE WHEN strategy = 'external_upload' "
                        "THEN ? ELSE discovery_checked_at END "
                        "WHERE url = ?",
                        (
                            result.get("full_description"),
                            normalize_application_url(
                                result.get("application_url"), url
                            ),
                            now,
                            result.get("title"),
                            is_external_ashby,
                            result.get("company"),
                            result.get("company"),
                            result.get("company_logo"),
                            result.get("location"),
                            result.get("posted_at"),
                            location_allowed,
                            location_allowed,
                            now,
                            url,
                        ),
                    )
                else:
                    stats["error"] += 1
                    conn.execute(
                        "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? WHERE url = ?",
                        (result.get("error", "unknown"), now, url),
                    )

                conn.commit()

                if i < len(jobs) - 1:
                    time.sleep(delay)

            browser.close()
    finally:
        if own_conn:
            conn.close()

    return stats


def _run_detail_scraper(
    conn: sqlite3.Connection,
    sites: list[str] | None = None,
    max_per_site: int | None = None,
    workers: int = 1,
) -> dict:
    """Groups pending jobs by site and processes each batch.

    Sequential by default. When workers > 1, processes multiple site batches
    in parallel using ThreadPoolExecutor (each thread gets its own browser
    and DB connection).

    Returns aggregate stats dict.
    """
    skip_filter = " AND ".join(f"site != '{s}'" for s in SKIP_DETAIL_SITES)
    where = (
        "WHERE detail_scraped_at IS NULL "
        "AND COALESCE(discovery_status, 'accepted') = 'accepted' "
        f"AND {skip_filter}"
    )
    rows = conn.execute(
        f"SELECT url, title, site FROM jobs {where} ORDER BY site"
    ).fetchall()

    if not rows:
        log.info("No pending jobs to scrape.")
        return {"processed": 0, "ok": 0, "partial": 0, "error": 0}

    site_jobs: dict[str, list[tuple]] = {}
    for row in rows:
        url, title, site = row[0], row[1], row[2]
        if sites and site not in sites:
            continue
        site_jobs.setdefault(site, []).append((url, title))

    log.info("Pending: %d jobs across %d sites (workers=%d)", len(rows), len(site_jobs), workers)
    for site, jobs in site_jobs.items():
        log.info("  %s: %d jobs", site, len(jobs))

    known_order = [
        "RemoteOK", "Job Bank Canada", "BuiltIn Remote",
        "WelcomeToTheJungle", "CareerJet Canada", "Hacker News Jobs",
    ]
    order = [s for s in known_order if s in site_jobs]
    order += [s for s in sorted(site_jobs.keys()) if s not in order]

    total_stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    def _merge_stats(stats: dict) -> None:
        for k in ("processed", "ok", "partial", "error"):
            total_stats[k] += stats[k]
        for t, count in stats["tiers"].items():
            total_stats["tiers"][t] = total_stats["tiers"].get(t, 0) + count

    if workers > 1 and len(order) > 1:
        # Parallel mode: each site batch runs in its own thread with its own
        # DB connection (conn=None tells scrape_site_batch to create one)
        def _scrape_site(site: str) -> dict:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)
            stats = scrape_site_batch(None, site, jobs, delay=delay, max_jobs=max_per_site)
            log.info("%s summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     site, stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))
            return stats

        with ThreadPoolExecutor(max_workers=min(workers, len(order))) as pool:
            futures = {pool.submit(_scrape_site, site): site for site in order}
            for future in as_completed(futures):
                _merge_stats(future.result())
    else:
        # Sequential mode (default)
        for site in order:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)

            stats = scrape_site_batch(conn, site, jobs, delay=delay, max_jobs=max_per_site)
            _merge_stats(stats)

            log.info("Site summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))

    log.info("TOTAL: %d processed | %d ok | %d partial | %d error",
             total_stats["processed"], total_stats["ok"], total_stats["partial"], total_stats["error"])
    log.info("Tier distribution: T1=%d T2=%d T3=%d",
             total_stats["tiers"].get(1, 0), total_stats["tiers"].get(2, 0), total_stats["tiers"].get(3, 0))

    llm_calls = total_stats["tiers"].get(3, 0)
    total = total_stats["processed"]
    if total > 0:
        savings = ((total - llm_calls) / total) * 100
        log.info("LLM calls: %d/%d (%.0f%% saved)", llm_calls, total, savings)

    return total_stats


# -- Streaming detail scraper (for sequential pipeline) ----------------------

def stream_detail(
    upstream_done,
    my_done,
    proxy_str: str | None = None,
    poll_interval: float = 5.0,
) -> None:
    """Streaming detail scraper: polls DB for un-scraped jobs, scrapes sites sequentially.

    Args:
        upstream_done: Event set when discover+extract done. None = run once.
        my_done: Event to set when this stage completes.
        proxy_str: Proxy in host:port:user:pass format.
        poll_interval: Seconds to sleep when no pending jobs found.
    """
    if proxy_str:
        set_proxy(proxy_str)

    conn = init_db()
    audit = reconcile_unscored_jobs(conn)
    if audit["rejected"]:
        log.info("Discovery title gate excludes %d pending job(s) from enrichment", audit["rejected"])

    reset_count = reset_incomplete_apple_descriptions(conn)
    if reset_count:
        log.info("Apple: requeued %d incomplete descriptions", reset_count)
    amazon_reset_count = reset_incomplete_amazon_descriptions(conn)
    if amazon_reset_count:
        log.info("Amazon: requeued %d incomplete descriptions", amazon_reset_count)
    microsoft_reset_count = reset_incomplete_microsoft_descriptions(conn)
    if microsoft_reset_count:
        log.info("Microsoft: requeued %d incomplete descriptions", microsoft_reset_count)
    meta_reset_count = reset_incomplete_meta_descriptions(conn)
    if meta_reset_count:
        log.info("Meta: requeued %d incomplete descriptions", meta_reset_count)

    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute",
             url_stats['resolved'], url_stats['already_absolute'])

    total_ok = 0
    total_err = 0
    t0 = time.time()

    try:
        while True:
            skip_filter = " AND ".join(f"site != '{s}'" for s in SKIP_DETAIL_SITES)
            rows = conn.execute(
                "SELECT url, title, site FROM jobs "
                "WHERE detail_scraped_at IS NULL "
                "AND COALESCE(discovery_status, 'accepted') = 'accepted' "
                f"AND {skip_filter} "
                "ORDER BY site LIMIT 200"
            ).fetchall()

            if rows:
                site_jobs: dict[str, list[tuple]] = {}
                for row in rows:
                    url, title, site = row[0], row[1], row[2]
                    site_jobs.setdefault(site, []).append((url, title))

                for site, jobs in site_jobs.items():
                    delay = SITE_DELAYS.get(site, 2.0)
                    log.info("%s: %d jobs (delay=%.1fs)", site, len(jobs), delay)

                    try:
                        stats = scrape_site_batch(conn, site, jobs, delay=delay)
                        total_ok += stats["ok"] + stats["partial"]
                        total_err += stats["error"]
                        log.info("%s: %d ok, %d partial, %d error",
                                 site, stats['ok'], stats['partial'], stats['error'])
                    except Exception as e:
                        log.error("%s: CRASHED: %s", site, e)

            upstream_finished = upstream_done is None or upstream_done.is_set()
            if upstream_finished and not rows:
                break
            if not rows:
                time.sleep(poll_interval)
    finally:
        elapsed = time.time() - t0
        if total_ok or total_err:
            log.info("DONE: %d ok, %d errors in %.1fs", total_ok, total_err, elapsed)
        conn.close()
        my_done.set()


# -- Public entry point ------------------------------------------------------

def run_enrichment(limit: int = 100, workers: int = 3) -> dict:
    """Main entry point for detail page enrichment.

    Fetches pending jobs from the database (those without full_description),
    resolves relative URLs, then runs the three-tier extraction cascade on
    each detail page.

    Args:
        limit: Maximum number of jobs per site to process.
        workers: Number of parallel threads for site batch processing. Default 3.

    Returns:
        Dict with stats: processed, ok, partial, error, tiers.
    """
    conn = init_db()
    audit = reconcile_unscored_jobs(conn)
    if audit["rejected"]:
        log.info("Discovery title gate excludes %d pending job(s) from enrichment", audit["rejected"])

    reset_count = reset_incomplete_apple_descriptions(conn)
    if reset_count:
        log.info("Apple: requeued %d incomplete descriptions", reset_count)
    amazon_reset_count = reset_incomplete_amazon_descriptions(conn)
    if amazon_reset_count:
        log.info("Amazon: requeued %d incomplete descriptions", amazon_reset_count)
    microsoft_reset_count = reset_incomplete_microsoft_descriptions(conn)
    if microsoft_reset_count:
        log.info("Microsoft: requeued %d incomplete descriptions", microsoft_reset_count)
    meta_reset_count = reset_incomplete_meta_descriptions(conn)
    if meta_reset_count:
        log.info("Meta: requeued %d incomplete descriptions", meta_reset_count)

    # URL resolution first
    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute, %d failed",
             url_stats["resolved"], url_stats["already_absolute"], url_stats["failed"])

    # WTTJ special handling
    wttj_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchone()[0]
    if wttj_count > 0:
        sample = conn.execute(
            "SELECT url FROM jobs WHERE site = 'WelcomeToTheJungle' LIMIT 1"
        ).fetchone()
        if sample and not sample[0].startswith("http"):
            updated = resolve_wttj_urls(conn)
            log.info("WTTJ: %d URLs updated", updated)

    # Run the detail scraper
    stats = _run_detail_scraper(conn, max_per_site=limit, workers=workers)

    return stats
