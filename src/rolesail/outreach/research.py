"""Bounded retrieval of public, official company pages."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

MAX_PAGE_BYTES = 500_000
PAGE_PATHS = ("/", "/about", "/careers")


def _public_host(host: str) -> bool:
    if not host or host.lower() == "localhost":
        return False
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            return False
    return True


def fetch_official_pages(domain: str, *, client: httpx.Client | None = None) -> list[dict]:
    """Return short text extracts from up to three HTTPS pages on one domain."""
    domain = domain.strip().lower().removeprefix("www.")
    if not domain or "/" in domain or not _public_host(domain):
        return []
    http = client or httpx.Client(timeout=10, follow_redirects=False)
    results: list[dict] = []
    for path in PAGE_PATHS:
        url = f"https://{domain}{path}"
        try:
            response = http.get(url, headers={"User-Agent": "RoleSail/1.0 (+company research)"})
            if response.status_code in {301, 302, 303, 307, 308}:
                redirect = urljoin(url, response.headers.get("Location", ""))
                parsed = urlparse(redirect)
                if parsed.scheme != "https" or parsed.hostname not in {domain, f"www.{domain}"}:
                    continue
                response = http.get(redirect, headers={"User-Agent": "RoleSail/1.0 (+company research)"})
                url = redirect
            if response.status_code != 200 or "text/html" not in response.headers.get("Content-Type", ""):
                continue
            body = response.content[:MAX_PAGE_BYTES]
            soup = BeautifulSoup(body, "html.parser")
            for node in soup(["script", "style", "noscript", "svg"]):
                node.decompose()
            text = " ".join(soup.get_text(" ", strip=True).split())[:6000]
            if text:
                results.append({"url": url, "text": text})
        except httpx.HTTPError:
            continue
    return results
