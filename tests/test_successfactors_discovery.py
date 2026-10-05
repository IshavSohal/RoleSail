import urllib.parse

from rolesail.discovery import bigtech, greenhouse


def test_fetch_successfactors_jobs_normalizes_and_paginates(monkeypatch):
    calls = []

    def fake_request(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"] == {"Accept": "text/html"}
        path = urllib.parse.urlparse(url).path
        rows = []
        if path.endswith("/technology/"):
            rows = [
                ("/job/Toronto-Software-Engineer/123/", "Software Engineer", "Toronto, ON, CA", "Aug 23, 2026"),
                ("/job/Ottawa-Developer/456/", "Developer", "Ottawa, ON, CA", "not-a-date"),
            ]
        elif path.endswith("/technology/2/"):
            rows = [
                ("/job/Remote-Backend-Engineer/789/", "Backend Engineer", "Remote", "Aug 22, 2026"),
            ]
        markup = "".join(
            f'''<tr class="data-row">
              <td class="colTitle"><a class="jobTitle-link" href="{href}">{title}</a></td>
              <td class="colDate"><span class="jobDate">{date}</span></td>
              <td class="colLocation"><span class="jobLocation">{location}</span></td>
            </tr>'''
            for href, title, location, date in rows
        )
        return markup.encode()

    monkeypatch.setattr(bigtech, "_http_request", fake_request)

    jobs = bigtech._fetch_successfactors_jobs(
        {
            "base_url": "https://jobs.example.com",
            "category_path": "/go/technology/",
            "page_size": 2,
            "max_pages": 5,
        },
        ["software engineer"],
    )

    assert len(calls) == 2
    assert jobs == [
        {
            "title": "Software Engineer",
            "location": "Toronto, ON, CA",
            "url": "https://jobs.example.com/job/Toronto-Software-Engineer/123/",
            "content": "",
            "content_is_full": False,
            "posted_at": "2026-08-23",
            "application_url": "https://jobs.example.com/job/Toronto-Software-Engineer/123/",
        },
        {
            "title": "Developer",
            "location": "Ottawa, ON, CA",
            "url": "https://jobs.example.com/job/Ottawa-Developer/456/",
            "content": "",
            "content_is_full": False,
            "posted_at": None,
            "application_url": "https://jobs.example.com/job/Ottawa-Developer/456/",
        },
        {
            "title": "Backend Engineer",
            "location": "Remote",
            "url": "https://jobs.example.com/job/Remote-Backend-Engineer/789/",
            "content": "",
            "content_is_full": False,
            "posted_at": "2026-08-22",
            "application_url": "https://jobs.example.com/job/Remote-Backend-Engineer/789/",
        },
    ]


def test_scotiabank_registry_uses_successfactors_adapter():
    scotiabank = greenhouse.load_bigtech_companies()["scotiabank"]

    assert scotiabank["name"] == "Scotiabank"
    assert scotiabank["provider"] in greenhouse.BIGTECH_FETCHERS
