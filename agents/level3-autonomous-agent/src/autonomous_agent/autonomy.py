"""Autonomous cycle: ingest -> medallion -> index -> brief -> governed publication.

Designed to run as a Kubernetes CronJob (``run-cycle``) or a long-running loop.
Publication is a high-risk action: it is queued for human approval unless the
environment policy explicitly auto-approves it (dev/ci only by default).
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from autonomous_agent import observability as obs
from autonomous_agent.data.quality import DataQualityError
from autonomous_agent.runtime import Runtime

BRIEF_PROMPT = (
    "Write the monthly music intelligence brief from the knowledge base: global top chart of "
    "the last month, chart diversity report, emerging artists and latest releases."
)


class CycleReport(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: Literal["ok", "degraded", "failed"] = "ok"
    rows: dict[str, int] = Field(default_factory=dict)
    quality_failures: list[str] = Field(default_factory=list)
    fairness_flags: list[str] = Field(default_factory=list)
    index: dict[str, int] = Field(default_factory=dict)
    brief_request_id: str | None = None
    brief_status: str | None = None
    approval_id: str | None = None
    published: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class AutonomousCycle:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.reports_dir = rt.settings.state_dir / "reports"
        (self.reports_dir / "pending").mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "published").mkdir(parents=True, exist_ok=True)

    async def run_once(self, actor: str = "autonomous-cycle") -> CycleReport:
        report = CycleReport(run_id=uuid.uuid4().hex, started_at=datetime.now(UTC))
        started = time.perf_counter()
        self.rt.audit.record("cycle_started", actor, {"run_id": report.run_id})
        try:
            async with asyncio.timeout(self.rt.policy.autonomy.max_cycle_seconds):
                await self._steps(report, actor)
        except DataQualityError as exc:
            report.status = "failed"
            report.quality_failures = [f.name for f in exc.report.failures]
            report.errors["data_quality"] = str(exc)
            obs.DQ_FAILURES.labels(table=exc.report.table).inc()
        except TimeoutError:
            report.status = "failed"
            report.errors["timeout"] = "cycle exceeded max_cycle_seconds"
        report.finished_at = datetime.now(UTC)
        obs.CYCLES.labels(outcome=report.status).inc()
        obs.CYCLE_SECONDS.observe(time.perf_counter() - started)
        self.rt.audit.record("cycle_finished", actor, report.model_dump(mode="json"))
        return report

    async def _steps(self, report: CycleReport, actor: str) -> None:
        with obs.span("cycle.ingest"):
            snap = await self.rt.service.snapshot()
        if snap.errors:
            report.status = "degraded"
            report.errors.update({f"source.{k}": v for k, v in snap.errors.items()})

        with obs.span("cycle.medallion"):
            result = await asyncio.to_thread(self.rt.pipeline.run, snap, report.run_id)
        report.rows = result.rows
        report.quality_failures = [
            f"{q.table}:{f.name}" for q in result.quality for f in q.failures
        ]
        if result.fairness:
            report.fairness_flags = result.fairness.flags
            for flag in result.fairness.flags:
                obs.FAIRNESS_FLAGS.labels(flag=flag.split("(")[0]).inc()

        with obs.span("cycle.index"):
            report.index = await self.rt.indexer.index(result.documents, result.snapshot_date)

        with obs.span("cycle.brief"):
            brief = await self.rt.agent.ask(BRIEF_PROMPT, principal=actor)
        report.brief_request_id, report.brief_status = brief.request_id, brief.status
        if brief.status != "answered":
            report.status = "degraded"
            report.errors["brief"] = f"brief status {brief.status}"
            return

        path = self.reports_dir / "pending" / f"brief-{result.snapshot_date}-{report.run_id[:8]}.md"
        sources = "\n".join(
            f"- {s.citation} {s.title} ({s.source}, {s.as_of})" for s in brief.sources
        )
        fairness = ", ".join(report.fairness_flags) or "none"
        path.write_text(
            f"# Music intelligence brief - {result.snapshot_date}\n\n{brief.answer}\n\n"
            f"## Sources\n{sources}\n\n## Governance\n- run_id: {report.run_id}\n"
            f"- request_id: {brief.request_id}\n- responsible-AI flags: {fairness}\n",
            "utf-8",
        )
        approval = self.rt.approvals.request(
            "publish_report",
            {"path": str(path), "run_id": report.run_id, "request_id": brief.request_id},
            requested_by=actor,
        )
        report.approval_id = approval.id
        if self.rt.engine.auto_approved("publish_report"):
            rule = f"autonomy.auto_approve.{self.rt.settings.environment}"
            self.rt.approvals.auto_approve(approval.id, rule)
        report.published = self.publish_approved(actor)

    def publish_approved(self, actor: str) -> list[str]:
        published: list[str] = []
        for item in self.rt.approvals.list("approved"):
            if item.action != "publish_report":
                continue
            src = Path(str(item.payload["path"])).resolve()
            pending = (self.reports_dir / "pending").resolve()
            if not src.exists() or src.parent != pending:  # path-traversal guard
                self.rt.audit.record(
                    "publish_skipped",
                    actor,
                    {"approval_id": item.id, "reason": "missing or invalid path"},
                )
                self.rt.approvals.mark_executed(item.id)
                continue
            dest = self.reports_dir / "published" / src.name
            shutil.move(str(src), dest)
            self.rt.approvals.mark_executed(item.id)
            self.rt.audit.record(
                "report_published",
                actor,
                {
                    "approval_id": item.id,
                    "reviewer": item.reviewer,
                    "path": str(dest),
                },
            )
            published.append(str(dest))
        return published

    async def run_forever(self, interval_s: int, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                continue
