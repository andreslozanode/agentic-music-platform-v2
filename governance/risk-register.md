# Risk register

Risk tier: **limited** (EU AI Act) - transparency obligations apply; no consequential decisions
about individuals. Reviewed quarterly by the AI-governance team; every new tool, data source or
autonomy expansion requires an entry.

| ID | Risk | Likelihood | Impact | Controls | Residual | Owner |
|---|---|---|---|---|---|---|
| R01 | Prompt injection makes the agent ignore policy or leak its instructions | High | High | Deterministic input guardrails (EN/ES, hidden Unicode, tag spoofing), canary leak detection, untrusted-data wrapping, red-team gate at 100 % block | Medium-low: novel phrasings may evade patterns; output guard and budgets compensate | AI governance |
| R02 | Indirect injection through retrieved documents | Medium | High | Only DQ-gated gold documents indexed, content hashes, chunk screening with quarantine, classification filter | Low | Platform |
| R03 | Hallucinated or ungrounded chart claims | Medium | Medium | Grounding requirement with citations, revise-then-fallback, provenance on every record, AI disclosure | Low-medium: the model can still misread a passage; sources are always shown | AI governance |
| R04 | Popularity and demographic bias in charts | High | Medium | HHI, dominant-artist share, unique-artist ratio, Gini and cross-source agreement computed per cycle; flags attached to the brief and the system card; disclosure text | Medium: accepted and disclosed, not removable | AI governance |
| R05 | Upstream source outage or breaking change | Medium | Medium | Graceful degradation per source, bronze retains raw payloads, critical DQ gate stops bad promotions, alerting on cycle failures | Low | Platform |
| R06 | Autonomous publication of a wrong or harmful brief | Low | High | Four-eyes approval in staging/prod, path-traversal guard on publication, full audit, rollback playbook | Low | AI governance |
| R07 | Credential compromise (LLM, sources, cloud) | Low | High | Workload identity, secrets as files, hashed API keys, SSRF allowlist, secret scanning in CI and in model output | Low | Security |
| R08 | Supply-chain compromise of image or dependencies | Low | High | Pinned actions, frozen lockfile, SBOM, cosign signing, provenance attestation, Kyverno admission, Trivy before signing | Low | Security |
| R09 | Cost blow-up or denial of wallet | Medium | Medium | Token and tool budgets per run, rate limiting, request-size limits, cycle timeout, HPA bounds | Low | Platform |
| R10 | PII entering logs, storage or the model | Medium | High | Input and output PII redaction, hashed queries/answers in the audit log, redaction in structured logs, no personal data ingested from sources | Low | AI governance |
| R11 | Tampering with audit evidence | Low | High | HMAC hash chain verified in CI, in-cluster and via the auditor endpoint; logs shipped to immutable storage | Low | Security |
| R12 | Policy drift between what was reviewed and what runs | Low | Medium | Packaged policy plus chart copy compared in the pre-deploy gate; ConfigMap checksum annotation forces pod restart on change | Low | Platform |
