# ADR 0004 - Deployment testing packaged as a reusable Skill

- Status: accepted
- Date: 2026-09

## Context

The same three questions come up for every service: is it safe to build, is the rollout healthy,
and is the live release behaving? Answering them ad hoc in YAML produces gates that differ per
repository and rot quietly.

## Decision

Package the practice as a Skill (`skills/deploy-quality-gates`) with `SKILL.md`, four scripts,
three references and its own tests and evals. Constraints:

- **bash + Python standard library only**, so the scripts run in any runner, a bastion host or a
  Kubernetes Job without installing the project.
- **Machine-readable evidence** (`reports/*.jsonl|json|xml`) and one aggregated GO / NO-GO verdict.
- **Injectable dependencies** (command runner, HTTP fetch), so the gates are themselves tested -
  the post-deploy gate runs against the real FastAPI app in the test suite.
- `STRICT=1` turns a missing tool into a failure, so CI cannot silently skip a security stage.

## Consequences

- The gates are versioned, reviewed and reusable by other services in the monorepo.
- Workflow YAML stays thin: it installs tools and calls the scripts.
- The scripts encode this platform's defaults (paths, endpoints); other services override them via
  flags and environment variables.
