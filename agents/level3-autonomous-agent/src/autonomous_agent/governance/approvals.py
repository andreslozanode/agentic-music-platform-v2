"""Human-in-the-loop approvals with four-eyes enforcement."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["pending", "approved", "rejected", "executed"]


class Approval(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    action: str
    payload: dict[str, Any]
    requested_by: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Status = "pending"
    reviewer: str | None = None
    reason: str | None = None
    decided_at: datetime | None = None


class ApprovalError(RuntimeError):
    pass


class ApprovalStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Approval]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text("utf-8"))
        return {k: Approval.model_validate(v) for k, v in raw.items()}

    def _save(self, items: dict[str, Approval]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({k: v.model_dump(mode="json") for k, v in items.items()}, indent=1),
            "utf-8",
        )
        tmp.replace(self.path)  # atomic on POSIX

    def request(self, action: str, payload: dict[str, Any], requested_by: str) -> Approval:
        with self._lock:
            items = self._load()
            item = Approval(action=action, payload=payload, requested_by=requested_by)
            items[item.id] = item
            self._save(items)
            return item

    def decide(self, approval_id: str, *, approve: bool, reviewer: str, reason: str) -> Approval:
        with self._lock:
            items = self._load()
            item = items.get(approval_id)
            if item is None:
                raise ApprovalError("approval not found")
            if item.status != "pending":
                raise ApprovalError(f"approval already {item.status}")
            if reviewer == item.requested_by:
                raise ApprovalError("four-eyes principle: requester cannot approve")
            if not reason.strip():
                raise ApprovalError("a justification is required")
            item.status = "approved" if approve else "rejected"
            item.reviewer, item.reason = reviewer, reason
            item.decided_at = datetime.now(UTC)
            self._save(items)
            return item

    def auto_approve(self, approval_id: str, policy_rule: str) -> Approval:
        with self._lock:
            items = self._load()
            item = items[approval_id]
            item.status, item.reviewer = "approved", f"policy:{policy_rule}"
            item.reason, item.decided_at = "auto-approved by environment policy", datetime.now(UTC)
            self._save(items)
            return item

    def mark_executed(self, approval_id: str) -> None:
        with self._lock:
            items = self._load()
            items[approval_id].status = "executed"
            self._save(items)

    def list(self, status: Status | None = None) -> list[Approval]:
        items = sorted(self._load().values(), key=lambda a: a.requested_at)
        return [a for a in items if status is None or a.status == status]
