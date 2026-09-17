"""AI system card generated from live configuration (transparency, EU AI Act Art. 50)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autonomous_agent.runtime import Runtime


def render_model_card(rt: Runtime, eval_report: Path | None = None) -> str:
    p = rt.policy
    evals = "_No evaluation report attached._"
    if eval_report and eval_report.exists():
        data = json.loads(eval_report.read_text("utf-8"))
        evals = "\n".join(f"| {k} | {v} |" for k, v in data.get("metrics", {}).items())
        evals = "| metric | value |\n|---|---|\n" + evals
    tools = ", ".join(p.tools.allowed)
    return f"""# AI System Card - Autonomous Music Intelligence Agent

| Field | Value |
|---|---|
| Environment | {rt.settings.environment} |
| Cloud | {rt.settings.cloud} |
| LLM provider / model | {rt.provider.name} / {rt.provider.model} |
| Embeddings | {rt.embedder.name} (dim {rt.embedder.dim}) |
| Vector store | Qdrant ({rt.settings.vector_backend}), collection `{rt.settings.collection}` |
| Policy | {p.name} v{p.version} (owner {p.owner}) |
| Risk tier | {p.risk_tier} |

## Intended use
Summarise public music-consumption signals (latest releases, emerging artists, playlists,
monthly global chart) for analysts. **Not** for decisions about individuals, royalties,
or any consequential decision.

## Data
ListenBrainz (open data, sitewide aggregates), Deezer public API, optional Spotify search.
No personal data is ingested; user queries are PII-redacted and only hashed in the audit log.

## Controls
- Input guardrails: max {p.guardrails.max_input_chars} chars, prompt-injection threshold
  {p.guardrails.injection_threshold} ({p.guardrails.injection_action}),
  PII {p.guardrails.pii_action}.
- Tool allowlist: {tools}; approval required for: {", ".join(p.tools.require_approval)}.
- Budgets: {p.tools.max_calls_per_run} tool calls and {p.llm.max_tokens_per_run} tokens per run.
- Output: grounding required={p.guardrails.require_grounding}, canary-based prompt-leak
  detection, secret and PII scanning, AI disclosure appended.
- Tamper-evident HMAC-chained audit log; four-eyes approval for publication.
- Responsible AI: chart concentration (HHI <= {p.responsible_ai.max_hhi}), dominant-artist share
  (<= {p.responsible_ai.max_top_artist_share}), cross-source agreement monitoring.

## Known limitations
- Charts reflect ListenBrainz/Deezer user bases (demographic and popularity bias).
- "New artists" is a heuristic, not ground truth.
- LLM output can still be wrong; sources are always listed for verification.

## Evaluation
{evals}

## Disclosure
{p.responsible_ai.disclosure}
"""
