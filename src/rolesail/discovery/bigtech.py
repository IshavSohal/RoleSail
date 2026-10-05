"""Adapters for proprietary employer career sites."""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import UTC, datetime

from bs4 import BeautifulSoup

from rolesail.discovery.shared import _http_request, _normalize_description

log = logging.getLogger(__name__)


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
    """Convert one Amazon API record to RoleSail's discovery job shape."""
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
    scoped to the employer's relevant job family before RoleSail applies its
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
