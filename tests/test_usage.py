import json

from rolesail import dashboard_server, usage
from rolesail.database import get_connection, init_db


def _isolated_usage(tmp_path, monkeypatch):
    db_path = tmp_path / "usage.db"
    conn = init_db(db_path)
    monkeypatch.setattr(usage, "init_db", lambda: conn)
    monkeypatch.setattr(usage, "get_connection", lambda: get_connection(db_path))
    pricing_path = tmp_path / "pricing.json"
    monkeypatch.setattr(usage, "PRICING_PATH", pricing_path)
    return conn


def test_usage_estimates_persists_context_and_rate_snapshot(tmp_path, monkeypatch):
    conn = _isolated_usage(tmp_path, monkeypatch)
    monkeypatch.setattr(
        usage,
        "DEFAULT_RATES",
        {"test-model": {"input": 1.0, "output": 2.0, "cache_read": 0.5, "cache_write": 0.0}},
    )
    run = usage.create_run(["score"])
    with usage.usage_context(run["id"], "score"):
        usage.record_usage(
            provider="openai",
            model="test-model",
            tokens={"input": 1_000_000, "output": 500_000, "cache_read": 100_000, "cache_write": 0},
        )

    row = conn.execute("SELECT * FROM llm_usage").fetchone()
    assert row["run_id"] == run["id"]
    assert row["stage"] == "score"
    assert row["estimated_cost_microusd"] == 2_050_000
    assert row["cost_kind"] == "estimated"
    assert json.loads(row["pricing_json"])["rates_per_million_usd"]["input"] == 1.0


def test_reported_cost_precedes_estimate_and_missing_usage_is_unavailable(tmp_path, monkeypatch):
    conn = _isolated_usage(tmp_path, monkeypatch)
    monkeypatch.setattr(
        usage,
        "DEFAULT_RATES",
        {"test-model": {"input": 100.0, "output": 100.0, "cache_read": 0.0, "cache_write": 0.0}},
    )
    usage.record_usage(
        provider="anthropic",
        model="test-model",
        tokens={"input": 1000, "output": 1000},
        reported_cost_usd=0.123456,
        stage="auto_apply",
    )
    usage.record_usage(provider="openai", model="unknown-model")

    rows = conn.execute("SELECT * FROM llm_usage ORDER BY id").fetchall()
    assert rows[0]["reported_cost_microusd"] == 123456
    assert rows[0]["cost_kind"] == "reported"
    assert rows[1]["cost_kind"] == "unavailable"
    summary = usage.usage_summary()
    assert summary["all_time"]["cost_microusd"] == 123456
    assert summary["all_time"]["reported_requests"] == 1
    assert summary["all_time"]["unavailable_requests"] == 1


def test_pricing_override_validation_and_future_only_costs(tmp_path, monkeypatch):
    conn = _isolated_usage(tmp_path, monkeypatch)
    monkeypatch.setattr(usage, "DEFAULT_RATES", {})
    usage.save_pricing({"custom": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0}})
    usage.record_usage(provider="openai", model="custom", tokens={"input": 1_000_000, "output": 0})
    usage.save_pricing({"custom": {"input": 3, "output": 1, "cache_read": 0, "cache_write": 0}})
    usage.record_usage(provider="openai", model="custom", tokens={"input": 1_000_000, "output": 0})

    costs = [row[0] for row in conn.execute("SELECT estimated_cost_microusd FROM llm_usage ORDER BY id")]
    assert costs == [1_000_000, 3_000_000]


def test_new_default_rate_backfills_unavailable_openai_usage(tmp_path, monkeypatch):
    conn = _isolated_usage(tmp_path, monkeypatch)
    monkeypatch.setattr(usage, "DEFAULT_RATES", {})
    usage.record_usage(
        provider="openai",
        model="new-model",
        tokens={
            "input": 1_000_000,
            "output": 100_000,
            "cache_read": 200_000,
            "cache_write": 0,
        },
    )

    monkeypatch.setattr(
        usage,
        "DEFAULT_RATES",
        {"new-model": {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.25}},
    )
    summary = usage.usage_summary()

    row = conn.execute("SELECT * FROM llm_usage").fetchone()
    assert row["input_tokens"] == 800_000
    assert row["estimated_cost_microusd"] == 1_020_000
    assert row["cost_kind"] == "estimated"
    assert summary["all_time"]["cost_usd"] == 1.02


def test_recover_interrupted_runs(tmp_path, monkeypatch):
    _isolated_usage(tmp_path, monkeypatch)
    run = usage.create_run(["discover", "enrich", "score"])
    assert usage.recover_interrupted_runs() == 1
    recovered = usage.get_run(run["id"])
    assert recovered["status"] == "interrupted"
    assert recovered["finished_at"]


def test_dashboard_pipeline_stops_after_scoring(monkeypatch):
    captured = {}

    def run_pipeline(**kwargs):
        captured.update(kwargs)
        return {"stages": [], "errors": {}, "elapsed": 0}

    updates = []
    monkeypatch.setattr("rolesail.pipeline.run_pipeline", run_pipeline)
    monkeypatch.setattr("rolesail.usage.update_run", lambda run_id, **kwargs: updates.append((run_id, kwargs)))
    dashboard_server._execute_dashboard_pipeline(object(), "run-1", 3)

    assert captured["stages"] == ["discover", "enrich", "score"]
    assert "tailor" not in captured["stages"]
    assert "cover" not in captured["stages"]
    assert captured["run_id"] == "run-1"
    assert updates[-1][1]["status"] == "complete"
