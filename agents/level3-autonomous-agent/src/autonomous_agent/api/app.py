"""FastAPI application for the governed autonomous agent."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from autonomous_agent import __version__
from autonomous_agent.api.security import (
    Authenticator,
    HardeningMiddleware,
    Principal,
    require_role,
)
from autonomous_agent.autonomy import AutonomousCycle, CycleReport
from autonomous_agent.config import Settings
from autonomous_agent.governance.approvals import Approval, ApprovalError
from autonomous_agent.governance.audit import VerificationResult
from autonomous_agent.graph.workflow import GovernedAnswer
from autonomous_agent.responsible_ai.model_card import render_model_card
from autonomous_agent.runtime import Runtime
from music_agent.models import MusicSnapshot

RuntimeFactory = Callable[[], Awaitable[Runtime]]


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


RT = Annotated[Runtime, Depends(get_runtime)]


class AskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)


class DecisionRequest(BaseModel):
    approve: bool
    reason: str = Field(min_length=5, max_length=500)


def create_app(settings: Settings | None = None, factory: RuntimeFactory | None = None) -> FastAPI:
    settings = settings or Settings()
    make_runtime = factory or (lambda: Runtime.create(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = await make_runtime()
        app.state.runtime = rt
        app.state.cycle = AutonomousCycle(rt)
        try:
            yield
        finally:
            await rt.close()

    docs = settings.environment != "prod"
    app = FastAPI(
        title="Autonomous Music Intelligence Agent",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.authenticator = Authenticator(settings)
    app.add_middleware(
        HardeningMiddleware,
        max_body=settings.max_body_bytes,
        per_minute=settings.rate_limit_per_minute,
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(runtime: RT) -> dict[str, Any]:
        vector_ok = await runtime.store.healthy()
        if not vector_ok:
            raise HTTPException(503, "vector store unavailable")
        return {"status": "ready", "policy": runtime.policy.name, "provider": runtime.provider.name}

    @app.post("/v1/ask", response_model=GovernedAnswer)
    async def ask(
        body: AskRequest,
        runtime: RT,
        principal: Annotated[Principal, Depends(require_role("reader"))],
    ) -> GovernedAnswer:
        return await runtime.agent.ask(body.query, principal=principal.subject)

    @app.get("/v1/music/snapshot", response_model=MusicSnapshot)
    async def snapshot(
        runtime: RT, _: Annotated[Principal, Depends(require_role("reader"))]
    ) -> MusicSnapshot:
        return await runtime.service.snapshot()

    @app.post("/v1/cycles", response_model=CycleReport)
    async def run_cycle(
        request: Request,
        principal: Annotated[Principal, Depends(require_role("operator"))],
    ) -> CycleReport:
        cycle: AutonomousCycle = request.app.state.cycle
        return await cycle.run_once(actor=f"api:{principal.subject}")

    @app.get("/v1/approvals", response_model=list[Approval])
    async def approvals(
        runtime: RT, _: Annotated[Principal, Depends(require_role("approver"))]
    ) -> list[Approval]:
        return runtime.approvals.list("pending")

    @app.post("/v1/approvals/{approval_id}", response_model=Approval)
    async def decide(
        approval_id: str,
        body: DecisionRequest,
        request: Request,
        runtime: RT,
        principal: Annotated[Principal, Depends(require_role("approver"))],
    ) -> Approval:
        try:
            item = runtime.approvals.decide(
                approval_id, approve=body.approve, reviewer=principal.subject, reason=body.reason
            )
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime.audit.record(
            "approval_decided",
            principal.subject,
            {
                "approval_id": approval_id,
                "approved": body.approve,
                "reason": body.reason,
            },
        )
        if item.status == "approved":
            cycle: AutonomousCycle = request.app.state.cycle
            cycle.publish_approved(actor=principal.subject)
        return item

    @app.get("/v1/governance/audit/verify", response_model=VerificationResult)
    async def verify(
        runtime: RT, _: Annotated[Principal, Depends(require_role("auditor"))]
    ) -> VerificationResult:
        return runtime.audit.verify()

    @app.get("/v1/governance/model-card")
    async def model_card(
        runtime: RT, _: Annotated[Principal, Depends(require_role("reader"))]
    ) -> dict[str, str]:
        return {"markdown": render_model_card(runtime)}

    return app
