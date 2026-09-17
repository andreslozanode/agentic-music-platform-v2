# Test strategy for agentic services

## Pyramid

1. **Unit** (ms): pure functions - guardrails, policy decisions, metrics, parsers.
2. **Component** (ms-s): connectors against `httpx.MockTransport`; Delta tables in `tmp_path`;
   Qdrant `:memory:`.
3. **Contract**: Arrow schemas in `data/contracts.py` are the published interface; the
   `schema_contract` DQ check fails the pipeline when a producer drops a column.
4. **Agent flows**: `ScriptedProvider` replays exact LLM turns (tool call -> answer) so graph
   branches (revise, fallback, denial, canary leak) are deterministic.
5. **Evaluation**: golden + red-team datasets with thresholds in the governance policy.
   Offline (heuristic provider) on every PR; online (real model) nightly and before prod.
6. **Deployment**: kind cluster e2e with the real Helm chart, then post-deploy verification.

## Evaluation datasets

- Add a red-team case for every incident or new jailbreak pattern (regression-first).
- Keep benign look-alikes in the golden set to measure false positives.
- Never tune thresholds in CI YAML; change the policy file (reviewed by governance owners).

## Canary / progressive delivery criteria

Roll back when any holds during the observation window:
- readiness failure ratio > 5 %;
- any container restart;
- 5xx ratio > 1 % or p95 latency > budget (from Prometheus: `agent_request_seconds`);
- guardrail block rate deviates > 3x from baseline (possible attack or broken detector);
- `agent_data_quality_failures_total` increases after a cycle.

## Flaky test policy

Quarantine with `@pytest.mark.skip(reason=<ticket>)` for at most one sprint; a flaky gate is
worse than no gate because people learn to ignore red builds.
