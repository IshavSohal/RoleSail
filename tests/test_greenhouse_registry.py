from rolesail.discovery import ats, greenhouse


def test_requested_companies_are_in_default_greenhouse_registry() -> None:
    companies = greenhouse.load_companies()

    assert companies["konrad"] == {
        "name": "Konrad",
        "board_token": "konradgroup",
    }
    assert companies["robinhood"] == {
        "name": "Robinhood",
        "board_token": "robinhood",
    }
    assert companies["squarepoint"] == {
        "name": "Squarepoint Capital",
        "board_token": "squarepointcapital",
    }


def test_ramp_uses_ashby_discovery() -> None:
    assert ats.load_ashby_companies()["ramp"] == {
        "name": "Ramp",
        "board": "ramp",
    }
    assert "ramp" not in greenhouse.load_companies()


def test_handshake_uses_ashby_discovery() -> None:
    assert ats.load_ashby_companies()["handshake"] == {
        "name": "Handshake",
        "board": "handshake",
    }


def test_charta_uses_ashby_discovery() -> None:
    assert ats.load_ashby_companies()["charta"] == {
        "name": "Charta Health",
        "board": "chartahealth",
    }


def test_greenhouse_multi_location_job_uses_eligible_office() -> None:
    accepted, location = greenhouse._greenhouse_location(
        {
            "location": {"name": "London, Montreal, Singapore"},
            "offices": [
                {"location": "London, United Kingdom"},
                {"location": "Montreal, QC, Canada"},
                {"location": "Singapore"},
            ],
        },
        accept=[],
        reject=[],
        search_cfg={
            "allowed_countries": ["Canada", "United States"],
            "accept_unknown_locations": False,
        },
        enforce_filter=True,
    )

    assert accepted is True
    assert location == "Montreal, QC, Canada"


def test_greenhouse_does_not_use_an_office_absent_from_display_location() -> None:
    accepted, location = greenhouse._greenhouse_location(
        {
            "location": {"name": "London"},
            "offices": [
                {"location": "London, United Kingdom"},
                {"location": "Montreal, QC, Canada"},
            ],
        },
        accept=[],
        reject=[],
        search_cfg={
            "allowed_countries": ["Canada", "United States"],
            "accept_unknown_locations": False,
        },
        enforce_filter=True,
    )

    assert accepted is False
    assert location == "London"
