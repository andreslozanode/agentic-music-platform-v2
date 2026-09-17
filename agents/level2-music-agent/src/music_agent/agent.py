"""Level 2 agent factory."""

from __future__ import annotations

from agent_core import ToolCallingAgent, build_provider
from agent_core.config import LLMSettings
from agent_core.llm import LLMProvider
from music_agent.config import MusicSettings
from music_agent.service import MusicIntelligenceService
from music_agent.tools import MusicTools

SYSTEM_PROMPT = """You are a music-intelligence analyst.
- Use tools for every factual claim about releases, artists, playlists or charts.
- State the data source and period for every chart ("ListenBrainz sitewide, last month").
- ListenBrainz charts reflect ListenBrainz users' listens, not official Spotify charts; say so
  when users ask for "Spotify Top 50".
- "New artists" is a heuristic signal; present it as such.
- Keep answers concise, use tables for rankings, reply in the user's language."""


def build_agent(
    settings: MusicSettings | None = None,
    llm_settings: LLMSettings | None = None,
    *,
    service: MusicIntelligenceService | None = None,
    provider: LLMProvider | None = None,
) -> tuple[ToolCallingAgent, MusicIntelligenceService]:
    svc = service or MusicIntelligenceService(settings)
    agent = ToolCallingAgent(
        provider or build_provider(llm_settings),
        MusicTools(svc).registry(),
        SYSTEM_PROMPT,
        max_steps=5,
        max_tool_calls=8,
    )
    return agent, svc
