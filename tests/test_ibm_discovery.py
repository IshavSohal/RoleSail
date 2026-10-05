import json

from rolesail.discovery import bigtech, greenhouse


def test_fetch_ibm_jobs_normalizes_deduplicates_and_paginates(monkeypatch):
    calls = []

    def fake_request(url, **kwargs):
        assert url == "https://www-api.ibm.com/search/api/v2"
        assert kwargs["headers"] == {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = json.loads(kwargs["data"])
        calls.append(body)
        assert body["appId"] == "careers"
        assert body["scopes"] == ["careers2"]
        assert body["size"] == 2

        if body["from"] == 0:
            hits = [
                {
                    "_id": "search-index-id",
                    "_source": {
                        "title": "Software Engineer",
                        "url": "https://careers.ibm.com/careers/JobDetail?jobId=123",
                        "description": "Build reliable software...",
                        "field_keyword_19": ["Toronto, CA", "Remote"],
                    },
                }
            ] * 2
        else:
            hits = [
                {
                    "_id": "other-search-index-id",
                    "_source": {
                        "title": "Backend Developer",
                        "url": "https://careers.ibm.com/careers/JobDetail?jobId=456",
                        "description": "Develop cloud services...",
                        "field_keyword_19": "Ottawa, CA",
                    },
                }
            ]
        return json.dumps(
            {"hits": {"total": {"value": 3, "relation": "eq"}, "hits": hits}}
        ).encode()

    monkeypatch.setattr(bigtech, "_http_request", fake_request)

    jobs = bigtech._fetch_ibm_jobs(
        {"max_pages": 5, "page_size": 2}, ["software engineer"]
    )

    assert [call["from"] for call in calls] == [0, 2]
    assert jobs == [
        {
            "title": "Software Engineer",
            "location": "Toronto, CA; Remote",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=123",
            "content": "Build reliable software...",
            "content_is_full": False,
            "posted_at": None,
            "application_url": "https://careers.ibm.com/careers/JobDetail?jobId=123",
        },
        {
            "title": "Backend Developer",
            "location": "Ottawa, CA",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=456",
            "content": "Develop cloud services...",
            "content_is_full": False,
            "posted_at": None,
            "application_url": "https://careers.ibm.com/careers/JobDetail?jobId=456",
        },
    ]


def test_bigtech_config_includes_supported_ibm_provider():
    ibm = greenhouse.load_bigtech_companies()["ibm"]

    assert ibm["name"] == "IBM"
    assert ibm["provider"] in greenhouse.BIGTECH_FETCHERS
