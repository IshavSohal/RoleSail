import threading
from pathlib import Path

from rolesail.database import init_db
from rolesail.scoring import scorer


def _job(description: str = "Requires 5+ years of professional experience.") -> dict:
    return {
        "title": "Software Engineer",
        "site": "Example Co",
        "location": "Toronto",
        "full_description": description,
    }


class _ScoringClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[dict] = []

    def chat(self, messages, max_tokens, temperature):
        self.messages = messages
        assert max_tokens == 1024
        assert temperature == 0.2
        return self.response


def _response(*, score: int = 8, years: str = "5", kind: str = "REQUIRED") -> str:
    return (
        f"SCORE: {score}\n"
        f"REQUIRED_YEARS: {years}\n"
        f"REQUIREMENT_TYPE: {kind}\n"
        "EXPERIENCE_EVIDENCE: 5+ years of professional experience\n"
        "KEYWORDS: Python, APIs\n"
        "REASONING: Strong technical alignment, with an experience gap."
    )


def test_parse_score_response_extracts_experience_requirement() -> None:
    result = scorer._parse_score_response(_response(years="5.5"))

    assert result["score"] == 8
    assert result["required_years"] == 5.5
    assert result["requirement_type"] == "REQUIRED"
    assert result["experience_evidence"] == "5+ years of professional experience"


def test_parse_score_response_allows_no_numeric_requirement() -> None:
    result = scorer._parse_score_response(
        _response(years="NONE", kind="NONE").replace(
            "EXPERIENCE_EVIDENCE: 5+ years of professional experience",
            "EXPERIENCE_EVIDENCE: NONE",
        )
    )

    assert result["required_years"] is None
    assert result["requirement_type"] == "NONE"
    assert result["experience_evidence"] == ""


def test_required_experience_caps_scale_with_shortfall() -> None:
    assert scorer._experience_score_cap(2, 2, "REQUIRED") is None
    assert scorer._experience_score_cap(2, 3, "REQUIRED") == 7
    assert scorer._experience_score_cap(2, 4, "REQUIRED") == 6
    assert scorer._experience_score_cap(2, 5, "REQUIRED") == 5
    assert scorer._experience_score_cap(2, 6, "REQUIRED") == 4


def test_preferred_experience_does_not_cap_score() -> None:
    assert scorer._experience_score_cap(2, 6, "PREFERRED") is None


def test_experience_evidence_must_appear_in_posting() -> None:
    assert scorer._evidence_is_grounded(
        "5+ years of professional experience",
        "Requirements:\n  5+ years of professional experience",
    )
    assert not scorer._evidence_is_grounded(
        "5+ years of professional experience",
        "Professional experience is preferred.",
    )


def test_posting_required_years_extracts_hard_minimum() -> None:
    description = """Minimum Qualifications
Bachelor's degree or equivalent experience.
5+ years of software development experience.
Preferred Qualifications
8+ years of software development experience.
"""

    assert scorer._posting_required_years(description) == 5


def test_posting_required_years_uses_least_demanding_alternative() -> None:
    description = (
        "Bachelor's degree and 1+ years of experience; OR 3+ years of "
        "professional experience in lieu of a degree."
    )

    assert scorer._posting_required_years(description) == 1


def test_posting_required_years_ignores_preferred_requirement() -> None:
    assert scorer._posting_required_years(
        "PREFERRED SKILLS AND EXPERIENCE:\n5+ years of web development experience"
    ) is None


def test_score_job_passes_candidate_years_and_applies_cap(monkeypatch) -> None:
    client = _ScoringClient(_response(score=8, years="5", kind="REQUIRED"))
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.score_job("Python backend developer", _job(), candidate_years=2)

    assert result["score"] == 5
    assert "raw score 8 was capped at 5" in result["reasoning"]
    assert "CANDIDATE PROFESSIONAL EXPERIENCE: 2 years" in client.messages[1]["content"]


def test_score_job_does_not_cap_preferred_experience(monkeypatch) -> None:
    client = _ScoringClient(_response(score=8, years="5", kind="PREFERRED"))
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.score_job(
        "Python backend developer",
        _job("Preferred Qualifications:\n5+ years of professional experience"),
        candidate_years=2,
    )

    assert result["score"] == 8
    assert "guardrail" not in result["reasoning"]


