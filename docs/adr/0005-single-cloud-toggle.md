# ADR 0005 - One cloud toggle, workload identity everywhere

- Status: accepted
- Date: 2026-09

## Context

The platform must run on AWS, GCP or Azure without forking the code, and static cloud credentials
are the most common cause of long-lived compromise.

## Decision

- `AGENT_CLOUD` (`local|aws|gcp|azure`) is the only switch in the application. It is validated
  against the storage URI scheme at startup, so a mismatched configuration fails fast instead of
  failing at the first write.
- Credentials always come from the platform: IRSA/Pod Identity, GKE Workload Identity or Azure
  Workload Identity, injected through the ServiceAccount annotation set in the values file.
  Configuration explicitly rejects credential-like keys in `AGENT_STORAGE_OPTIONS_JSON`.
- In CI/CD, the same toggle (`vars.CLOUD`) selects which OIDC login step runs; no cloud secret is
  stored in GitHub.
- Application secrets are mounted as files, read through `pydantic-settings` `secrets_dir`, so they
  never appear in the pod spec or the process environment.

## Consequences

- Porting to another cloud is a values file plus an identity binding.
- Local development stays credential-free (`cloud=local` + fixtures).
- Cloud-specific object-store tuning must go through `storage_options`, which is deliberately
  limited to non-credential keys.
