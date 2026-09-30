import httpx

from rolesail import llm


class _FakeCondition:
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.waits: list[float] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def wait(self, timeout: float) -> None:
        self.waits.append(timeout)
        self.clock[0] += timeout

    def notify_all(self) -> None:
        return None


def test_rate_limiter_evenly_paces_requests(monkeypatch) -> None:
    clock = [100.0]
    limiter = llm.RateLimiter(rpm=60)
    condition = _FakeCondition(clock)
    limiter._condition = condition
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])

    limiter.acquire()
    limiter.acquire()

    assert condition.waits == [1.0]
    assert clock[0] == 101.0


def test_rate_limiter_applies_shared_cooldown(monkeypatch) -> None:
    clock = [100.0]
    limiter = llm.RateLimiter()
    condition = _FakeCondition(clock)
    limiter._condition = condition
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])

    limiter.defer(7.5)
    limiter.acquire()

    assert condition.waits == [7.5]
    assert clock[0] == 107.5


def test_rate_limiter_honors_rolling_token_budget(monkeypatch) -> None:
    clock = [100.0]
    limiter = llm.RateLimiter(tpm=100)
    condition = _FakeCondition(clock)
    limiter._condition = condition
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])

    limiter.acquire(60)
    limiter.acquire(60)

    assert condition.waits == [60.0]
    assert clock[0] == 160.0


def test_hosted_client_defaults_to_fifteen_rpm(monkeypatch) -> None:
    monkeypatch.delenv("LLM_RPM", raising=False)
    client = llm.LLMClient("https://api.openai.com/v1", "test-model", "test-key")

    try:
        assert client._rate_limiter.rpm == 15
    finally:
        client.close()


def test_client_applies_429_cooldown_to_shared_limiter(monkeypatch) -> None:
    request = httpx.Request("POST", "https://example.com/chat/completions")
    response = httpx.Response(429, headers={"Retry-After": "2"}, request=request)
    error = httpx.HTTPStatusError("rate limited", request=request, response=response)
    calls = 0

    def chat_compat(messages, temperature, max_tokens):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return "ok"

    class _RecordingLimiter:
        def __init__(self) -> None:
            self.acquires = 0
            self.cooldowns: list[float] = []

        def acquire(self, estimated_tokens=0) -> None:
            self.acquires += 1

        def defer(self, seconds: float) -> None:
            self.cooldowns.append(seconds)

    client = llm.LLMClient("http://local.test/v1", "test-model", "")
    limiter = _RecordingLimiter()
    client._rate_limiter = limiter
    monkeypatch.setattr(client, "_chat_compat", chat_compat)
    monkeypatch.setattr(llm.random, "uniform", lambda start, end: 0.0)

    try:
        result = client.chat([{"role": "user", "content": "hello"}], max_tokens=10)
    finally:
        client.close()

    assert result == "ok"
    assert limiter.acquires == 2
    assert limiter.cooldowns == [2.0]


def test_successful_completion_survives_usage_database_failure(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "SCORE: 8"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    client = llm.LLMClient("http://local.test/v1", "test-model", "")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "rolesail.usage.record_usage",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("unable to open database file")),
    )

    try:
        result = client.chat([{"role": "user", "content": "score this"}])
    finally:
        client.close()

    assert result == "SCORE: 8"