def test_score_job_does_not_cap_preferred_section_mislabeled_as_required(monkeypatch) -> None:
    client = _ScoringClient(_response(score=8, years="5", kind="REQUIRED"))
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.score_job(
        "Python backend developer",
        _job("Preferred Qualifications:\n5+ years of professional experience"),
        candidate_years=2,
    )

    assert result["score"] == 8
    assert "guardrail" not in result["reasoning"]


def test_score_job_ignores_ungrounded_required_experience(monkeypatch) -> None:
    client = _ScoringClient(_response(score=8, years="5", kind="REQUIRED"))
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.score_job(
        "Python backend developer",
        _job("Professional experience is preferred."),
        candidate_years=2,
    )

    assert result["score"] == 8
    assert "guardrail" not in result["reasoning"]


def test_score_job_caps_when_model_omits_required_experience(monkeypatch) -> None:
    response = _response(score=8, years="NONE", kind="NONE").replace(
        "EXPERIENCE_EVIDENCE: 5+ years of professional experience",
        "EXPERIENCE_EVIDENCE: NONE",
    )
    client = _ScoringClient(response)
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.score_job("Python backend developer", _job(), candidate_years=2)

    assert result["score"] == 5
    assert "raw score 8 was capped at 5" in result["reasoning"]


def test_invalid_candidate_years_disable_cap() -> None:
    assert scorer._as_years("") is None
    assert scorer._as_years("unknown") is None
    assert scorer._as_years(-1) is None
    assert scorer._as_years("2.5") == 2.5


def test_run_scoring_scores_in_parallel_but_writes_on_coordinator(monkeypatch, tmp_path) -> None:
    jobs = [
        {
            "url": f"https://example.com/{index}",
            "title": f"Engineer {index}",
            "site": "Example Co",
            "location": "Toronto",
            "full_description": "Python APIs",
        }
        for index in range(3)
    ]
    barrier = threading.Barrier(3)
    scoring_threads: set[int] = set()
    write_threads: list[int] = []

    class _Connection:
        def __init__(self):
            self.rows = []

        def execute(self, query, params=None):
            if query.lstrip().startswith("UPDATE"):
                write_threads.append(threading.get_ident())
                self.rows = []
            else:
                self.rows = [(7, len(jobs))]
            return self

        def fetchall(self):
            return self.rows

        def commit(self):
            return None

    resume = tmp_path / "resume.txt"
    resume.write_text("Python developer", encoding="utf-8")

    def fake_score(resume_text, job, candidate_years=None):
        scoring_threads.add(threading.get_ident())
        barrier.wait(timeout=2)
        return {"score": 7, "keywords": "Python", "reasoning": "Good fit"}

    monkeypatch.setattr(scorer, "RESUME_PATH", Path(resume))
    monkeypatch.setattr(scorer, "RESUME_TEX_PATH", tmp_path / "missing.tex")
    monkeypatch.setattr(scorer, "load_profile", lambda: {"experience": {"years_of_experience_total": 2}})
    monkeypatch.setattr(scorer, "get_connection", _Connection)
    monkeypatch.setattr(scorer, "get_jobs_by_stage", lambda **kwargs: jobs)
    monkeypatch.setattr(scorer, "get_client", lambda: object())
    monkeypatch.setattr(scorer, "score_job", fake_score)

    coordinator_thread = threading.get_ident()
    result = scorer.run_scoring(workers=3)

    assert result["scored"] == 3
    assert len(scoring_threads) == 3
    assert write_threads == [coordinator_thread] * 3


def test_run_scoring_rejects_invalid_worker_count(monkeypatch, tmp_path) -> None:
    resume = tmp_path / "resume.txt"
    resume.write_text("Python developer", encoding="utf-8")
    monkeypatch.setattr(scorer, "RESUME_PATH", resume)
    monkeypatch.setattr(scorer, "RESUME_TEX_PATH", tmp_path / "missing.tex")
    monkeypatch.setattr(scorer, "load_profile", dict)
    monkeypatch.setattr(scorer, "get_connection", lambda: object())
    monkeypatch.setattr(scorer, "get_jobs_by_stage", lambda **kwargs: [_job() | {"url": "https://example.com"}])

    try:
        scorer.run_scoring(workers=0)
    except ValueError as exc:
        assert str(exc) == "workers must be at least 1"
    else:
        raise AssertionError("expected run_scoring to reject workers=0")


