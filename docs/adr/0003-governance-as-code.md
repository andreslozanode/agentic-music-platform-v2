# ADR 0003 - AI governance as code, enforced at runtime

- Status: accepted
- Date: 2026-09
- Deciders: AI governance, security, platform team

## Context

Governance that lives in a wiki is not enforced. The system is an autonomous agent that calls
tools, reads retrieved data and publishes reports, so the EU AI Act transparency obligations
(limited-risk tier) and the OWASP LLM Top 10 both apply.

## Decision

- A single YAML **policy** declares allowed providers and data modes per environment, the tool
  allowlist, approval requirements, budgets, guardrail thresholds, responsible-AI thresholds,
  autonomy rules and evaluation thresholds. It is validated by Pydantic at startup.
- The policy is **packaged with the application** and mounted in Kubernetes from a ConfigMap; CI
  fails if the chart copy drifts from the package copy.
- Every decision (`input`, `tool`, `output`, runtime) returns rule ids that are written to a
  **tamper-evident audit chain**, so an auditor can reconstruct why an answer was allowed.
- Thresholds are never tuned in CI YAML: the evaluation gate reads them from the policy, and the
  policy is owned by governance CODEOWNERS.
- Guardrails are **deterministic code**, not a model: they cannot themselves be prompt-injected,
  they run in microseconds and they are unit-tested per pattern.

## Consequences

- Changing what the agent may do is a reviewed code change with a diff and an audit trail.
- A new jailbreak becomes a red-team dataset entry plus a rule, and the gate blocks regressions.
- Deterministic detectors have false negatives against novel phrasings; the output guard,
  budgets and human approval are the compensating controls. Adding a model-based classifier stays
  possible behind the same `PolicyEngine` interface.
