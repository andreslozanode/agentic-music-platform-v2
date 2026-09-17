# Governance

This directory is the human-facing side of the governance that the code enforces.

| Artefact | Where |
|---|---|
| Policy as code | `agents/level3-autonomous-agent/src/autonomous_agent/governance/policies/default.yaml` (copy in `deploy/helm/agentic-platform/files/policy.yaml`, drift checked in CI) |
| System card | generated: `autonomous-agent model-card` or `GET /v1/governance/model-card` |
| Evaluation evidence | `reports/eval.json` from `autonomous-agent evaluate`, uploaded by CI |
| Audit trail | HMAC-chained log in the state volume; verify with `autonomous-agent verify-audit` |
| Risk register | `governance/risk-register.md` |

## Change process

1. Policy changes are pull requests. `CODEOWNERS` routes them to the AI-governance team.
2. The pre-deploy gate re-runs the golden and red-team suites with the new policy; thresholds come
   from the policy itself, so lowering a threshold is visible in the diff.
3. The chart copy must be identical (`make policy-check`), so what was reviewed is what runs.
4. Material changes (new tool, new data source, autonomy expansion, risk-tier change) update the
   risk register and the system card in the same PR.

## Roles

- **reader** - ask questions, read snapshots and the system card.
- **operator** - trigger cycles.
- **approver** - approve or reject publications (cannot approve their own request: four eyes).
- **auditor** - verify the audit chain.

## Incident response for AI-specific events

1. Verify the audit chain and export the relevant records.
2. Reject any pending approvals created by the affected release.
3. Add the offending prompt or document to `redteam.jsonl` (regression first), then fix the rule.
4. If retrieved data was poisoned, restore the affected Delta version (time travel) and re-index.
5. Record the incident and the control change in the risk register.
