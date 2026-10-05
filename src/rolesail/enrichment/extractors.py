"""Deterministic and LLM-backed job-detail extractors."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError

from rolesail.llm import get_client

log = logging.getLogger(__name__)

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
    'button.apply-button',
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
    '.show-more-less-html__markup',
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
                    parent_href = el.evaluate("el => el.closest('a')?.href || null")
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


def reset_failed_linkedin_descriptions(conn: sqlite3.Connection) -> int:
    """Requeue LinkedIn pages missed by the former generic-only selectors."""
    cursor = conn.execute(
        "UPDATE jobs SET detail_scraped_at = NULL, detail_error = NULL "
        "WHERE (site = 'LinkedIn' OR strategy = 'linkedin_careers') "
        "AND full_description IS NULL "
        "AND detail_scraped_at IS NOT NULL "
        "AND detail_error = 'no data extracted'"
    )
    conn.commit()
    return cursor.rowcount
