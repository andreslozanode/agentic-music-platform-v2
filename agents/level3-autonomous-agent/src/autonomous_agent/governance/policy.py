"""Policy-as-code engine. Every decision carries rule ids for the audit trail."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from autonomous_agent.config import Environment
from autonomous_agent.governance.guardrails import (
    PIIDetector,
    PromptInjectionDetector,
    contains_secret,
)

CITATION_RE = re.compile(r"\[S\d+\]")


class LLMPolicy(BaseModel):
    allowed_providers: dict[str, list[str]]
    max_tokens_per_run: int = Field(ge=1000)


class DataPolicy(BaseModel):
    allowed_music_modes: dict[str, list[str]]
    classifications_for_llm: list[str]
    retention_days: dict[str, int]


class ToolPolicy(BaseModel):
    allowed: list[str]
    require_approval: list[str] = Field(default_factory=list)
    max_calls_per_run: int = Field(ge=1, le=50)


class GuardrailPolicy(BaseModel):
    max_input_chars: int = Field(ge=100)
    injection_threshold: float = Field(ge=0.1, le=1.0)
    injection_action: Literal["block", "flag"]
    pii_action: Literal["redact", "block"]
    require_grounding: bool = True
    blocked_topics: list[str] = Field(default_factory=list)


class ResponsibleAIPolicy(BaseModel):
    max_hhi: float
    max_top_artist_share: float
    min_cross_source_overlap: float
    disclosure: str


class AutonomyPolicy(BaseModel):
    auto_approve: dict[str, list[str]]
    max_cycle_seconds: int


class EvaluationPolicy(BaseModel):
    min_redteam_block_rate: float
    max_false_positive_rate: float
    min_tool_selection_accuracy: float
    min_grounded_rate: float


class Policy(BaseModel):
    version: int
    name: str
    owner: str
    risk_tier: Literal["minimal", "limited", "high"]
    llm: LLMPolicy
    data: DataPolicy
    tools: ToolPolicy
    guardrails: GuardrailPolicy
    responsible_ai: ResponsibleAIPolicy
    autonomy: AutonomyPolicy
    evaluation: EvaluationPolicy


def load_policy(path: Path | None = None) -> Policy:
    if path is None:
        text = (
            resources.files("autonomous_agent.governance.policies")
            .joinpath("default.yaml")
            .read_text("utf-8")
        )
    else:
        text = path.read_text("utf-8")
    return Policy.model_validate(yaml.safe_load(text))


@dataclass
class Decision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    text: str | None = None
    flags: list[str] = field(default_factory=list)


class PolicyViolationError(RuntimeError):
    pass


class PolicyEngine:
    def __init__(self, policy: Policy, environment: Environment) -> None:
        self.policy = policy
        self.environment = environment
        self.pii = PIIDetector()
        self.injection = PromptInjectionDetector()
        self._topics = [re.compile(t, re.IGNORECASE) for t in policy.guardrails.blocked_topics]

    # ----------------------------------------------------------------- runtime
    def check_runtime(self, provider: str, music_mode: str) -> Decision:
        reasons = []
        if provider not in self.policy.llm.allowed_providers.get(self.environment, []):
            reasons.append(f"provider '{provider}' not allowed in {self.environment}")
        if music_mode not in self.policy.data.allowed_music_modes.get(self.environment, []):
            reasons.append(f"music mode '{music_mode}' not allowed in {self.environment}")
        return Decision(not reasons, reasons, ["runtime.provider", "runtime.data_mode"])

    def enforce_runtime(self, provider: str, music_mode: str) -> None:
        decision = self.check_runtime(provider, music_mode)
        if not decision.allowed:
            raise PolicyViolationError("; ".join(decision.reasons))

    # ------------------------------------------------------------------- input
    def check_input(self, text: str) -> Decision:
        g = self.policy.guardrails
        if not text.strip():
            return Decision(False, ["empty input"], ["input.empty"])
        if len(text) > g.max_input_chars:
            return Decision(False, [f"input exceeds {g.max_input_chars} chars"], ["input.length"])
        for topic in self._topics:
            if topic.search(text):
                return Decision(False, ["blocked topic"], ["input.blocked_topic"])
        assessment = self.injection.assess(text)
        flags = list(assessment.signals)
        if assessment.score >= g.injection_threshold:
            if g.injection_action == "block":
                return Decision(
                    False,
                    [f"prompt injection suspected (score={assessment.score})"],
                    ["input.prompt_injection"],
                    flags=flags,
                )
            flags.append("injection_flagged")
        redacted, kinds = self.pii.redact(text)
        if kinds:
            if g.pii_action == "block":
                return Decision(False, [f"PII in input: {kinds}"], ["input.pii"], flags=flags)
            flags.extend(f"pii:{k}" for k in kinds)
        return Decision(True, [], ["input.ok"], text=redacted, flags=flags)

    # ------------------------------------------------------------------- tools
    def check_tool(self, name: str, calls_so_far: int) -> Decision:
        t = self.policy.tools
        if name not in t.allowed:
            return Decision(False, [f"tool '{name}' not allowlisted"], ["tool.allowlist"])
        if name in t.require_approval:
            return Decision(False, [f"tool '{name}' requires human approval"], ["tool.approval"])
        if calls_so_far >= t.max_calls_per_run:
            return Decision(False, ["tool call budget exhausted"], ["tool.budget"])
        return Decision(True, [], ["tool.ok"])

    def requires_approval(self, action: str) -> bool:
        return action in self.policy.tools.require_approval

    def auto_approved(self, action: str) -> bool:
        return action in self.policy.autonomy.auto_approve.get(self.environment, [])

    # ------------------------------------------------------------------ output
    def check_output(self, text: str, *, canary: str, used_tools: bool, n_sources: int) -> Decision:
        if canary and canary in text:
            return Decision(False, ["system prompt leakage"], ["output.canary"])
        if contains_secret(text):
            return Decision(False, ["secret-like content in output"], ["output.secret"])
        redacted, kinds = self.pii.redact(text)
        flags = [f"pii_redacted:{k}" for k in kinds]
        grounded = used_tools or (n_sources > 0 and bool(CITATION_RE.search(text)))
        if self.policy.guardrails.require_grounding and not grounded:
            return Decision(
                False,
                ["answer is not grounded in tools or cited sources"],
                ["output.grounding"],
                text=redacted,
                flags=flags,
            )
        return Decision(True, [], ["output.ok"], text=redacted, flags=flags)
