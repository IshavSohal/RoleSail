"""
Unified LLM client for RoleSail.

Auto-detects provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-2.0-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import random
import threading
import time
from collections import deque

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.

    Priority:
      1. LLM_URL (local OpenAI-compatible server)
      2. Model-hinted provider when LLM_MODEL looks like gpt-*/o* and
         OPENAI_API_KEY is set (avoids sending OpenAI model IDs to Gemini)
      3. GEMINI_API_KEY
      4. OPENAI_API_KEY
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    local_url = os.environ.get("LLM_URL", "").strip()
    model_override = os.environ.get("LLM_MODEL", "").strip()
    model_l = model_override.lower()

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", "").strip(),
        )

    # If the user explicitly set an OpenAI-looking model, prefer OpenAI.
    openai_model_hint = model_l.startswith(("gpt-", "o1", "o3", "o4"))
    if openai_key and openai_model_hint:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if gemini_key:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-2.0-flash",
            gemini_key,
        )

    if openai_key:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


def _openai_allows_custom_temperature(model: str) -> bool:
    """Return False for OpenAI models that only accept the default temperature."""
    m = model.lower()
    return not m.startswith(("gpt-5", "o1", "o3", "o4"))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10
_DEFAULT_HOSTED_RPM = 15


def _env_rate_limit(name: str, default: int) -> int:
    """Read a non-negative integer rate limit from the environment."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Ignoring invalid %s=%r; using %d", name, raw, default)
        return default
    if value < 0:
        log.warning("Ignoring negative %s=%r; using %d", name, raw, default)
        return default
    return value


class RateLimiter:
    """Thread-safe request pacer with an optional token-per-minute budget.

    RPM requests are evenly spaced instead of released in bursts. Token usage
    is conservatively reserved for a rolling 60-second window before a request
    starts. A zero limit disables that dimension.
    """

    def __init__(self, rpm: int = 0, tpm: int = 0) -> None:
        self.rpm = max(0, rpm)
        self.tpm = max(0, tpm)
        self._request_interval = 60.0 / self.rpm if self.rpm else 0.0
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._token_reservations: deque[tuple[float, int]] = deque()
        self._condition = threading.Condition()

    def acquire(self, estimated_tokens: int = 0) -> None:
        """Wait until both request and token budgets permit a request."""
        tokens = max(0, estimated_tokens)
        if self.tpm and tokens > self.tpm:
            # A single oversized prompt can never fit the rolling budget. Let
            # it through alone rather than deadlocking forever.
            tokens = self.tpm

        with self._condition:
            while True:
                now = time.monotonic()
                cutoff = now - 60.0
                while self._token_reservations and self._token_reservations[0][0] <= cutoff:
                    self._token_reservations.popleft()

                wait_until = max(self._blocked_until, self._next_request_at)
                if self.tpm and tokens:
                    used = sum(reserved for _, reserved in self._token_reservations)
                    if used + tokens > self.tpm and self._token_reservations:
                        wait_until = max(wait_until, self._token_reservations[0][0] + 60.0)

                delay = wait_until - now
                if delay > 0:
                    self._condition.wait(timeout=delay)
                    continue

                started_at = time.monotonic()
                if self.rpm:
                    self._next_request_at = started_at + self._request_interval
                if self.tpm and tokens:
                    self._token_reservations.append((started_at, tokens))
                return

    def defer(self, seconds: float) -> None:
        """Apply a provider-requested cooldown to all waiting threads."""
        with self._condition:
            self._blocked_until = max(self._blocked_until, time.monotonic() + max(0.0, seconds))
            self._condition.notify_all()


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _record_usage_safely(**kwargs) -> None:
    """Record request telemetry without invalidating a successful LLM call."""
    try:
        from rolesail.usage import record_usage

        record_usage(**kwargs)
    except Exception:  # Telemetry must never break the requested work.
        log.exception("Could not persist LLM usage telemetry")


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        self.provider = (
            "gemini" if self._is_gemini else
            "openai" if base_url.startswith("https://api.openai.com") else
            "local"
        )
        hosted_provider = base_url.startswith((_GEMINI_COMPAT_BASE, "https://api.openai.com"))
        default_rpm = _DEFAULT_HOSTED_RPM if hosted_provider else 0
        self._rate_limiter = RateLimiter(
            rpm=_env_rate_limit("LLM_RPM", default_rpm),
            tpm=_env_rate_limit("LLM_TPM", 0),
        )
        log.info(
            "LLM rate limits: %s RPM, %s TPM",
            self._rate_limiter.rpm or "unlimited",
            self._rate_limiter.tpm or "unlimited",
        )

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        metadata = data.get("usageMetadata", {})
        _record_usage_safely(
            provider=self.provider,
            model=self.model,
            tokens={
                "input": metadata.get("promptTokenCount"),
                "output": metadata.get("candidatesTokenCount"),
                "cache_read": metadata.get("cachedContentTokenCount", 0),
                "cache_write": 0,
            },
        )
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # OpenAI's Chat Completions API renamed `max_tokens` to
        # `max_completion_tokens` for newer models (e.g. gpt-5*, o-series).
        # Gemini's OpenAI-compat layer and local OpenAI-compat servers still
        # expect the legacy `max_tokens`, so keep the rename scoped to OpenAI.
        is_openai = self.base_url.startswith("https://api.openai.com")
        token_param = "max_completion_tokens" if is_openai else "max_tokens"
        payload: dict = {
            "model": self.model,
            "messages": messages,
            token_param: max_tokens,
        }
        # gpt-5* / o-series reject non-default temperature values.
        if not is_openai or _openai_allows_custom_temperature(self.model):
            payload["temperature"] = temperature
        elif temperature != 1:
            log.debug(
                "Omitting temperature=%.2f for model '%s' (only default supported)",
                temperature,
                self.model,
            )

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        text, usage = self._handle_compat_response(resp)
        prompt_details = usage.get("prompt_tokens_details") or {}
        prompt_tokens = usage.get("prompt_tokens")
        cache_read_tokens = prompt_details.get("cached_tokens", 0) or 0
        cache_write_tokens = prompt_details.get("cache_write_tokens", 0) or 0
        uncached_input_tokens = (
            None
            if prompt_tokens is None
            else max(0, int(prompt_tokens) - int(cache_read_tokens) - int(cache_write_tokens))
        )
        _record_usage_safely(
            provider=self.provider,
            model=self.model,
            tokens={
                "input": uncached_input_tokens,
                "output": usage.get("completion_tokens"),
                "cache_read": cache_read_tokens,
                "cache_write": cache_write_tokens,
            },
        )
        return text

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> tuple[str, dict]:
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"], data.get("usage") or {}

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        estimated_tokens = (
            sum(len(str(message.get("content", ""))) for message in messages) // 4
            + max_tokens
        )

        for attempt in range(_MAX_RETRIES):
            try:
                self._rate_limiter.acquire(estimated_tokens)
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Re-enter through chat so the native request gets the same
                # pacing, retry, and global 429 handling as every other call.
                return self.chat(messages, temperature=temperature, max_tokens=max_tokens)

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    wait += random.uniform(0.0, min(1.0, wait * 0.1))

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    if resp.status_code == 429:
                        self._rate_limiter.defer(wait)
                    else:
                        time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None
_instance_lock = threading.Lock()


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                base_url, model, api_key = _detect_provider()
                log.info("LLM provider: %s  model: %s", base_url, model)
                _instance = LLMClient(base_url, model, api_key)
    return _instance
