"""Direct-employer discovery for Greenhouse and proprietary career sites.

Greenhouse exposes a free, unauthenticated JSON board API per company:

    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

When `content=true`, every posting is returned with its full HTML description,
location, departments, offices, and an `absolute_url` that doubles as the apply
URL -- so no enrichment call (and no LLM tokens) are required.

Greenhouse companies are loaded from `config/greenhouse_companies.yaml`.
Employers with proprietary career systems are loaded from
`config/bigtech_companies.yaml`. Both use the filters in `searches.yaml`.
"""

import html as html_module
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timezone

import yaml
from bs4 import BeautifulSoup

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db, is_job_within_retention_window
from applypilot.discovery.filters import classify_title, reconcile_unscored_jobs
from applypilot.discovery.workday import strip_html

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
API_BASE = "https://boards-api.greenhouse.io/v1/boards"


# -- Company registry from YAML ---------------------------------------------

def load_companies() -> dict:
    """Load Greenhouse company registry from config/greenhouse_companies.yaml."""
    path = CONFIG_DIR / "greenhouse_companies.yaml"
    if not path.exists():
        log.warning("greenhouse_companies.yaml not found at %s", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("companies", {})


def load_bigtech_companies() -> dict:
    """Load companies backed by proprietary career-site adapters."""
    path = CONFIG_DIR / "bigtech_companies.yaml"
    if not path.exists():
        log.warning("bigtech_companies.yaml not found at %s", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("companies", {})


# -- Filtering helpers -------------------------------------------------------

def _load_location_filter(search_cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """Load location accept/reject lists from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


def _location_ok(
    location: str | None,
    accept: list[str],
    reject: list[str],
    search_cfg: dict | None = None,
) -> bool:
    """Check if a job location passes the user's location filter."""
    policy = dict(search_cfg or {})
    policy.setdefault("location_accept", accept)
    policy.setdefault("location_reject_non_remote", reject)
    return config.location_is_allowed(location, policy)


def _load_query_terms(search_cfg: dict | None = None) -> list[str]:
    """Pull each `query` from `searches.yaml` and lowercase for substring matching.

    Returns the list of raw query strings (e.g. "software engineer", "backend
    developer"). Used for case-insensitive substring matching against job titles.
    """
    if search_cfg is None:
        search_cfg = config.load_search_config()
    queries = search_cfg.get("queries", [])
    terms = []
    for q in queries:
        if isinstance(q, dict) and q.get("query"):
            terms.append(str(q["query"]).lower().strip())
        elif isinstance(q, str):
            terms.append(q.lower().strip())
    return [t for t in terms if t]


def _load_excluded_titles(search_cfg: dict | None = None) -> list[str]:
    """Load the `exclude_titles` list from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()
    excludes = search_cfg.get("exclude_titles", [])
    return [str(e).lower().strip() for e in excludes if e]


def _title_matches(title: str | None, terms: list[str], excludes: list[str]) -> bool:
    """Case-insensitive substring match: title must contain any query term and
    must not contain any excluded phrase.
    """
    policy = {
        "include_titles": terms,
        "exclude_titles": excludes,
    }
    return classify_title(title, policy).accepted


# -- HTTP fetch --------------------------------------------------------------

def _http_get_json(url: str, max_retries: int = 3, backoff: float = 2.0) -> dict:
    """GET a URL with retries on 429 / transient failures. Returns parsed JSON."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                raise
            if e.code == 429 and attempt < max_retries:
                wait = backoff * (attempt + 1) * 2
                log.warning("429 from %s, retry %d/%d in %.0fs", url, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            if attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("HTTP %s from %s, retry %d/%d in %.0fs",
                            e.code, url, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Transient error on %s: %s -- retry %d/%d in %.0fs",
                            url, e, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            raise
    if last_err:
        raise last_err
    return {}


def _http_request(
    url: str,
    data: bytes | None = None,
    headers: dict | None = None,
    max_retries: int = 3,
    backoff: float = 2.0,
) -> bytes:
    """Make a GET or POST request with retries and return its raw body."""
    request_headers = {"User-Agent": UA}
    request_headers.update(headers or {})
    last_err: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=request_headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in (429, 500, 502, 503, 504) or attempt >= max_retries:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt >= max_retries:
                raise

        wait = backoff * (attempt + 1)
        log.warning(
            "Transient error from %s, retry %d/%d in %.0fs",
            url,
            attempt + 1,
            max_retries,
            wait,
        )
        time.sleep(wait)

    if last_err:
        raise last_err
    return b""


def fetch_company_jobs(board_token: str) -> list[dict]:
    """Fetch all jobs for a Greenhouse board, with descriptions inlined.

    Returns the raw `jobs` list from the API (each item has keys like `id`,
    `title`, `absolute_url`, `location`, `content`, `updated_at`, `departments`,
    `offices`, `metadata`).
    """
    url = f"{API_BASE}/{board_token}/jobs?content=true"
    data = _http_get_json(url)
    return data.get("jobs", []) or []


# -- Description normalization ----------------------------------------------

def _normalize_description(content: str | None) -> str:
    """Greenhouse returns `content` as HTML-escaped HTML (entities like
    `&lt;p&gt;`). Unescape once so the HTML stripper can do its job, then
    convert tags to plain text.
    """
    if not content:
        return ""
    decoded = html_module.unescape(content)
    return strip_html(decoded)


# -- Proprietary career-site adapters ----------------------------------------

def _fetch_google_jobs(company: dict, terms: list[str]) -> list[dict]:
    """Fetch jobs from the Google Careers result payload."""
    base = "https://www.google.com/about/careers/applications/jobs/results/"
    jobs: dict[str, dict] = {}
    for term in terms or [""]:
        for page in range(1, int(company.get("max_pages", 5)) + 1):
            params = {"q": term}
            if page > 1:
                params["page"] = str(page)
            text = _http_request(f"{base}?{urllib.parse.urlencode(params)}").decode("utf-8")
            match = re.search(
                r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel:",
                text,
                re.DOTALL,
            )
            if not match:
                raise ValueError("Google Careers result payload was not found")
            payload = json.loads(match.group(1))
            results = payload[0] if payload and isinstance(payload[0], list) else []
            for item in results:
                if not isinstance(item, list) or len(item) < 11:
                    continue
                job_id, title = str(item[0]), item[1]
                locations = item[9] if isinstance(item[9], list) else []
                location = "; ".join(
                    str(loc[0])
                    for loc in locations
                    if isinstance(loc, list) and loc and loc[0]
                )
                content_parts = []
                for index in (10, 4, 3):
                    field = item[index] if len(item) > index else None
                    if isinstance(field, list) and len(field) > 1 and field[1]:
                        content_parts.append(str(field[1]))
                jobs[job_id] = {
                    "title": title,
                    "location": location,
                    "url": f"{base}{job_id}",
                    "content": "\n".join(content_parts),
                }
            if len(results) < 20:
                break
    return list(jobs.values())


AMAZON_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
AMAZON_REQUEST_HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}


def _normalize_amazon_job(item: dict) -> dict | None:
    """Convert one Amazon API record to ApplyPilot's discovery job shape."""
    job_id = str(item.get("id_icims") or item.get("id") or "")
    path = item.get("job_path") or ""
    if not job_id or not path:
        return None

    description_parts = []
    if item.get("description"):
        description_parts.append(str(item["description"]))
    if item.get("basic_qualifications"):
        description_parts.append(
            "Basic Qualifications<br/><br/>"
            + str(item["basic_qualifications"])
        )
    if item.get("preferred_qualifications"):
        description_parts.append(
            "Preferred Qualifications<br/><br/>"
            + str(item["preferred_qualifications"])
        )

    # Amazon appends its compensation disclosure to the preferred
    # qualifications field instead of exposing a dedicated salary property.
    preferred = _normalize_description(item.get("preferred_qualifications"))
    salary = next(
        (
            line
            for line in reversed(preferred.splitlines())
            if re.search(
                r"\d[\d,.]*\s*-\s*\d[\d,.]*\s+[A-Z]{3}\b",
                line,
            )
        ),
        None,
    )
    return {
        "id": job_id,
        "title": item.get("title"),
        "company": "Amazon",
        "location": item.get("normalized_location") or item.get("location"),
        "url": urllib.parse.urljoin("https://www.amazon.jobs", path),
        "content": "<br/><br/>".join(description_parts)
        or item.get("description_short"),
        # Do not mark a partial API record as enriched. A normal Amazon
        # posting has both qualification fields.
        "content_is_full": bool(
            item.get("basic_qualifications")
            and item.get("preferred_qualifications")
        ),
        "salary": salary,
        # Amazon's search API has returned obsolete account hosts here. The
        # public job page consistently links through this canonical route,
        # which redirects into Amazon's authenticated passport flow.
        "application_url": f"https://www.amazon.jobs/applicant/jobs/{job_id}/apply",
        "posted_at": item.get("posted_date"),
    }


def fetch_amazon_job(job_id: str) -> dict | None:
    """Fetch one exact Amazon job by its numeric public job ID."""
    job_id = str(job_id).strip()
    if not job_id.isdigit():
        return None
    params = {"base_query": job_id, "result_limit": 10, "offset": 0}
    data = json.loads(
        _http_request(
            f"{AMAZON_SEARCH_URL}?{urllib.parse.urlencode(params)}",
            headers=AMAZON_REQUEST_HEADERS,
        )
    )
    for item in data.get("jobs", []) or []:
        candidate_id = str(item.get("id_icims") or item.get("id") or "")
        if candidate_id == job_id:
            return _normalize_amazon_job(item)
    return None


def _fetch_amazon_jobs(company: dict, terms: list[str]) -> list[dict]:
    """Fetch jobs from Amazon Jobs' JSON search endpoint."""
    jobs: dict[str, dict] = {}
    page_size = int(company.get("page_size", 100))
    for term in terms or [""]:
        for page in range(int(company.get("max_pages", 5))):
            params = {
                "base_query": term,
                "result_limit": page_size,
                "offset": page * page_size,
            }
            data = json.loads(
                _http_request(
                    f"{AMAZON_SEARCH_URL}?{urllib.parse.urlencode(params)}",
                    headers=AMAZON_REQUEST_HEADERS,
                )
            )
            results = data.get("jobs", []) or []
            for item in results:
                job = _normalize_amazon_job(item)
                if job:
                    jobs[job["id"]] = job
            if len(results) < page_size:
                break
    return list(jobs.values())


def _fetch_apple_jobs(company: dict, terms: list[str]) -> list[dict]:
    """Fetch jobs from Apple Careers' server-rendered hydration data."""
    base = "https://jobs.apple.com/en-us/search"
    jobs: dict[str, dict] = {}
    for term in terms or [""]:
        for page in range(1, int(company.get("max_pages", 5)) + 1):
            params: dict[str, str | int] = {
                "search": re.sub(r"\s+", "-", term.strip()),
                "page": page,
            }
            if company.get("location"):
                params["location"] = company["location"]
            text = _http_request(
                f"{base}?{urllib.parse.urlencode(params)}",
                headers={"Accept": "text/html"},
            ).decode("utf-8")
            match = re.search(
                r'window\.__staticRouterHydrationData = JSON\.parse\("(.*?)"\);',
                text,
                re.DOTALL,
            )
            if not match:
                raise ValueError("Apple Careers hydration payload was not found")
            payload = json.loads(json.loads(f'"{match.group(1)}"'))
            search = payload.get("loaderData", {}).get("search", {})
            results = search.get("searchResults", []) or []
            for item in results:
                job_id = str(item.get("positionId") or item.get("reqId") or "")
                if not job_id:
                    continue
                locations = item.get("locations", []) or []
                location = "; ".join(
                    ", ".join(
                        part
                        for part in (loc.get("name"), loc.get("countryName"))
                        if part
                    )
                    for loc in locations
                    if isinstance(loc, dict)
                )
                slug = item.get("transformedPostingTitle") or "job"
                jobs[job_id] = {
                    "title": item.get("postingTitle"),
                    "location": location,
                    "url": f"https://jobs.apple.com/en-us/details/{job_id}/{slug}",
                    "content": item.get("jobSummary"),
                    # Search results expose only the Summary section.  The
                    # remaining Apple sections live on the detail page.
                    "content_is_full": False,
                    "posted_at": item.get("postDateInGMT") or item.get("postingDate"),
                }
            if len(results) < 20:
                break
    return list(jobs.values())


def _fetch_meta_jobs(company: dict, terms: list[str]) -> list[dict]:
    """Fetch jobs from Meta Careers' persisted GraphQL query."""
    endpoint = "https://www.metacareers.com/graphql"
    doc_id = str(company.get("doc_id", "29615178951461218"))
    lsd = str(company.get("lsd", "AdFL9XlD5sA"))
    jobs: dict[str, dict] = {}
    for term in terms or [""]:
        variables = {
            "search_input": {
                "q": term or None,
                "divisions": [],
                "offices": [],
                "roles": [],
                "leadership_levels": [],
                "saved_jobs": [],
                "saved_searches": [],
                "sub_teams": [],
                "teams": [],
                "is_leadership": False,
                "is_remote_only": False,
                "sort_by_new": False,
                "results_per_page": None,
            }
        }
        body = urllib.parse.urlencode(
            {
                "lsd": lsd,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "CareersJobSearchResultsDataQuery",
                "variables": json.dumps(variables),
                "doc_id": doc_id,
            }
        ).encode("utf-8")
        payload = json.loads(
            _http_request(
                endpoint,
                data=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "x-fb-friendly-name": "CareersJobSearchResultsDataQuery",
                    "x-fb-lsd": lsd,
                },
            )
        )
        results = (
            payload.get("data", {})
            .get("job_search_with_featured_jobs", {})
            .get("all_jobs", [])
            or []
        )
        for item in results:
            job_id = str(item.get("id") or "")
            if not job_id:
                continue
            locations = item.get("locations", []) or []
            jobs[job_id] = {
                "title": item.get("title"),
                "location": "; ".join(str(loc) for loc in locations),
                "url": f"https://www.metacareers.com/jobs/{job_id}",
                "content": "",
                "content_is_full": False,
            }
    return list(jobs.values())


def _fetch_microsoft_job_details(job: dict) -> None:
    """Attach Microsoft's authoritative full description to one search result."""
    origin = "https://apply.careers.microsoft.com"
    details_endpoint = f"{origin}/api/pcsx/position_details"
    job_id_match = re.search(r"/job/(\d+)", job.get("url") or "")
    if not job_id_match:
        return

    job_id = job_id_match.group(1)
    params = {
        "position_id": job_id,
        "domain": "microsoft.com",
        "hl": "en",
    }
    try:
        payload = json.loads(
            _http_request(
                f"{details_endpoint}?{urllib.parse.urlencode(params)}",
                headers={
                    "Accept": "application/json",
                    "Referer": job["url"],
                },
            )
        )
        details = payload.get("data", {}) or {}
        description = details.get("jobDescription") or ""
        if description:
            job["content"] = description
            job["content_is_full"] = True
            job["application_url"] = job["url"]
    except (OSError, TypeError, ValueError) as exc:
        # Leave the posting pending for the normal browser enrichment cascade
        # rather than failing the entire Microsoft discovery run.
        log.warning("Microsoft: could not fetch details for %s: %s", job_id, exc)


def _fetch_microsoft_jobs(company: dict, terms: list[str]) -> list[dict]:
    """Fetch jobs from Microsoft's public Eightfold/PCSX search endpoint."""
    origin = "https://apply.careers.microsoft.com"
    endpoint = f"{origin}/api/pcsx/search"
    jobs: dict[str, dict] = {}
    page_size = 10
    max_pages = int(company.get("max_pages", 5))

    for term in terms or [""]:
        for page in range(max_pages):
            params = {
                "domain": "microsoft.com",
                "query": term,
                "start": page * page_size,
                "sort_by": "relevance",
            }
            payload = json.loads(
                _http_request(
                    f"{endpoint}?{urllib.parse.urlencode(params)}",
                    headers={"Accept": "application/json"},
                )
            )
            data = payload.get("data", {}) or {}
            results = data.get("positions", []) or []
            for item in results:
                job_id = str(item.get("id") or "")
                if not job_id:
                    continue
                locations = item.get("locations") or item.get("standardizedLocations") or []
                position_path = item.get("positionUrl") or f"/careers/job/{job_id}"
                posted_at = None
                if item.get("postedTs"):
                    try:
                        posted_at = datetime.fromtimestamp(
                            int(item["postedTs"]), UTC
                        ).date().isoformat()
                    except (TypeError, ValueError, OverflowError):
                        pass
                jobs[job_id] = {
                    "title": item.get("name"),
                    "location": "; ".join(str(location) for location in locations),
                    "url": urllib.parse.urljoin(origin, position_path),
                    "content": "",
                    "content_is_full": False,
                    "posted_at": posted_at,
                }

            total = int(data.get("count") or len(results))
            if len(results) < page_size or (page + 1) * page_size >= total:
                break

    return list(jobs.values())


def _fetch_netflix_jobs(company: dict, terms: list[str]) -> list[dict]:
    """Fetch jobs from Netflix's public Eightfold careers endpoint."""
    origin = "https://explore.jobs.netflix.net"
    endpoint = f"{origin}/api/apply/v2/jobs"
    jobs: dict[str, dict] = {}
    page_size = 10  # Eightfold currently caps this endpoint at ten results.
    max_pages = int(company.get("max_pages", 5))

    for term in terms or [""]:
        for page in range(max_pages):
            params = {
                "domain": "netflix.com",
                "query": term,
                "start": page * page_size,
                "num": page_size,
            }
            payload = json.loads(
                _http_request(
                    f"{endpoint}?{urllib.parse.urlencode(params)}",
                    headers={"Accept": "application/json"},
                )
            )
            results = payload.get("positions", []) or []
            for item in results:
                job_id = str(item.get("id") or "")
                if not job_id:
                    continue
                locations = item.get("locations") or []
                location = "; ".join(str(value) for value in locations if value)
                if not location:
                    location = str(item.get("location") or "")
                posted_at = None
                if item.get("t_create"):
                    try:
                        posted_at = datetime.fromtimestamp(
                            int(item["t_create"]), UTC
                        ).date().isoformat()
                    except (TypeError, ValueError, OverflowError):
                        pass
                url = (
                    item.get("canonicalPositionUrl")
                    or f"{origin}/careers/job/{job_id}"
                )
                jobs[job_id] = {
                    "title": item.get("name") or item.get("posting_name"),
                    "location": location,
                    "url": url,
                    # Eightfold search results omit the full job description;
                    # leave the posting queued for normal detail enrichment.
                    "content": "",
                    "content_is_full": False,
                    "posted_at": posted_at,
                    "application_url": url,
                }

            total = int(payload.get("count") or len(results))
            if len(results) < page_size or (page + 1) * page_size >= total:
                break

    return list(jobs.values())


def _fetch_ibm_jobs(company: dict, terms: list[str]) -> list[dict]:
    """Fetch jobs from IBM's public careers search index."""
    endpoint = "https://www-api.ibm.com/search/api/v2"
    jobs: dict[str, dict] = {}
    page_size = max(1, int(company.get("page_size", 30)))
    max_pages = int(company.get("max_pages", 5))
    source_fields = [
        "_id",
        "title",
        "url",
        "description",
        "language",
        "field_keyword_17",
        "field_keyword_08",
        "field_keyword_18",
        "field_keyword_19",
    ]

    for term in terms or [""]:
        for page in range(max_pages):
            query: dict = {"match_all": {}}
            if term:
                query = {
                    "bool": {
                        "must": [
                            {
                                "simple_query_string": {
                                    "query": term,
                                    "fields": [
                                        "keywords^1",
                                        "body^1",
                                        "url^2",
                                        "description^2",
                                        "h1s_content^2",
                                        "title^3",
                                        "field_text_01",
                                    ],
                                }
                            }
                        ]
                    }
                }
            body = {
                "appId": "careers",
                "scopes": ["careers2"],
                "query": query,
                "from": page * page_size,
                "size": page_size,
                "sort": [{"_score": "desc"}, {"pageviews": "desc"}],
                "lang": "zz",
                "_source": source_fields,
            }
            payload = json.loads(
                _http_request(
                    endpoint,
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )
            )
            hits = payload.get("hits", {}) or {}
            results = hits.get("hits", []) or []
            for item in results:
                source = item.get("_source", {}) or {}
                url = str(source.get("url") or "").strip()
                job_id_match = re.search(r"[?&]jobId=([^&]+)", url)
                job_id = (
                    urllib.parse.unquote(job_id_match.group(1))
                    if job_id_match
                    else str(item.get("_id") or url)
                )
                if not job_id or not url:
                    continue
                location = source.get("field_keyword_19") or ""
                if isinstance(location, list):
                    location = "; ".join(str(value) for value in location if value)
                jobs[job_id] = {
                    "title": source.get("title"),
                    "location": str(location),
                    "url": url,
                    # Search results contain an excerpt, not the authoritative
                    # full posting. Leave the row queued for detail enrichment.
                    "content": source.get("description") or "",
                    "content_is_full": False,
                    "posted_at": None,
                    "application_url": url,
                }

            total_value = hits.get("total", len(results))
            if isinstance(total_value, dict):
                total = int(total_value.get("value") or 0)
            else:
                total = int(total_value or 0)
            if len(results) < page_size or (page + 1) * page_size >= total:
                break

    return list(jobs.values())


def _fetch_linkedin_jobs(company: dict, terms: list[str]) -> list[dict]:
    """Fetch LinkedIn's own jobs from its public guest search endpoint."""
    endpoint = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    jobs: dict[str, dict] = {}
    page_size = max(1, int(company.get("page_size", 10)))
    max_pages = int(company.get("max_pages", 5))
    company_id = str(company.get("company_id", "1337"))

    for term in terms or [""]:
        for page in range(max_pages):
            params = {
                "f_C": company_id,
                "keywords": term,
                "sortBy": "DD",
                "start": page * page_size,
            }
            markup = _http_request(
                f"{endpoint}?{urllib.parse.urlencode(params)}",
                headers={"Accept": "text/html"},
            ).decode("utf-8")
            soup = BeautifulSoup(markup, "html.parser")
            cards = soup.select("[data-entity-urn^='urn:li:jobPosting:']")
            for card in cards:
                entity_urn = str(card.get("data-entity-urn") or "")
                job_id = entity_urn.rpartition(":")[2]
                title_node = card.select_one(".base-search-card__title")
                location_node = card.select_one(".job-search-card__location")
                posted_node = card.select_one("time[datetime]")
                if not job_id or not title_node:
                    continue
                url = f"https://www.linkedin.com/jobs/view/{job_id}"
                jobs[job_id] = {
                    "title": title_node.get_text(" ", strip=True),
                    "location": (
                        location_node.get_text(" ", strip=True)
                        if location_node
                        else ""
                    ),
                    "url": url,
                    # Guest search cards do not include the description. The
                    # normal detail-enrichment cascade will fetch it later.
                    "content": "",
                    "content_is_full": False,
                    "posted_at": posted_node.get("datetime") if posted_node else None,
                    "application_url": url,
                }

            if len(cards) < page_size:
                break

    return list(jobs.values())


def _fetch_successfactors_jobs(company: dict, terms: list[str]) -> list[dict]:
    """Fetch postings from a public SAP SuccessFactors category page.

    SuccessFactors renders stable, server-side result tables. Category pages
    are preferable to the global search endpoint because they keep discovery
    scoped to the employer's relevant job family before ApplyPilot applies its
    normal title and location filters.
    """
    del terms  # Category scope plus the shared title filter handles relevance.
    base_url = str(company.get("base_url") or "").rstrip("/")
    category_path = str(company.get("category_path") or "").strip()
    if not base_url or not category_path:
        raise ValueError(
            "SuccessFactors company requires base_url and category_path"
        )

    page_size = max(1, int(company.get("page_size", 25)))
    max_pages = max(1, int(company.get("max_pages", 20)))
    jobs: dict[str, dict] = {}

    for page in range(max_pages):
        offset = page * page_size
        path = category_path.rstrip("/") + "/"
        if offset:
            path += f"{offset}/"
        params = urllib.parse.urlencode({
            "q": "",
            "sortColumn": "referencedate",
            "sortDirection": "desc",
        })
        url = f"{urllib.parse.urljoin(base_url + '/', path.lstrip('/'))}?{params}"
        markup = _http_request(
            url,
            headers={"Accept": "text/html"},
        ).decode("utf-8")
        soup = BeautifulSoup(markup, "html.parser")
        cards = soup.select("tr.data-row")

        for card in cards:
            title_node = card.select_one(".colTitle .jobTitle-link")
            location_node = card.select_one(".colLocation .jobLocation")
            posted_node = card.select_one(".colDate .jobDate")
            if not title_node or not title_node.get("href"):
                continue
            job_url = urllib.parse.urljoin(base_url + "/", title_node["href"])
            posted_at = None
            if posted_node:
                try:
                    posted_at = datetime.strptime(
                        posted_node.get_text(" ", strip=True), "%b %d, %Y"
                    ).replace(tzinfo=UTC).date().isoformat()
                except ValueError:
                    pass
            jobs[job_url] = {
                "title": title_node.get_text(" ", strip=True),
                "location": (
                    location_node.get_text(" ", strip=True)
                    if location_node
                    else ""
                ),
                "url": job_url,
                "content": "",
                "content_is_full": False,
                "posted_at": posted_at,
                "application_url": job_url,
            }

        if len(cards) < page_size:
            break

    return list(jobs.values())


BIGTECH_FETCHERS = {
    "google": _fetch_google_jobs,
    "amazon": _fetch_amazon_jobs,
    "apple": _fetch_apple_jobs,
    "meta": _fetch_meta_jobs,
    "microsoft": _fetch_microsoft_jobs,
    "netflix": _fetch_netflix_jobs,
    "ibm": _fetch_ibm_jobs,
    "linkedin": _fetch_linkedin_jobs,
    "successfactors": _fetch_successfactors_jobs,
}


# -- Per-company processing --------------------------------------------------

def _process_company(
    key: str,
    company: dict,
    terms: list[str],
    excludes: list[str],
    accept_locs: list[str],
    reject_locs: list[str],
    search_cfg: dict,
    location_filter: bool,
) -> dict:
    """Fetch + filter + store jobs for one Greenhouse company."""
    name = company.get("name", key)
    token = company.get("board_token", key)
    result = {"company": name, "found": 0, "kept": 0, "title_rejected": 0,
              "location_rejected": 0, "new": 0, "existing": 0, "error": None}

    try:
        raw_jobs = fetch_company_jobs(token)
    except Exception as e:
        log.error("%s: API error: %s", name, e)
        result["error"] = str(e)
        return result

    result["found"] = len(raw_jobs)
    log.info("%s: %d total postings", name, len(raw_jobs))

    if not raw_jobs:
        return result

    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple] = []

    for job in raw_jobs:
        title = job.get("title") or ""
        loc_obj = job.get("location") or {}
        location = loc_obj.get("name") if isinstance(loc_obj, dict) else None

        if not classify_title(title, search_cfg).accepted:
            result["title_rejected"] += 1
            continue
        enforce_location = location_filter or config.location_filter_is_mandatory(search_cfg)
        if enforce_location and not _location_ok(location, accept_locs, reject_locs, search_cfg):
            result["location_rejected"] += 1
            continue

        url = job.get("absolute_url") or ""
        if not url:
            continue

        full_description = _normalize_description(job.get("content"))
        short_desc = full_description[:500] if full_description else None

        detail_scraped_at = now if full_description and len(full_description) > 200 else None
        full_for_db = full_description if detail_scraped_at else None

        rows.append((
            url,
            title or None,
            name,
            None,  # salary -- Greenhouse doesn't expose this on the boards API
            short_desc,
            location,
            name,
            "greenhouse_api",
            now,
            full_for_db,
            url,
            detail_scraped_at,
        ))

    result["kept"] = len(rows)

    conn = get_connection()
    new = 0
    existing = 0
    for row in rows:
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, salary, description, location, site, "
                "strategy, discovered_at, full_description, application_url, "
                "detail_scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
    conn.commit()

    result["new"] = new
    result["existing"] = existing
    log.info(
        "%s: %d found, %d title-rejected, %d location-rejected, %d kept -> %d new, %d dupes",
        name, result["found"], result["title_rejected"], result["location_rejected"],
        len(rows), new, existing,
    )
    return result


def _process_bigtech_company(
    key: str,
    company: dict,
    terms: list[str],
    excludes: list[str],
    accept_locs: list[str],
    reject_locs: list[str],
    search_cfg: dict,
    location_filter: bool,
) -> dict:
    """Fetch, normalize, filter, and store one proprietary career site."""
    name = company.get("name", key)
    provider = company.get("provider", key)
    result = {
        "company": name,
        "found": 0,
        "kept": 0,
        "title_rejected": 0,
        "location_rejected": 0,
        "new": 0,
        "existing": 0,
        "error": None,
    }
    fetcher = BIGTECH_FETCHERS.get(provider)
    if not fetcher:
        result["error"] = f"unsupported provider: {provider}"
        return result

    try:
        raw_jobs = fetcher(company, terms)
    except Exception as e:
        log.error("%s: careers adapter error: %s", name, e)
        result["error"] = str(e)
        return result

    result["found"] = len(raw_jobs)
    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple[tuple, str, bool]] = []
    for job in raw_jobs:
        title = job.get("title") or ""
        location = job.get("location") or None
        if not classify_title(title, search_cfg).accepted:
            result["title_rejected"] += 1
            continue
        enforce_location = location_filter or config.location_filter_is_mandatory(search_cfg)
        if enforce_location and not _location_ok(location, accept_locs, reject_locs, search_cfg):
            result["location_rejected"] += 1
            continue
        url = job.get("url") or ""
        if not url:
            continue
        if not is_job_within_retention_window(job.get("posted_at"), reference_at=now):
            continue

        # Microsoft's JobPosting JSON-LD omits Overview. Fetch the complete
        # structured HTML only after filtering, so rejected search results do
        # not incur an unnecessary detail request.
        if provider == "microsoft" and not job.get("content_is_full"):
            _fetch_microsoft_job_details(job)

        description = _normalize_description(job.get("content"))
        content_is_full = job.get("content_is_full", True)
        full_description = description if content_is_full else ""
        detail_scraped_at = now if len(full_description) > 200 else None
        row = (
            url,
            title or None,
            name,
            job.get("salary"),
            description[:500] if description else None,
            location,
            name,
            f"{provider}_careers",
            now,
            job.get("posted_at"),
            full_description if detail_scraped_at else None,
            job.get("application_url") or url,
            detail_scraped_at,
        )
        rows.append((row, description, content_is_full))

    result["kept"] = len(rows)
    conn = get_connection()
    for row, description, content_is_full in rows:
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, salary, description, location, site, "
                "strategy, discovered_at, posted_at, full_description, application_url, "
                "detail_scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            result["new"] += 1
        except sqlite3.IntegrityError:
            # Older versions stored Apple's search-result summary as the full
            # description and marked the job enriched. Make those rows pending
            # again, without overwriting a genuinely enriched description.
            if not content_is_full:
                conn.execute(
                    "UPDATE jobs SET full_description = NULL, detail_scraped_at = NULL "
                    "WHERE url = ? AND full_description = ?",
                    (row[0], description),
                )
            elif provider in {"amazon", "microsoft"}:
                # These APIs supply complete, authoritative descriptions.
                # Refresh rows created by older versions that stored only a
                # partial description and therefore skipped enrichment.
                conn.execute(
                    "UPDATE jobs SET salary = COALESCE(?, salary), description = ?, "
                    "full_description = ?, application_url = COALESCE(?, application_url), "
                    "detail_scraped_at = ? WHERE url = ?",
                    (row[3], row[4], row[10], row[11], row[12], row[0]),
                )
            elif provider == "successfactors":
                # A dashboard upload may have enriched the posting before the
                # employer adapter sees it. Preserve that enrichment and score,
                # but replace generic upload metadata with authoritative fields
                # from the SuccessFactors result row.
                conn.execute(
                    "UPDATE jobs SET title = ?, company = ?, site = ?, strategy = ?, "
                    "description = COALESCE(description, ?), "
                    "posted_at = COALESCE(posted_at, ?), "
                    "location = COALESCE(?, location) WHERE url = ?",
                    (
                        row[1], row[2], row[6], row[7], row[4], row[9],
                        row[5], row[0],
                    ),
                )
            conn.execute(
                "UPDATE jobs SET posted_at = COALESCE(posted_at, ?), "
                "location = COALESCE(?, location) WHERE url = ?",
                (row[9], row[5], row[0]),
            )
            result["existing"] += 1
    conn.commit()
    log.info(
        "%s: %d found, %d title-rejected, %d location-rejected, %d kept -> %d new, %d dupes",
        name,
        result["found"],
        result["title_rejected"],
        result["location_rejected"],
        result["kept"],
        result["new"],
        result["existing"],
    )
    return result


# -- Public entry point ------------------------------------------------------

def run_greenhouse_discovery(
    companies: dict | None = None,
    workers: int = 3,
) -> dict:
    """Main entry point for Greenhouse-based discovery.

    Loads the company registry from `config/greenhouse_companies.yaml` (or uses
    the provided dict), then loads search queries + location filters from the
    user's `searches.yaml` and pulls every matching posting in a single API
    call per company.

    Args:
        companies: Override the company registry. If None, loads from YAML.
        workers: Number of parallel threads for company scraping. Default 3.

    Returns:
        Dict with stats: found, kept, new, existing, errors, companies.
    """
    if companies is None:
        companies = load_companies()

    if not companies:
        log.warning("No Greenhouse companies configured. Create config/greenhouse_companies.yaml.")
        return {"found": 0, "kept": 0, "new": 0, "existing": 0, "errors": 0, "companies": 0}

    search_cfg = config.load_search_config()
    reconcile_unscored_jobs(init_db(), search_cfg)
    terms = _load_query_terms(search_cfg)
    excludes = _load_excluded_titles(search_cfg)
    accept_locs, reject_locs = _load_location_filter(search_cfg)
    location_filter = search_cfg.get("greenhouse_location_filter", True)

    log.info("Greenhouse crawl: %d companies | %d query terms | %d excludes | workers=%d",
             len(companies), len(terms), len(excludes), workers)

    keys = list(companies.keys())
    grand: dict = {"found": 0, "kept": 0, "title_rejected": 0,
                   "location_rejected": 0, "new": 0, "existing": 0, "errors": 0,
                   "companies": len(keys)}
    t0 = time.time()

    if workers > 1 and len(keys) > 1:
        completed = 0
        with ThreadPoolExecutor(max_workers=min(workers, len(keys))) as pool:
            futures = {
                pool.submit(
                    _process_company, key, companies[key],
                    terms, excludes, accept_locs, reject_locs, search_cfg, location_filter,
                ): key
                for key in keys
            }
            for fut in as_completed(futures):
                r = fut.result()
                completed += 1
                grand["found"] += r["found"]
                grand["kept"] += r["kept"]
                grand["title_rejected"] += r["title_rejected"]
                grand["location_rejected"] += r["location_rejected"]
                grand["new"] += r["new"]
                grand["existing"] += r["existing"]
                if r["error"]:
                    grand["errors"] += 1
                if completed % 5 == 0 or completed == len(keys):
                    elapsed = time.time() - t0
                    log.info("Greenhouse progress: %d/%d (%d new, %d dupes, %d errors) [%.0fs]",
                             completed, len(keys), grand["new"], grand["existing"],
                             grand["errors"], elapsed)
    else:
        for i, key in enumerate(keys, 1):
            r = _process_company(
                key, companies[key],
                terms, excludes, accept_locs, reject_locs, search_cfg, location_filter,
            )
            grand["found"] += r["found"]
            grand["kept"] += r["kept"]
            grand["title_rejected"] += r["title_rejected"]
            grand["location_rejected"] += r["location_rejected"]
            grand["new"] += r["new"]
            grand["existing"] += r["existing"]
            if r["error"]:
                grand["errors"] += 1
            if i % 5 == 0 or i == len(keys):
                elapsed = time.time() - t0
                log.info("Greenhouse progress: %d/%d (%d new, %d dupes, %d errors) [%.0fs]",
                         i, len(keys), grand["new"], grand["existing"],
                         grand["errors"], elapsed)

    elapsed = time.time() - t0
    log.info(
        "Greenhouse crawl done in %.0fs: %d found, %d title-rejected, "
        "%d location-rejected, %d kept, %d new, %d dupes, %d errors",
        elapsed, grand["found"], grand["title_rejected"], grand["location_rejected"],
        grand["kept"], grand["new"], grand["existing"], grand["errors"],
    )
    return grand


def run_bigtech_discovery(
    companies: dict | None = None,
    workers: int = 3,
) -> dict:
    """Discover jobs from configured proprietary big-tech career sites."""
    if companies is None:
        companies = load_bigtech_companies()
    if not companies:
        return {
            "found": 0,
            "kept": 0,
            "new": 0,
            "existing": 0,
            "errors": 0,
            "companies": 0,
        }

    search_cfg = config.load_search_config()
    reconcile_unscored_jobs(init_db(), search_cfg)
    terms = _load_query_terms(search_cfg)
    excludes = _load_excluded_titles(search_cfg)
    accept_locs, reject_locs = _load_location_filter(search_cfg)
    location_filter = search_cfg.get(
        "bigtech_location_filter",
        search_cfg.get("greenhouse_location_filter", True),
    )
    keys = list(companies)
    grand = {
        "found": 0,
        "kept": 0,
        "title_rejected": 0,
        "location_rejected": 0,
        "new": 0,
        "existing": 0,
        "errors": 0,
        "companies": len(keys),
    }

    def add_result(result: dict) -> None:
        for field in ("found", "kept", "title_rejected", "location_rejected", "new", "existing"):
            grand[field] += result[field]
        if result["error"]:
            grand["errors"] += 1

    log.info(
        "Big-tech crawl: %d companies | %d query terms | %d excludes | workers=%d",
        len(keys),
        len(terms),
        len(excludes),
        workers,
    )
    if workers > 1 and len(keys) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(keys))) as pool:
            futures = [
                pool.submit(
                    _process_bigtech_company,
                    key,
                    companies[key],
                    terms,
                    excludes,
                    accept_locs,
                    reject_locs,
                    search_cfg,
                    location_filter,
                )
                for key in keys
            ]
            for future in as_completed(futures):
                add_result(future.result())
    else:
        for key in keys:
            add_result(
                _process_bigtech_company(
                    key,
                    companies[key],
                    terms,
                    excludes,
                    accept_locs,
                    reject_locs,
                    search_cfg,
                    location_filter,
                )
            )

    log.info(
        "Big-tech crawl done: %d found, %d title-rejected, %d location-rejected, "
        "%d kept, %d new, %d dupes, %d errors",
        grand["found"],
        grand["title_rejected"],
        grand["location_rejected"],
        grand["kept"],
        grand["new"],
        grand["existing"],
        grand["errors"],
    )
    return grand
