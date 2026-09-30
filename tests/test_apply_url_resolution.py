from rolesail.enrichment.detail import (
    extract_apply_url_deterministic,
    normalize_application_url,
)


class _Link:
    def get_attribute(self, name):
        return "/gv-form/?id=54f473e7-f225-44b3-beb7-b5d50403fe02" if name == "href" else None

    def evaluate(self, _script):
        return "a"


class _Page:
    url = "https://careers.veeva.com/job/example"

    def query_selector(self, selector):
        return _Link() if selector == 'a[href*="apply"]' else None

    def query_selector_all(self, _selector):
        return []


def test_relative_apply_link_is_resolved_against_job_page():
    assert extract_apply_url_deterministic(_Page()) == (
        "https://careers.veeva.com/gv-form/"
        "?id=54f473e7-f225-44b3-beb7-b5d50403fe02"
    )


def test_successfactors_talentcommunity_link_uses_job_page():
    job_url = (
        "https://jobs.scotiabank.com/job/"
        "Toronto-Junior-Software-Engineer-ON-M1L4S2/604648717/"
    )

    assert normalize_application_url(
        "https://jobs.scotiabank.com/talentcommunity/apply/604648717/"
        "?locale=en_US",
        job_url,
    ) == job_url


def test_normal_application_link_is_unchanged():
    application_url = "https://ats.example.com/apply/123"

    assert normalize_application_url(
        application_url,
        "https://careers.example.com/jobs/123",
    ) == application_url
