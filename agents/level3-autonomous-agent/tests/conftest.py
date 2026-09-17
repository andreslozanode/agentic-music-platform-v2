from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from agent_core.llm import HeuristicProvider, LLMProvider
from autonomous_agent.config import Settings
from autonomous_agent.runtime import Runtime
from music_agent.config import MusicSettings
from music_agent.service import MusicIntelligenceService

RuntimeFactory = Callable[..., Awaitable[Runtime]]


def make_settings(tmp: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "ENVIRONMENT": "ci",
        "storage_root": str(tmp / "lake"),
        "state_dir": tmp / "state",
        "vector_backend": "memory",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
async def runtime_factory(tmp_path: Path) -> AsyncIterator[RuntimeFactory]:
    created: list[Runtime] = []

    async def factory(provider: LLMProvider | None = None, **overrides: Any) -> Runtime:
        rt = await Runtime.create(
            make_settings(tmp_path, **overrides),
            provider=provider or HeuristicProvider(),
            service=MusicIntelligenceService(MusicSettings(mode="fixtures")),
        )
        created.append(rt)
        return rt

    yield factory
    for rt in created:
        await rt.close()
