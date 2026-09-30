"""Persistent LLM usage and pipeline-run accounting."""

from __future__ import annotations

import contextlib
import contextvars
import json
import math
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

from rolesail import config
from rolesail.database import get_connection, init_db

PRICING_VERSION = "2026-08-15"
PRICING_PATH = config.APP_DIR / "llm_pricing.json"
DEFAULT_RATES: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075, "cache_write": 0.0},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20, "cache_read": 0.02, "cache_write": 0.25},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40, "cache_read": 0.025, "cache_write": 0.0},
}

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("rolesail_run_id", default=None)
_stage: contextvars.ContextVar[str] = contextvars.ContextVar("rolesail_stage", default="other")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextlib.contextmanager
def usage_context(run_id: str | None = None, stage: str | None = None) -> Iterator[None]:
    """Attribute LLM calls in this context to a pipeline run and stage."""
    run_token = _run_id.set(run_id if run_id is not None else _run_id.get())
    stage_token = _stage.set(stage if stage is not None else _stage.get())
    try:
        yield
    finally:
        _stage.reset(stage_token)
        _run_id.reset(run_token)


def current_context() -> tuple[str | None, str]:
    return _run_id.get(), _stage.get()


def _validate_rates(value: object) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict):
        raise ValueError("Pricing overrides must be an object keyed by model")  # noqa: TRY004
    validated: dict[str, dict[str, float]] = {}
    for model, rates in value.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(rates, dict):
            raise ValueError("Each pricing override needs a model name and rate object")
        row: dict[str, float] = {}
        for key in ("input", "output", "cache_read", "cache_write"):
            raw = rates.get(key, 0)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"{model}.{key} must be a non-negative number")  # noqa: TRY004
            number = float(raw)
            if number < 0 or not math.isfinite(number):
                raise ValueError(f"{model}.{key} must be a non-negative finite number")
            row[key] = number
        validated[model.strip()] = row
    return validated


def load_pricing() -> dict:
    overrides: dict[str, dict[str, float]] = {}
    if PRICING_PATH.exists():
        overrides = _validate_rates(json.loads(PRICING_PATH.read_text(encoding="utf-8")))
    return {"version": PRICING_VERSION, "defaults": DEFAULT_RATES, "overrides": overrides}


