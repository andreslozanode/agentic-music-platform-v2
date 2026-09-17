"""Hardened outbound HTTP.

* Host allowlist enforced on every request, including redirects (SSRF defence).
* HTTPS only.
* Conservative timeouts and bounded response sizes.
* Retries with exponential backoff on 429/5xx honouring ``Retry-After``.
* Async token-bucket rate limiter per upstream API.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class EgressDeniedError(RuntimeError):
    """Outbound request to a non-allowlisted destination."""


class UpstreamError(RuntimeError):
    def __init__(self, status: int, url: str, detail: str = "") -> None:
        super().__init__(f"upstream {status} for {url} {detail}".strip())
        self.status = status


class RetryableUpstreamError(UpstreamError):
    def __init__(self, status: int, url: str, retry_after: float | None) -> None:
        super().__init__(status, url, "(retryable)")
        self.retry_after = retry_after


class RateLimiter:
    """Token bucket: ``rate`` tokens per ``per`` seconds."""

    def __init__(self, rate: int, per: float) -> None:
        self.capacity = float(rate)
        self.tokens = float(rate)
        self.fill_rate = rate / per
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated) * self.fill_rate
                )
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.fill_rate)


def _enforce_allowlist(allowed_hosts: frozenset[str]) -> Any:
    async def hook(request: httpx.Request) -> None:
        if request.url.scheme != "https":
            raise EgressDeniedError(f"non-https egress blocked: {request.url.scheme}")
        host = request.url.host.lower()
        if host not in allowed_hosts:
            raise EgressDeniedError(f"egress to '{host}' is not allowlisted")

    return hook


def build_http_client(
    *,
    allowed_hosts: Iterable[str],
    user_agent: str,
    timeout_s: float = 20.0,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    hosts = frozenset(h.lower() for h in allowed_hosts)
    if not hosts:
        raise ValueError("allowed_hosts must not be empty")
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent, "Accept": "application/json", **(headers or {})},
        timeout=httpx.Timeout(timeout_s, connect=5.0),
        follow_redirects=True,
        max_redirects=3,
        event_hooks={"request": [_enforce_allowlist(hosts)]},
        transport=transport,
    )


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return min(float(value), 60.0)
    except ValueError:
        return None


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limiter: RateLimiter | None = None,
    attempts: int = 4,
    **kwargs: Any,
) -> Any:
    """Perform a request and return decoded JSON, retrying transient failures."""

    async def _once() -> Any:
        if limiter:
            await limiter.acquire()
        resp = await client.request(method, url, **kwargs)
        if resp.status_code in RETRYABLE_STATUS:
            delay = _retry_after(resp)
            if delay:
                await asyncio.sleep(delay)
            raise RetryableUpstreamError(resp.status_code, str(resp.request.url), delay)
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, str(resp.request.url), resp.text[:200])
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise UpstreamError(resp.status_code, str(resp.request.url), "response too large")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    retrying = AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        retry=retry_if_exception(
            lambda e: isinstance(e, RetryableUpstreamError | httpx.TransportError)
        ),
        reraise=True,
    )
    async for attempt in retrying:
        with attempt:
            return await _once()
    raise AssertionError("unreachable")  # pragma: no cover
