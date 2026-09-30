"""Tests for pipeline stage resolution."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from rolesail import pipeline
from rolesail.database import delete_jobs_older_than, get_jobs_by_stage, get_stats, init_db
from rolesail.pipeline import _PENDING_SQL, _resolve_stages


def test_discover_includes_enrich_and_score_when_llm_available(monkeypatch) -> None:
    monkeypatch.setattr("rolesail.config.get_tier", lambda: 2)
    assert _resolve_stages(["discover"]) == ["discover", "enrich", "score"]


def test_discover_stays_alone_without_llm(monkeypatch) -> None:
    monkeypatch.setattr("rolesail.config.get_tier", lambda: 1)
    assert _resolve_stages(["discover"]) == ["discover"]


def test_discover_with_later_stages_keeps_order(monkeypatch) -> None:
    monkeypatch.setattr("rolesail.config.get_tier", lambda: 2)
    assert _resolve_stages(["discover", "tailor"]) == [
        "discover",
        "enrich",
        "score",
        "tailor",
    ]


def test_explicit_score_only_unchanged(monkeypatch) -> None:
    monkeypatch.setattr("rolesail.config.get_tier", lambda: 2)
    assert _resolve_stages(["score"]) == ["score"]


def test_sequential_pipeline_forwards_score_workers(monkeypatch) -> None:
    received: list[int] = []
    monkeypatch.setitem(
        pipeline._STAGE_RUNNERS,
        "score",
        lambda workers: received.append(workers) or {"status": "ok"},
    )

    result = pipeline._run_sequential(["score"], min_score=7, score_workers=4)

    assert received == [4]
    assert result["errors"] == {}


def test_pipeline_defaults_to_streaming() -> None:
    assert inspect.signature(pipeline.run_pipeline).parameters["stream"].default is True


def test_pipeline_defaults_to_three_discovery_enrichment_workers() -> None:
    assert inspect.signature(pipeline.run_pipeline).parameters["workers"].default == 3


def test_delete_jobs_older_than_uses_posted_date_then_discovered_date(tmp_path) -> None:
    connection = init_db(tmp_path / "jobs.db")
    connection.executemany(
        "INSERT INTO jobs (url, posted_at, discovered_at) VALUES (?, ?, ?)",
        [
            ("old-iso", "2026-07-19T11:59:59+00:00", "2026-08-19T12:00:00+00:00"),
            ("old-display", "July 18, 2026", "2026-08-19T12:00:00+00:00"),
            ("old-fallback", None, "2026-07-01T12:00:00+00:00"),
            ("old-applied", "2026-06-01", "2026-06-01T12:00:00+00:00"),
            ("cutoff", "2026-07-20T12:00:00+00:00", "2026-07-20T12:00:00+00:00"),
            ("recent", "2026-08-18", "2026-06-01T12:00:00+00:00"),
        ],
    )
    connection.execute(
        "UPDATE jobs SET applied_at = '2026-08-01T12:00:00+00:00' WHERE url = 'old-applied'"
    )
    connection.commit()

    deleted = delete_jobs_older_than(
        connection,
        days=30,
        now=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
    )

    assert deleted == 3
    remaining = connection.execute("SELECT url FROM jobs ORDER BY url").fetchall()
    assert [row[0] for row in remaining] == ["cutoff", "old-applied", "recent"]


def test_pipeline_cleans_old_jobs_before_running(monkeypatch) -> None:
    calls: list[tuple[str, int | None]] = []
    monkeypatch.setattr(pipeline, "load_env", lambda: None)
    monkeypatch.setattr(pipeline, "ensure_dirs", lambda: None)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(
        pipeline,
        "delete_jobs_older_than",
        lambda *, days: calls.append(("cleanup", days)) or 2,
    )
    monkeypatch.setattr(pipeline, "get_stats", lambda: {
        "total": 0,
        "pending_detail": 0,
        "discovery_rejected": 0,
        "with_description": 0,
        "scored": 0,
        "tailored": 0,
        "with_cover_letter": 0,
        "ready_to_apply": 0,
        "applied": 0,
    })
    monkeypatch.setattr(
        pipeline,
        "_run_sequential",
        lambda *args, **kwargs: {
            "stages": [{"stage": "score", "status": "ok", "elapsed": 0.0}],
            "errors": {},
            "elapsed": 0.0,
        },
    )

    pipeline.run_pipeline(["score"], stream=False)

    assert calls == [("cleanup", 7)]


def test_pending_tailoring_excludes_jobs_already_applied_to(tmp_path) -> None:
    connection = init_db(tmp_path / "jobs.db")
    for url, applied_at in (
        ("https://example.com/not-applied", None),
        ("https://example.com/already-applied", "2026-08-02T12:00:00+00:00"),
    ):
        connection.execute(
            "INSERT INTO jobs (url, title, full_description, fit_score, applied_at) "
            "VALUES (?, 'Engineer', 'Complete job description', 8, ?)",
            (url, applied_at),
        )
    connection.commit()

    pending = get_jobs_by_stage(
        connection,
        stage="pending_tailor",
        min_score=7,
    )
    assert [job["url"] for job in pending] == ["https://example.com/not-applied"]
    assert get_stats(connection)["untailored_eligible"] == 1

    pipeline_count = connection.execute(_PENDING_SQL["tailor"], (7,)).fetchone()[0]
    assert pipeline_count == 1
