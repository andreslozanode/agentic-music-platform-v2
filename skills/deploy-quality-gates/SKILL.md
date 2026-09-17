---
name: deploy-quality-gates
description: Test and security gates for shipping code to production in three phases - before deploy (lint, types, unit/contract tests, coverage, SAST, dependency and secret scanning, IaC/Kubernetes policy checks, LLM red-team evals), during deploy (rollout watch, health/error-budget monitoring, restart detection, automatic Helm rollback) and after deploy (smoke tests, auth/RBAC enforcement, security headers, guardrail probes, latency budget, DAST). Use this skill whenever the user asks to test, validate, verify, gate, smoke-test, canary, roll back or harden a deployment, release, pipeline, Helm chart, container or Kubernetes rollout, or wants a go/no-go decision - even if they only say "is it safe to ship?", "check the deploy" or "add tests to the CI/CD".
---

# Deploy quality gates

Three gates, each producing machine-readable evidence and a single GO / NO-GO verdict.
The scripts are dependency-light (bash + Python stdlib) so they run in any CI runner,
a bastion host, or a Kubernetes Job.

```
PRE-DEPLOY  ──►  DURING DEPLOY  ──►  POST-DEPLOY  ──►  gate_report.py (GO / NO-GO)
static + tests    rollout watch        smoke + security
+ security        + auto-rollback      probes + DAST
```

## When to apply which gate

| Situation | Run |
|---|---|
| Pull request / before building an image | `scripts/pre_deploy_gate.sh` |
| `helm upgrade` / `kubectl apply` in progress | `scripts/deploy_watch.py` |
| Release is live (staging or prod) | `scripts/post_deploy_verify.py` (+ ZAP baseline in CI) |
| Summarise for reviewers / GitHub step summary | `scripts/gate_report.py` |

Always run the gates in order. A failed critical check stops the pipeline: do not
"retry until green" - read the evidence, fix the cause, re-run.

## 1. Pre-deploy gate

```bash
STRICT=1 REPORT_DIR=reports skills/deploy-quality-gates/scripts/pre_deploy_gate.sh
```

Stages (critical unless noted) and why they exist:

1. **lint / format** (`ruff`) - cheap, catches unsafe patterns via the `S` (bandit) rules.
2. **types** (`mypy --strict`) - most production incidents in Python agents are shape errors.
3. **tests + coverage** (`pytest`, fail under 80 %) - JUnit written to `reports/junit.xml`.
4. **SAST** (`bandit -ll`) - medium+ severity blocks.
5. **dependency audit** (`pip-audit` on the frozen `uv` export) - known CVEs block.
6. **secret scan** (`gitleaks`) - any finding blocks; never "allowlist" a real secret, rotate it.
7. **IaC** (`helm lint`, `helm template | kubeconform`, `checkov`) - catches privileged pods,
   missing limits, host networking, latest tags.
8. **policy drift** - the AI-governance policy packaged in the app must equal the copy in the
   Helm chart.
9. **LLM evaluation** (`autonomous-agent evaluate`) - red-team block rate, false-positive rate,
   tool accuracy, grounding; thresholds live in the policy file, not in CI YAML.

`STRICT=1` turns "tool not installed" into a failure. Use it in CI; leave it off locally.

## 2. During-deploy gate

```bash
python skills/deploy-quality-gates/scripts/deploy_watch.py \
  --release agentic --namespace agentic-staging --deployment agentic-api \
  --health-url https://staging.example.org/readyz \
  --duration 180 --interval 5 --max-failure-ratio 0.05 --max-restarts 0 --rollback
```

It waits for `kubectl rollout status`, then samples the health endpoint for the
observation window and watches container restart counts. If the failure ratio or
restarts exceed the budget it runs `helm rollback <release> 0 --wait` (previous revision)
and exits `2`. Prefer `helm upgrade --atomic` as well - the watch covers failures that only
appear *after* pods report ready (crash loops, dependency outages, bad config).

## 3. Post-deploy gate

```bash
python skills/deploy-quality-gates/scripts/post_deploy_verify.py \
  --base-url https://staging.example.org --api-key "$SMOKE_API_KEY" \
  --expect-docs-disabled --latency-samples 20 --p95-budget-ms 800 \
  --json reports/post-deploy.json --junit reports/post-deploy-junit.xml
```

Checks: liveness, readiness, mandatory security headers, no server banner, unauthenticated
access rejected (401), authenticated smoke answer, prompt-injection probe is **blocked**,
API docs hidden in prod, p95 latency budget. Then run the OWASP ZAP baseline scan (see
`references/security-gates.md`).

Use a dedicated low-privilege smoke credential (role `reader` only). Never reuse an admin key.

## 4. Verdict

```bash
python skills/deploy-quality-gates/scripts/gate_report.py reports/ --output reports/gate.md
```

Exit code `0` = GO, `1` = NO-GO. The markdown is appended to `$GITHUB_STEP_SUMMARY` when set.

## Report template (when summarising to a human)

```
## Deployment gate: <GO|NO-GO> - <service> <version> -> <environment>
### Pre-deploy   <n passed>/<n total>  (list failures with the fix)
### During       rollout <ok|rolled back>, failure ratio <x>, restarts <n>
### Post-deploy  <n passed>/<n total>, p95 <ms>
### Security     SAST <n>, CVEs <n>, secrets <n>, IaC <n>, red-team block rate <x>
### Decision & next action
```

## References

- `references/security-gates.md` - what each security control catches, severity policy,
  supply-chain (SBOM, cosign signing and verification, provenance), DAST, OWASP LLM Top 10 map.
- `references/test-strategy.md` - test pyramid for agentic systems, contract tests,
  evaluation datasets, canary metrics and SLO-based rollback criteria.
- `references/rollback-playbook.md` - decision tree and commands for rollback, data rollback
  with Delta time travel, audit and communication steps.

Read the reference that matches the question; they are not needed for routine runs.
