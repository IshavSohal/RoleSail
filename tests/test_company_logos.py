"""Tests for the persistent company-logo cache."""

from __future__ import annotations

import httpx

from rolesail import company_logos


def test_company_logo_candidates_backfill_known_company() -> None:
    assert company_logos.company_logo_candidates("OpenAI") == [
        "https://openai.com/favicon.ico",
        "https://www.google.com/s2/favicons?domain=openai.com&sz=128",
    ]


def test_amazon_logo_uses_customer_site_domain() -> None:
    assert company_logos.company_logo_candidates("Amazon") == [
        "https://www.amazon.com/favicon.ico",
        "https://www.google.com/s2/favicons?domain=www.amazon.com&sz=128",
    ]


def test_veeva_logo_uses_company_domain() -> None:
    assert company_logos.company_logo_candidates("Veeva Systems") == [
        "https://veeva.com/favicon.ico",
        "https://www.google.com/s2/favicons?domain=veeva.com&sz=128",
    ]


def test_konrad_logo_uses_company_domain() -> None:
    assert company_logos.company_logo_candidates("Konrad") == [
        "https://konrad.com/favicon.ico",
        "https://www.google.com/s2/favicons?domain=konrad.com&sz=128",
    ]


def test_disney_logo_uses_company_domain() -> None:
    assert company_logos.company_logo_candidates("The Walt Disney Company") == [
        "https://disney.com/favicon.ico",
        "https://www.google.com/s2/favicons?domain=disney.com&sz=128",
    ]


def test_company_logo_candidates_prefer_stored_url() -> None:
    candidates = company_logos.company_logo_candidates(
        "OpenAI", "https://cdn.example.com/openai.png"
    )

    assert candidates[0] == "https://cdn.example.com/openai.png"


def test_company_logo_candidates_prefer_normalized_png_for_unreliable_favicons() -> None:
    for company, domain in (
        ("Stripe", "stripe.com"),
        ("Pinterest", "pinterest.com"),
        ("Discord", "discord.com"),
    ):
        candidates = company_logos.company_logo_candidates(
            company, f"https://{domain}/favicon.ico"
        )

        assert candidates[0] == (
            f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
        )
        assert candidates.count(candidates[0]) == 1


def test_company_logo_is_downloaded_once_and_cached(tmp_path, monkeypatch) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"\x89PNG\r\nlogo",
        )

    monkeypatch.setattr(company_logos, "COMPANY_LOGO_DIR", tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    first = company_logos.load_company_logo(
        "Example Corp", "https://cdn.example.com/logo.png", client=client
    )
    second = company_logos.load_company_logo(
        "Example Corp", "https://cdn.example.com/logo.png", client=client
    )
    client.close()

    assert first == (b"\x89PNG\r\nlogo", "image/png")
    assert second == first
    assert len(requests) == 1
    assert len(list(tmp_path.glob("example-corp-*.png"))) == 1


def test_company_logo_rejects_non_image_response(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(company_logos, "COMPANY_LOGO_DIR", tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"not an image"
            )
        )
    )
    result = company_logos.load_company_logo(
        "Example Corp", "https://example.com/logo", client=client
    )
    client.close()

    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_company_logo_rejects_local_source(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(company_logos, "COMPANY_LOGO_DIR", tmp_path)

    assert company_logos.load_company_logo("Internal", "http://127.0.0.1/logo.png") is None
