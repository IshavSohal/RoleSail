import json
import urllib.parse

from rolesail.discovery import ats, greenhouse


def test_fetch_netflix_jobs_normalizes_deduplicates_and_paginates(monkeypatch):
    calls = []

    def fake_request(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Accept"] == "application/json"
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["domain"] == ["netflix.com"]
        assert query["num"] == ["10"]
        start = int(query["start"][0])
        positions = []
        if start == 0:
            positions = [
                {
                    "id": 101,
                    "name": "Software Engineer",
                    "locations": ["USA - Remote"],
                    "canonicalPositionUrl": (
                        "https://explore.jobs.netflix.net/careers/job/101"
                    ),
                    "t_create": 1_722_470_400,
                }
            ] * 10
        elif start == 10:
            positions = [
                {
                    "id": 202,
                    "posting_name": "Backend Engineer",
                    "location": "Toronto, ON, Canada",
                }
            ]
        return json.dumps({"count": 11, "positions": positions}).encode()

    monkeypatch.setattr(greenhouse, "_http_request", fake_request)

    jobs = greenhouse._fetch_netflix_jobs(
        {"max_pages": 5}, ["software engineer"]
    )

    assert len(calls) == 2
    assert jobs == [
        {
            "title": "Software Engineer",
            "location": "USA - Remote",
            "url": "https://explore.jobs.netflix.net/careers/job/101",
            "content": "",
            "content_is_full": False,
            "posted_at": "2024-08-01",
            "application_url": "https://explore.jobs.netflix.net/careers/job/101",
        },
        {
            "title": "Backend Engineer",
            "location": "Toronto, ON, Canada",
            "url": "https://explore.jobs.netflix.net/careers/job/202",
            "content": "",
            "content_is_full": False,
            "posted_at": None,
            "application_url": "https://explore.jobs.netflix.net/careers/job/202",
        },
    ]


def test_company_registries_include_netflix_and_spotify():
    netflix = greenhouse.load_bigtech_companies()["netflix"]
    spotify = ats.load_lever_companies()["spotify"]

    assert netflix["name"] == "Netflix"
    assert netflix["provider"] in greenhouse.BIGTECH_FETCHERS
    assert spotify == {"name": "Spotify", "site": "spotify"}
