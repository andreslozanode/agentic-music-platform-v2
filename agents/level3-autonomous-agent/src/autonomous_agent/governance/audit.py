"""Tamper-evident audit log.

Each record is chained to its predecessor with HMAC-SHA256
(``hash_n = HMAC(key, hash_{n-1} || canonical_json(record_n))``). Editing, deleting
or reordering any line breaks verification. Payloads are redacted before hashing.
One file per pod avoids cross-writer races; files are shipped to immutable object
storage (WORM / object lock) by the platform.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_core.observability import redaction_processor

GENESIS = "0" * 64
DEV_KEY = b"dev-only-audit-key-do-not-use-in-prod"


class AuditRecord(BaseModel):
    seq: int
    ts: datetime
    event: str
    actor: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str = ""


class VerificationResult(BaseModel):
    valid: bool
    records: int
    first_invalid_seq: int | None = None
    reason: str | None = None


def _canonical(record: AuditRecord) -> bytes:
    body = record.model_dump(mode="json", exclude={"hash"})
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class AuditLog:
    def __init__(self, path: Path, key: bytes | None = None) -> None:
        self.path = path
        self._key = key or DEV_KEY
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._seq, self._last = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last_line = ""
        with self.path.open("rb") as fh:
            for line in fh:
                if line.strip():
                    last_line = line.decode()
        if not last_line:
            return 0, GENESIS
        rec = AuditRecord.model_validate_json(last_line)
        return rec.seq, rec.hash

    def _sign(self, record: AuditRecord) -> str:
        msg = record.prev_hash.encode() + _canonical(record)
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def record(self, event: str, actor: str, payload: dict[str, Any] | None = None) -> AuditRecord:
        safe = dict(redaction_processor(None, "audit", dict(payload or {})))
        with self._lock:
            rec = AuditRecord(
                seq=self._seq + 1,
                ts=datetime.now(UTC),
                event=event,
                actor=actor,
                payload=json.loads(json.dumps(safe, default=str)),
                prev_hash=self._last,
            )
            rec.hash = self._sign(rec)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(rec.model_dump_json() + "\n")
            self._seq, self._last = rec.seq, rec.hash
        return rec

    def records(self) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        lines = self.path.read_text("utf-8").splitlines()
        return [AuditRecord.model_validate_json(line) for line in lines if line.strip()]

    def verify(self) -> VerificationResult:
        prev, expected_seq, count = GENESIS, 1, 0
        if not self.path.exists():
            return VerificationResult(valid=True, records=0)
        for line in self.path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = AuditRecord.model_validate_json(line)
            except ValueError:
                return VerificationResult(
                    valid=False,
                    records=count,
                    first_invalid_seq=expected_seq,
                    reason="unparseable record",
                )
            if rec.seq != expected_seq or rec.prev_hash != prev:
                return VerificationResult(
                    valid=False, records=count, first_invalid_seq=rec.seq, reason="broken chain"
                )
            if not hmac.compare_digest(rec.hash, self._sign(rec)):
                return VerificationResult(
                    valid=False, records=count, first_invalid_seq=rec.seq, reason="bad signature"
                )
            prev, expected_seq, count = rec.hash, expected_seq + 1, count + 1
        return VerificationResult(valid=True, records=count)