def test_run_scoring_removes_valid_low_scores_but_preserves_errors(monkeypatch, tmp_path) -> None:
    conn = init_db(tmp_path / "scoring.db")
    for title, score in (("Low", None), ("High", None), ("Error", None), ("Stored low", 4)):
        conn.execute(
            "INSERT INTO jobs (url, title, site, full_description, fit_score) VALUES (?, ?, ?, ?, ?)",
            (f"https://example.com/{title}", title, "Example Co", "Python APIs", score),
        )
    conn.commit()

    resume = tmp_path / "resume.txt"
    resume.write_text("Python developer", encoding="utf-8")
    scores = {"Low": 5, "High": 7, "Error": 0}

    def fake_score(resume_text, job, candidate_years=None):
        return {"score": scores[job["title"]], "keywords": "Python", "reasoning": "Result"}

    monkeypatch.setattr(scorer, "RESUME_PATH", resume)
    monkeypatch.setattr(scorer, "RESUME_TEX_PATH", tmp_path / "missing.tex")
    monkeypatch.setattr(scorer, "load_profile", lambda: {"experience": {"years_of_experience_total": 2}})
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "get_client", lambda: object())
    monkeypatch.setattr(scorer, "score_job", fake_score)

    result = scorer.run_scoring(workers=1)

    remaining = {
        row[0]: row[1]
        for row in conn.execute("SELECT title, fit_score FROM jobs ORDER BY title").fetchall()
    }
    assert result["scored"] == 3
    assert result["removed"] == 2
    assert result["errors"] == 1
    assert remaining == {"Error": 0, "High": 7}


def test_run_scoring_target_url_only_scores_requested_job(monkeypatch, tmp_path) -> None:
    conn = init_db(tmp_path / "targeted-scoring.db")
    urls = ["https://example.com/imported", "https://example.com/unrelated"]
    for url in urls:
        conn.execute(
            "INSERT INTO jobs (url, title, site, full_description) VALUES (?, ?, ?, ?)",
            (url, url.rsplit("/", 1)[-1], "Example Co", "Python APIs"),
        )
    conn.commit()

    resume = tmp_path / "resume.txt"
    resume.write_text("Python developer", encoding="utf-8")
    monkeypatch.setattr(scorer, "RESUME_PATH", resume)
    monkeypatch.setattr(scorer, "RESUME_TEX_PATH", tmp_path / "missing.tex")
    monkeypatch.setattr(scorer, "load_profile", lambda: {"experience": {}})
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "get_client", lambda: object())
    monkeypatch.setattr(
        scorer,
        "score_job",
        lambda *_args, **_kwargs: {
            "score": 8,
            "keywords": "Python",
            "reasoning": "Strong fit",
        },
    )

    result = scorer.run_scoring(target_url=urls[0], workers=1)

    rows = conn.execute("SELECT url, fit_score FROM jobs ORDER BY url").fetchall()
    assert result["scored"] == 1
    assert [(row["url"], row["fit_score"]) for row in rows] == [
        (urls[0], 8),
        (urls[1], None),
    ]


def test_run_scoring_preserves_low_score_for_requested_job(monkeypatch, tmp_path) -> None:
    conn = init_db(tmp_path / "targeted-low-score.db")
    url = "https://example.com/imported"
    conn.execute(
        "INSERT INTO jobs (url, title, site, strategy, full_description) "
        "VALUES (?, 'Imported', 'Example Co', 'external_upload', 'Python APIs')",
        (url,),
    )
    conn.commit()

    resume = tmp_path / "resume.txt"
    resume.write_text("Python developer", encoding="utf-8")
    monkeypatch.setattr(scorer, "RESUME_PATH", resume)
    monkeypatch.setattr(scorer, "RESUME_TEX_PATH", tmp_path / "missing.tex")
    monkeypatch.setattr(scorer, "load_profile", lambda: {"experience": {}})
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "get_client", lambda: object())
    monkeypatch.setattr(
        scorer,
        "score_job",
        lambda *_args, **_kwargs: {
            "score": 4,
            "keywords": "Python",
            "reasoning": "Limited fit",
        },
    )

    result = scorer.run_scoring(target_url=url, workers=1)

    row = conn.execute("SELECT fit_score FROM jobs WHERE url = ?", (url,)).fetchone()
    assert result["scored"] == 1
    assert result["removed"] == 0
    assert row["fit_score"] == 4
