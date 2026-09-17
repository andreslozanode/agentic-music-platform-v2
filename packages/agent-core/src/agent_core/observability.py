"""Structured logging with secret redaction, plus a tiny async TTL cache."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog

SENSITIVE_KEYS = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
SENSITIVE_VALUES = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # provider API keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/]+=*"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
]
REDACTED = "***REDACTED***"


def redact_text(value: str) -> str:
    for pattern in SENSITIVE_VALUES:
        value = pattern.sub(REDACTED, value)
    return value


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (REDACTED if SENSITIVE_KEYS.search(str(k)) else _redact(v)) for k, v in obj.items()
        }
    if isinstance(obj, list | tuple):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def redaction_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    redacted: dict[str, Any] = _redact(dict(event_dict))
    return redacted


class _StderrLogger:
    """Resolves ``sys.stderr`` at write time (robust to stream swapping in tests/CLIs)."""

    def msg(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    log = debug = info = warn = warning = error = critical = exception = fatal = msg


def _stderr_logger_factory(*_args: Any) -> _StderrLogger:
    return _StderrLogger()


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    logging.basicConfig(stream=sys.stderr, level=level, format="%(message)s")
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redaction_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=True,
    )


class AsyncTTLCache[V]:
    """Single-flight TTL cache keyed by string."""

    def __init__(self, ttl_s: float, max_items: int = 256) -> None:
        self.ttl_s = ttl_s
        self.max_items = max_items
        self._data: dict[str, tuple[float, V]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[V]]) -> V:
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._data.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            value = await factory()
            if len(self._data) >= self.max_items:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (time.monotonic() + self.ttl_s, value)
            return value

    def clear(self) -> None:
        self._data.clear()
