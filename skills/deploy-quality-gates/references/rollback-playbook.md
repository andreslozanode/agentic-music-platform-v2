# Rollback playbook

```
deploy_watch NO-GO ─► automatic `helm rollback <release> 0 --wait`
        │
        ├─ rollback succeeded ─► verify (post_deploy_verify) ─► incident note ─► fix forward in a PR
        └─ rollback failed ────► page on-call ─► scale to last known good digest manually
```

## Commands

```bash
helm history agentic -n agentic-prod
helm rollback agentic <REVISION> -n agentic-prod --wait --timeout 5m
kubectl -n agentic-prod rollout status deploy/agentic-api
python skills/deploy-quality-gates/scripts/post_deploy_verify.py --base-url https://api.example.org \
  --api-key "$SMOKE_API_KEY" --expect-docs-disabled
```

## Data rollback (Delta time travel)

A bad cycle writes new Delta versions; partitions are replaced idempotently per snapshot date.

```python
from deltalake import DeltaTable
dt = DeltaTable("gs://bucket/lake/gold/insight_documents")
dt.history(5)                 # find the last good version
dt.restore(<version>)          # creates a new commit restoring that state
```

Then re-index: `autonomous-agent run-cycle` (unchanged documents are skipped by hash).

## Governance steps

1. Verify the audit chain: `autonomous-agent verify-audit` (or `GET /v1/governance/audit/verify`).
2. Reject pending approvals created by the bad release (`POST /v1/approvals/{id}`).
3. Record the incident, add a regression test / red-team case, update the risk register.
