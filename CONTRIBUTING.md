# Contributing

1. `make setup`, then work in a feature branch.
2. `make fmt lint types test` before pushing; `make gates` reproduces the CI pre-deploy gate.
3. Tests are required for behaviour changes. Guardrail, tool or prompt changes also need a
   red-team or golden case (regression first, fix second).
4. Policy changes (`governance/policies/default.yaml`) need AI-governance review and must be
   mirrored into `deploy/helm/agentic-platform/files/policy.yaml` (`make policy-check`).
5. Never commit secrets. If one leaks, rotate first, then purge history.
6. Conventional commit subjects (`feat:`, `fix:`, `chore:`, `docs:`) keep the release notes useful.