def save_pricing(overrides: object) -> dict:
    validated = _validate_rates(overrides)
    PRICING_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PRICING_PATH.with_name(f".{PRICING_PATH.name}.tmp")
    try:
        temporary.write_text(json.dumps(validated, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(PRICING_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    return load_pricing()


def rates_for(provider: str, model: str) -> dict[str, float] | None:
    pricing = load_pricing()
    if model in pricing["overrides"]:
        return pricing["overrides"][model]
    if model in pricing["defaults"]:
        return pricing["defaults"][model]
    if provider == "local":
        return {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    return None


def _estimate(tokens: dict[str, int | None], rates: dict[str, float] | None) -> int | None:
    """Estimate cost from mutually exclusive token categories."""
    if rates is None or tokens.get("input") is None or tokens.get("output") is None:
        return None
    dollars = sum(
        int(tokens.get(kind) or 0) * float(rates.get(kind, 0)) / 1_000_000
        for kind in ("input", "output", "cache_read", "cache_write")
    )
    return round(dollars * 1_000_000)


def _backfill_new_default_rates() -> int:
    """Price old unavailable rows when support for their model is added.

    User overrides intentionally remain future-only. Older OpenAI rows stored
    total prompt tokens in ``input_tokens``, so cached tokens must be removed
    before applying the mutually exclusive rate categories.
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, provider, model, input_tokens, output_tokens,
                  cache_read_tokens, cache_write_tokens
           FROM llm_usage WHERE cost_kind = 'unavailable'"""
    ).fetchall()
    updated = 0
    for row in rows:
        rates = DEFAULT_RATES.get(row["model"])
        if rates is None:
            continue
        tokens = {
            "input": row["input_tokens"],
            "output": row["output_tokens"],
            "cache_read": row["cache_read_tokens"],
            "cache_write": row["cache_write_tokens"],
        }
        if row["provider"] == "openai" and tokens["input"] is not None:
            tokens["input"] = max(
                0,
                tokens["input"]
                - int(tokens["cache_read"] or 0)
                - int(tokens["cache_write"] or 0),
            )
        estimated = _estimate(tokens, rates)
        if estimated is None:
            continue
        snapshot = {"version": PRICING_VERSION, "rates_per_million_usd": rates}
        cursor = conn.execute(
            """UPDATE llm_usage
               SET input_tokens = ?, estimated_cost_microusd = ?,
                   cost_kind = 'estimated', pricing_json = ?
               WHERE id = ? AND cost_kind = 'unavailable'""",
            (tokens["input"], estimated, json.dumps(snapshot, separators=(",", ":")), row["id"]),
        )
        updated += cursor.rowcount
    if updated:
        conn.commit()
    return updated


def record_usage(
    *,
    provider: str,
    model: str,
    tokens: dict[str, int | None] | None = None,
    reported_cost_usd: float | None = None,
    status: str = "success",
    error: str | None = None,
    run_id: str | None = None,
    stage: str | None = None,
) -> int:
    """Append one normalized provider request to the local ledger."""
    init_db()
    context_run, context_stage = current_context()
    normalized = {key: None for key in ("input", "output", "cache_read", "cache_write")}
    if tokens:
        for key in normalized:
            value = tokens.get(key)
            normalized[key] = max(0, int(value)) if value is not None else None
    rates = rates_for(provider, model)
    reported = None if reported_cost_usd is None else round(max(0.0, reported_cost_usd) * 1_000_000)
    estimated = _estimate(normalized, rates)
    kind = "reported" if reported is not None else ("estimated" if estimated is not None else "unavailable")
    snapshot = {"version": PRICING_VERSION, "rates_per_million_usd": rates}
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO llm_usage (
            run_id, created_at, stage, provider, model, status,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            reported_cost_microusd, estimated_cost_microusd, cost_kind, pricing_json, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id if run_id is not None else context_run,
            _now(), stage or context_stage, provider, model, status,
            normalized["input"], normalized["output"], normalized["cache_read"], normalized["cache_write"],
            reported, estimated, kind, json.dumps(snapshot, separators=(",", ":")), error,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def create_run(stages: list[str]) -> dict:
    init_db()
    run_id = str(uuid.uuid4())
    now = _now()
    conn = get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (id, status, stages_json, started_at) VALUES (?, 'running', ?, ?)",
        (run_id, json.dumps(stages), now),
    )
    conn.commit()
    return {"id": run_id, "status": "running", "stages": stages, "started_at": now}


def recover_interrupted_runs() -> int:
    """Close runs left active by a previous dashboard process."""
    init_db()
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE pipeline_runs SET status = 'interrupted', finished_at = ?, "
        "error_summary = COALESCE(error_summary, 'Dashboard process stopped before completion') "
        "WHERE status = 'running'",
        (_now(),),
    )
    conn.commit()
    return cursor.rowcount


def update_run(run_id: str, *, status: str | None = None, current_stage: str | None = None,
               error: str | None = None, result: dict | None = None) -> None:
    fields: list[str] = []
    values: list[object] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
        if status in {"complete", "partial", "error", "interrupted"}:
            fields.append("finished_at = ?")
            values.append(_now())
    if current_stage is not None:
        fields.append("current_stage = ?")
        values.append(current_stage)
    if error is not None:
        fields.append("error_summary = ?")
        values.append(error[:2000])
    if result is not None:
        fields.append("result_json = ?")
        values.append(json.dumps(result))
    if not fields:
        return
    values.append(run_id)
    conn = get_connection()
    conn.execute(f"UPDATE pipeline_runs SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()


def get_run(run_id: str | None = None) -> dict | None:
    init_db()
    conn = get_connection()
    if run_id:
        row = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    result = dict(row)
    result["stages"] = json.loads(result.pop("stages_json"))
    result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
    result.pop("result_json", None)
    result["usage"] = usage_summary(run_id=result["id"])["all_time"]
    return result


def _summary_where(
    run_id: str | None,
    since: str | None,
    stage: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    values: list[str] = []
    if run_id:
        clauses.append("run_id = ?")
        values.append(run_id)
    if since:
        clauses.append("created_at >= ?")
        values.append(since)
    for column, value in (("stage", stage), ("provider", provider), ("model", model)):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    return (" WHERE " + " AND ".join(clauses) if clauses else "", values)


def _aggregate(run_id: str | None = None, since: str | None = None, *,
               stage: str | None = None, provider: str | None = None,
               model: str | None = None) -> dict:
    where, values = _summary_where(run_id, since, stage, provider, model)
    row = get_connection().execute(
        f"""SELECT COUNT(*) requests,
            COALESCE(SUM(input_tokens), 0) input_tokens,
            COALESCE(SUM(output_tokens), 0) output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) cache_read_tokens,
            COALESCE(SUM(cache_write_tokens), 0) cache_write_tokens,
            COALESCE(SUM(COALESCE(reported_cost_microusd, estimated_cost_microusd)), 0) cost_microusd,
            SUM(CASE WHEN cost_kind = 'reported' THEN 1 ELSE 0 END) reported_requests,
            SUM(CASE WHEN cost_kind = 'estimated' THEN 1 ELSE 0 END) estimated_requests,
            SUM(CASE WHEN cost_kind = 'unavailable' THEN 1 ELSE 0 END) unavailable_requests
        FROM llm_usage{where}""", values,
    ).fetchone()
    result = dict(row)
    result["cost_usd"] = result["cost_microusd"] / 1_000_000
    return result


def usage_summary(run_id: str | None = None, *, stage: str | None = None,
                  provider: str | None = None, model: str | None = None) -> dict:
    init_db()
    _backfill_new_default_rates()
    now = datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    conn = get_connection()
    groups = {}
    for column in ("stage", "provider", "model"):
        where, values = _summary_where(run_id, None, stage, provider, model)
        rows = conn.execute(
            f"""SELECT {column} name, COUNT(*) requests,
                COALESCE(SUM(COALESCE(reported_cost_microusd, estimated_cost_microusd)), 0) cost_microusd
            FROM llm_usage{where} GROUP BY {column} ORDER BY cost_microusd DESC""", values,
        ).fetchall()
        groups[column] = [dict(row) | {"cost_usd": row["cost_microusd"] / 1_000_000} for row in rows]
    return {
        "today": _aggregate(run_id, day, stage=stage, provider=provider, model=model),
        "month": _aggregate(run_id, month, stage=stage, provider=provider, model=model),
        "all_time": _aggregate(run_id, None, stage=stage, provider=provider, model=model),
        "by_stage": groups["stage"],
        "by_provider": groups["provider"],
        "by_model": groups["model"],
        "pricing_version": PRICING_VERSION,
    }


def usage_history(*, run_id: str | None = None, stage: str | None = None,
                  provider: str | None = None, model: str | None = None,
                  limit: int = 100) -> list[dict]:
    """Return recent normalized usage entries without exposing prompts or keys."""
    init_db()
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("Limit must be an integer between 1 and 500")
    where, values = _summary_where(run_id, None, stage, provider, model)
    rows = get_connection().execute(
        f"""SELECT id, run_id, created_at, stage, provider, model, status,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            reported_cost_microusd, estimated_cost_microusd, cost_kind, error
        FROM llm_usage{where} ORDER BY created_at DESC, id DESC LIMIT ?""",
        [*values, limit],
    ).fetchall()
    return [dict(row) for row in rows]
