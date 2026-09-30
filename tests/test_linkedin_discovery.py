import urllib.parse

from rolesail.discovery import greenhouse


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

    monkeypatch.setattr(greenhouse, "_http_request", fake_request)

    jobs = greenhouse._fetch_linkedin_jobs(
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
