"""Shared discovery transport and description normalization helpers."""

from __future__ import annotations

import html as html_module
import json
import logging
import time
import urllib.error
import urllib.request

from rolesail.discovery.workday import strip_html

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def _http_get_json(url: str, max_retries: int = 3, backoff: float = 2.0) -> dict:
    """GET a URL with retries on 429 / transient failures. Returns parsed JSON."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                raise
            if e.code == 429 and attempt < max_retries:
                wait = backoff * (attempt + 1) * 2
                log.warning("429 from %s, retry %d/%d in %.0fs", url, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            if attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("HTTP %s from %s, retry %d/%d in %.0fs",
                            e.code, url, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Transient error on %s: %s -- retry %d/%d in %.0fs",
                            url, e, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            raise
    if last_err:
        raise last_err
    return {}


def _http_request(
    url: str,
    data: bytes | None = None,
    headers: dict | None = None,
    max_retries: int = 3,
    backoff: float = 2.0,
) -> bytes:
    """Make a GET or POST request with retries and return its raw body."""
    request_headers = {"User-Agent": UA}
    request_headers.update(headers or {})
    last_err: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=request_headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in (429, 500, 502, 503, 504) or attempt >= max_retries:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt >= max_retries:
                raise

        wait = backoff * (attempt + 1)
        log.warning(
            "Transient error from %s, retry %d/%d in %.0fs",
            url,
            attempt + 1,
            max_retries,
            wait,
        )
        time.sleep(wait)

    if last_err:
        raise last_err
    return b""


def _normalize_description(content: str | None) -> str:
    """Greenhouse returns `content` as HTML-escaped HTML (entities like
    `&lt;p&gt;`). Unescape once so the HTML stripper can do its job, then
    convert tags to plain text.
    """
    if not content:
        return ""
    decoded = html_module.unescape(content)
    return strip_html(decoded)
