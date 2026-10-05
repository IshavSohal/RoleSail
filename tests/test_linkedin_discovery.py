import sqlite3
import urllib.parse

from rolesail.discovery import bigtech, greenhouse
from rolesail.enrichment.detail import (
    extract_apply_url_deterministic,
    extract_description_deterministic,
    reset_failed_linkedin_descriptions,
)


def _job_card(job_id: str, title: str, location: str, posted_at: str) -> str:
    return f"""
    <li>
      <div class="base-search-card" data-entity-urn="urn:li:jobPosting:{job_id}">
        <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/example-{job_id}"></a>
        <h3 class="base-search-card__title"> {title} </h3>
        <span class="job-search-card__location"> {location} </span>
        <time class="job-search-card__listdate" datetime="{posted_at}">1 day ago</time>
      </div>
    </li>
    """


def test_fetch_linkedin_jobs_normalizes_deduplicates_and_paginates(monkeypatch):
    calls = []

    def fake_request(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Accept"] == "text/html"
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["f_C"] == ["1337"]
        assert query["sortBy"] == ["DD"]
        start = int(query["start"][0])
        if start == 0:
            return (
                _job_card("101", "Software Engineer", "Toronto, ON", "2026-08-16")
                + _job_card("101", "Software Engineer", "Toronto, ON", "2026-08-16")
            ).encode()
        return _job_card(
            "202", "Backend Engineer", "Mountain View, CA", "2026-08-15"
        ).encode()

    monkeypatch.setattr(bigtech, "_http_request", fake_request)

    jobs = bigtech._fetch_linkedin_jobs(
        {"company_id": "1337", "max_pages": 5, "page_size": 2},
        ["software engineer"],
    )

    assert len(calls) == 2
    assert jobs == [
        {
            "title": "Software Engineer",
            "location": "Toronto, ON",
            "url": "https://www.linkedin.com/jobs/view/101",
            "content": "",
            "content_is_full": False,
            "posted_at": "2026-08-16",
            "application_url": "https://www.linkedin.com/jobs/view/101",
        },
        {
            "title": "Backend Engineer",
            "location": "Mountain View, CA",
            "url": "https://www.linkedin.com/jobs/view/202",
            "content": "",
            "content_is_full": False,
            "posted_at": "2026-08-15",
            "application_url": "https://www.linkedin.com/jobs/view/202",
        },
    ]


def test_bigtech_config_includes_supported_linkedin_provider():
    linkedin = greenhouse.load_bigtech_companies()["linkedin"]

    assert linkedin["name"] == "LinkedIn"
    assert linkedin["company_id"] == "1337"
    assert linkedin["page_size"] == 10
    assert linkedin["provider"] in greenhouse.BIGTECH_FETCHERS


class _LinkedInElement:
    def __init__(self, text: str, tag: str, href: str | None = None):
        self._text = text
        self._tag = tag
        self._href = href

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._href if name == "href" else None

    def evaluate(self, expression):
        if "tagName" in expression:
            return self._tag
        if "parentElement" in expression:
            return "https://www.linkedin.com/company/linkedin"
        return None


class _LinkedInPage:
    url = "https://www.linkedin.com/jobs/view/123"

    def query_selector(self, selector):
        if selector == ".show-more-less-html__markup":
            return _LinkedInElement(
                "About the role\n" + "Build reliable software systems. " * 5,
                "div",
            )
        if selector == "button.apply-button":
            return _LinkedInElement("Apply", "button")
        return None

    def query_selector_all(self, _selector):
        return []


def test_linkedin_detail_markup_is_extracted_without_llm_fallback():
    page = _LinkedInPage()

    description = extract_description_deterministic(page)

    assert description is not None
    assert description.startswith("About the role\nBuild reliable software systems.")
    assert extract_apply_url_deterministic(page) == page.url


def test_failed_linkedin_descriptions_are_requeued_selectively():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (site TEXT, strategy TEXT, full_description TEXT, "
        "detail_scraped_at TEXT, detail_error TEXT)"
    )
    conn.executemany(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
        [
            ("LinkedIn", "linkedin_careers", None, "2026-10-01", "no data extracted"),
            ("LinkedIn", "linkedin_careers", None, "2026-10-01", "HTTP 403"),
            ("LinkedIn", "linkedin_careers", "Complete description", "2026-10-01", None),
            ("Other", "other", None, "2026-10-01", "no data extracted"),
        ],
    )

    assert reset_failed_linkedin_descriptions(conn) == 1
    rows = conn.execute(
        "SELECT detail_scraped_at, detail_error FROM jobs ORDER BY rowid"
    ).fetchall()
    assert rows == [
        (None, None),
        ("2026-10-01", "HTTP 403"),
        ("2026-10-01", None),
        ("2026-10-01", "no data extracted"),
    ]
