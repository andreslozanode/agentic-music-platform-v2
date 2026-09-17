# ADR 0002 - Delta Lake for the medallion layers, Qdrant for vectors

- Status: accepted
- Date: 2026-09

## Context

The agent needs (a) replayable, contract-checked tabular history and (b) semantic retrieval over
curated insights, on any of three clouds or on a laptop.

## Decision

- **Delta Lake via `deltalake` (Rust, no JVM)**: ACID commits, partition-level `replace-where`
  overwrites for idempotent re-runs, schema evolution on append, time travel for data rollback,
  and the same code path for local paths, `s3://`, `gs://` and `abfss://`.
- **Qdrant** for vectors, with three backends behind one setting: in-memory (tests), embedded
  on-disk (dev), server or Qdrant Cloud (staging/prod). Payload filtering enforces the document
  classification allowed by policy.
- Embeddings are pluggable; production requires a semantic provider, and the hashing embedder is
  only for offline runs.

## Alternatives considered

- **Iceberg + a catalog**: excellent for multi-engine analytics, but adds a catalog service to
  operate for a workload that is a single writer with small daily volumes.
- **pgvector**: one less system when Postgres already exists, but no first-class payload filtering
  or hybrid search, and it would still need a relational schema for the medallion layers.
- **Parquet without a table format**: no atomic partition replacement, no time travel, and
  re-running a day becomes a manual delete-and-write.

## Consequences

- No JVM or Spark in the image; the whole pipeline is a Python process and fits a CronJob.
- Delta's time travel is the documented data-rollback mechanism in the rollback playbook.
- Very large volumes would eventually need a real compute engine; the contracts make that swap
  mechanical.
