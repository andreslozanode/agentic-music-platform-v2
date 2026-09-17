# Security policy

## Reporting a vulnerability

Email `security@example.org` with the affected component, reproduction steps and impact. Please do
not open a public issue. Acknowledgement within 2 business days; fix or mitigation plan within 10
business days for high and critical findings. Coordinated disclosure is appreciated.

## Supported versions

The `main` branch and the latest tagged release receive security fixes.

## Controls in this repository

**Build and supply chain**
- GitHub Actions pinned by commit SHA; Dependabot keeps pins current; `harden-runner` audits egress.
- `uv.lock` with `--frozen` everywhere; `pip-audit` against the exported lock.
- CodeQL (security-extended), bandit, Trivy (filesystem, config, image), gitleaks, checkov,
  OpenSSF Scorecard.
- Images built once and promoted by digest; Trivy blocks before signing; SPDX SBOM and SLSA
  provenance attested; cosign keyless signature verified again at deploy time and by Kyverno
  admission.

**Runtime**
- Non-root (UID 10001), read-only root filesystem, all capabilities dropped, `RuntimeDefault`
  seccomp, no service-account token, CPU/memory limits, PSA `restricted` namespaces.
- Default-deny NetworkPolicies with explicit DNS, in-release and public-HTTPS egress.
- Secrets mounted as files (`0440`) and read via `pydantic-settings`; never environment variables,
  never in values files. API keys are stored as SHA-256 digests and compared in constant time.
- Authentication by API key or OIDC JWT (JWKS, issuer and audience verified) with RBAC roles
  `reader`, `operator`, `approver`, `auditor`; rate limiting and request-size limits; strict
  security headers; API docs disabled in production.
- Outbound HTTP goes through an allowlisted, HTTPS-only client with rate limiting and retry
  handling (SSRF protection).

**AI-specific (OWASP LLM Top 10 2025)**
- Deterministic input guardrails (injection scoring in English and Spanish, hidden-Unicode
  detection, tag spoofing, blocked topics, PII redaction).
- Untrusted-data wrapping for every tool result and retrieved passage; injection screening and
  quarantine of retrieved chunks; only DQ-gated gold documents are indexed.
- Output guardrails: canary-based system-prompt leak detection, secret scanning, PII redaction,
  grounding requirement with citations, revise-then-fallback loop.
- Tool allowlist, per-run tool and token budgets, human approval for publication.
- Red-team and golden evaluation suites gate every release; thresholds live in the governance
  policy.
- HMAC-SHA256 hash-chained audit log; query and answer text are stored only as hashes.

## Handling secrets

If a secret is ever committed: rotate it first, then purge history. Allowlisting a leaked secret in
the scanner configuration is never an acceptable fix, and the gate is configured to fail on any
gitleaks finding.
