# Security gates reference

## Severity policy

| Finding | Pre-deploy | Post-deploy | Owner action |
|---|---|---|---|
| Secret in git history | **block** | - | Rotate the credential first, then purge history. Allowlisting is never a fix. |
| Critical/High CVE with fix available | **block** | Trivy re-scan nightly | Upgrade; if no fix, time-boxed exception in `SECURITY.md` with expiry. |
| SAST medium+ (bandit / semgrep / CodeQL) | **block** | - | Fix or justify inline with `# nosec <id> - reason` reviewed by CODEOWNERS. |
| Privileged pod, no limits, root user, writable root FS | **block** (checkov / Kyverno) | Kyverno admission denies | Fix the chart. |
| Unsigned image or wrong signer identity | - | **block** (cosign verify + Kyverno `verifyImages`) | Rebuild through the pipeline only. |
| Missing security header / auth not enforced | - | **block** | Fix middleware / ingress. |
| Prompt-injection probe not blocked | eval **block** | smoke **block** | Tune guardrails; add the case to `redteam.jsonl`. |
| ZAP baseline WARN | - | report | Triage within the sprint. |

## Supply chain (SLSA-style)

1. Actions pinned by commit SHA; Dependabot keeps pins fresh.
2. `uv.lock` + `--frozen` everywhere; `pip-audit` on the exported lock.
3. Image built once, promoted by digest (never rebuilt per environment).
4. SBOM (SPDX, Syft) attached as a signed attestation.
5. Build provenance via `actions/attest-build-provenance`.
6. Keyless cosign signature bound to the workflow identity:
   ```bash
   cosign verify ghcr.io/<org>/agentic-api@sha256:<digest> \
     --certificate-identity-regexp '^https://github.com/<org>/<repo>/.github/workflows/' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com
   ```
7. Cluster admission (Kyverno) rejects images without a matching signature.

## Runtime hardening checklist

- `runAsNonRoot`, UID 10001, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`,
  `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`.
- `automountServiceAccountToken: false` unless workload identity needs it.
- NetworkPolicy default-deny + explicit egress (DNS, Qdrant, LLM provider, music APIs).
- Secrets from a secret manager via External Secrets; never in values files.
- Pod Security Admission `restricted` on the namespace.

## DAST

OWASP ZAP baseline (passive) runs against the ephemeral kind deployment on every main build
and against staging after deploy. Rules tuned in `.zap/rules.tsv`.

## OWASP Top 10 for LLM applications (2025) mapping

| Risk | Control in this repo | Gate |
|---|---|---|
| LLM01 Prompt injection | heuristic detector (EN/ES, hidden unicode, tag spoofing), untrusted-data wrapping, RAG quarantine | eval + post-deploy probe |
| LLM02 Sensitive info disclosure | PII redaction in/out, secret scanning of output, audit stores hashes only | eval |
| LLM03 Supply chain | pinned actions, lockfile, SBOM, signing, provenance | pre + admission |
| LLM04 Data/model poisoning | only gold-layer (DQ-gated) docs indexed, content hashes, lineage | pipeline DQ |
| LLM05 Improper output handling | output guard, no tool executes model-generated code | tests |
| LLM06 Excessive agency | tool allowlist, budgets, human approval for publication | tests + policy |
| LLM07 System prompt leakage | canary token detection | eval |
| LLM08 Vector/embedding weaknesses | classification filter, injection screening of retrieved chunks | tests |
| LLM09 Misinformation | grounding requirement, citations, revise/fallback loop, disclosure | eval |
| LLM10 Unbounded consumption | token/tool budgets, rate limiting, body size limits, cycle timeout | tests + post-deploy |
