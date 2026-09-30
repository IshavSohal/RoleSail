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
