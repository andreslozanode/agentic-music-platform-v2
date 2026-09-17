"""Bronze -> Silver -> Gold pipeline with DQ gates and OpenLineage-style lineage events."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa

from autonomous_agent.data import quality as dq
from autonomous_agent.data.contracts import (
    BRONZE_SNAPSHOTS,
    GOLD_CHART_METRICS,
    GOLD_DOCUMENTS,
    SILVER_NEW_ARTISTS,
    SILVER_PLAYLISTS,
    SILVER_RELEASES,
    SILVER_TOP_TRACKS,
)
from autonomous_agent.data.storage import LakehouseStorage, Layer
from autonomous_agent.governance.audit import AuditLog
from autonomous_agent.responsible_ai.metrics import ChartFairnessReport, assess_chart
from music_agent.models import MusicSnapshot, normalise_key

NAMESPACE = "agentic-ai-platform"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class InsightDocument:
    doc_id: str
    doc_type: str
    title: str
    text: str
    source: str
    classification: str = "public"

    @property
    def content_sha256(self) -> str:
        return sha256(f"{self.title}\n{self.text}")


@dataclass
class PipelineResult:
    run_id: str
    snapshot_date: str
    rows: dict[str, int] = field(default_factory=dict)
    quality: list[dq.QualityReport] = field(default_factory=list)
    fairness: ChartFairnessReport | None = None
    documents: list[InsightDocument] = field(default_factory=list)
    lineage: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class FairnessThresholds:
    max_hhi: float
    max_top_artist_share: float
    min_cross_source_overlap: float
    disclosure: str


class MedallionPipeline:
    def __init__(
        self,
        storage: LakehouseStorage,
        audit: AuditLog,
        lineage_path: Path,
        thresholds: FairnessThresholds,
    ) -> None:
        self.storage = storage
        self.audit = audit
        self.lineage_path = lineage_path
        self.thresholds = thresholds
        lineage_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers
    def _lineage(
        self, result: PipelineResult, job: str, inputs: list[str], outputs: list[str]
    ) -> None:
        event = {
            "eventType": "COMPLETE",
            "eventTime": datetime.now(UTC).isoformat(),
            "producer": f"https://github.com/{NAMESPACE}/autonomous-agent",
            "run": {"runId": result.run_id},
            "job": {"namespace": NAMESPACE, "name": job},
            "inputs": [{"namespace": NAMESPACE, "name": n} for n in inputs],
            "outputs": [{"namespace": NAMESPACE, "name": n} for n in outputs],
        }
        result.lineage.append(event)
        with self.lineage_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def _gate(
        self,
        result: PipelineResult,
        name: str,
        table: pa.Table,
        checks: list[dq.Check],
    ) -> None:
        report = dq.run_checks(name, table, checks)
        result.quality.append(report)
        self.audit.record(
            "data_quality",
            "pipeline",
            {
                "run_id": result.run_id,
                "table": name,
                "passed": report.passed,
                "failures": [f"{f.severity}:{f.name}:{f.detail}" for f in report.failures],
            },
        )
        if not report.passed:
            raise dq.DataQualityError(report)

    def _write(
        self, result: PipelineResult, layer: Layer, name: str, table: pa.Table, **kw: Any
    ) -> None:
        version = self.storage.write(layer, name, table, **kw)
        result.rows[f"{layer}.{name}"] = table.num_rows
        self.audit.record(
            "table_write",
            "pipeline",
            {
                "run_id": result.run_id,
                "table": f"{layer}.{name}",
                "rows": table.num_rows,
                "delta_version": version,
            },
        )

    # ------------------------------------------------------------------- bronze
    def bronze(self, snap: MusicSnapshot, result: PipelineResult) -> None:
        now = datetime.now(UTC)
        rows = []
        sections: dict[str, Any] = {
            "latest_releases": [r.model_dump(mode="json") for r in snap.latest_releases],
            "new_artists": [a.model_dump(mode="json") for a in snap.new_artists],
            "playlists": [p.model_dump(mode="json") for p in snap.playlists],
            "top_global": snap.top_global.model_dump(mode="json") if snap.top_global else None,
        }
        for section, payload in sections.items():
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            count = (
                len(payload)
                if isinstance(payload, list)
                else (len(payload["tracks"]) if payload else 0)
            )
            rows.append(
                {
                    "run_id": result.run_id,
                    "ingest_date": result.snapshot_date,
                    "ingested_at": now,
                    "section": section,
                    "record_count": count,
                    "payload_json": text,
                    "payload_sha256": sha256(text),
                    "source_errors": snap.errors.get(section, ""),
                }
            )
        table = pa.Table.from_pylist(rows, schema=BRONZE_SNAPSHOTS)
        self._write(result, "bronze", "music_snapshots", table, partition_by=["ingest_date"])
        self._lineage(
            result,
            "bronze.ingest_music_snapshot",
            ["listenbrainz.api", "deezer.api", "spotify.api"],
            ["bronze.music_snapshots"],
        )

    # ------------------------------------------------------------------- silver
    def silver(self, snap: MusicSnapshot, result: PipelineResult) -> None:
        d, rid = result.snapshot_date, result.run_id
        replace = f"snapshot_date = '{d}'"
        write_kw: dict[str, Any] = {
            "mode": "overwrite",
            "partition_by": ["snapshot_date"],
            "replace_where": replace,
        }

        if snap.top_global is None:
            raise dq.DataQualityError(
                dq.QualityReport(
                    "silver.top_tracks_monthly",
                    0,
                    (
                        dq.CheckResult(
                            "source_available", "critical", False, snap.errors.get("top_global", "")
                        ),
                    ),
                )
            )
        chart = snap.top_global
        top = pa.Table.from_pylist(
            [
                {
                    "snapshot_date": d,
                    "rank": t.rank,
                    "track_key": t.key,
                    "title": t.title,
                    "artist": t.artist,
                    "album": t.album,
                    "listen_count": t.listen_count,
                    "mbid": t.mbid,
                    "source": t.provenance.source,
                    "period_start": chart.period_start,
                    "period_end": chart.period_end,
                    "run_id": rid,
                }
                for t in chart.tracks
            ],
            schema=SILVER_TOP_TRACKS,
        )
        self._gate(
            result,
            "silver.top_tracks_monthly",
            top,
            [
                dq.schema_matches(SILVER_TOP_TRACKS),
                dq.min_rows(10),
                dq.not_null("rank", "title", "artist"),
                dq.unique("rank"),
                dq.in_range("rank", 1, 100),
                dq.in_range("listen_count", 0, 10**10, severity="warning"),
                dq.fresh("period_end", timedelta(days=45)),
            ],
        )
        self._write(result, "silver", "top_tracks_monthly", top, **write_kw)

        dedup: dict[str, Any] = {}
        for r in snap.latest_releases:
            dedup.setdefault(r.key, r)
        releases = pa.Table.from_pylist(
            [
                {
                    "snapshot_date": d,
                    "release_key": k,
                    "title": r.title,
                    "artist": r.artist,
                    "release_date": r.release_date,
                    "release_type": (r.release_type or "").lower(),
                    "source": r.provenance.source,
                    "url": r.url,
                    "run_id": rid,
                }
                for k, r in dedup.items()
            ],
            schema=SILVER_RELEASES,
        )
        self._gate(
            result,
            "silver.releases",
            releases,
            [
                dq.schema_matches(SILVER_RELEASES),
                dq.not_null("title", "artist"),
                dq.unique("release_key"),
                dq.accepted_values("source", {"listenbrainz", "deezer", "spotify"}),
                dq.min_rows(1, severity="warning"),
            ],
        )
        self._write(result, "silver", "releases", releases, **write_kw)

        artists = pa.Table.from_pylist(
            [
                {
                    "snapshot_date": d,
                    "artist_key": normalise_key(a.name),
                    "name": a.name,
                    "signal": a.signal,
                    "evidence": "; ".join(a.evidence),
                    "monthly_rank": a.rank,
                    "listen_count": a.listen_count,
                    "run_id": rid,
                }
                for a in snap.new_artists
            ],
            schema=SILVER_NEW_ARTISTS,
        )
        self._gate(
            result,
            "silver.new_artists",
            artists,
            [
                dq.not_null("name", "signal"),
                dq.unique("artist_key"),
            ],
        )
        self._write(result, "silver", "new_artists", artists, **write_kw)

        playlists = pa.Table.from_pylist(
            [
                {
                    "snapshot_date": d,
                    "playlist_id": f"{p.provenance.source}:{p.external_id}",
                    "title": p.title,
                    "curator": p.curator,
                    "track_count": p.track_count,
                    "fans": p.fans,
                    "rank": p.rank,
                    "source": p.provenance.source,
                    "url": p.url,
                    "run_id": rid,
                }
                for p in snap.playlists
            ],
            schema=SILVER_PLAYLISTS,
        )
        self._gate(
            result,
            "silver.playlists",
            playlists,
            [
                dq.not_null("title", "playlist_id"),
                dq.unique("playlist_id"),
            ],
        )
        self._write(result, "silver", "playlists", playlists, **write_kw)
        self._lineage(
            result,
            "silver.conform_music",
            ["bronze.music_snapshots"],
            [
                "silver.top_tracks_monthly",
                "silver.releases",
                "silver.new_artists",
                "silver.playlists",
            ],
        )

    # --------------------------------------------------------------------- gold
    def gold(self, snap: MusicSnapshot, result: PipelineResult) -> None:
        d, rid = result.snapshot_date, result.run_id
        write_kw: dict[str, Any] = {
            "mode": "overwrite",
            "partition_by": ["snapshot_date"],
            "replace_where": f"snapshot_date = '{d}'",
        }
        if snap.top_global is None:  # pragma: no cover - silver gate guarantees presence
            raise RuntimeError("gold requires a chart")
        t = self.thresholds
        fairness = assess_chart(
            snap.top_global,
            max_hhi=t.max_hhi,
            max_top_artist_share=t.max_top_artist_share,
            min_cross_source_overlap=t.min_cross_source_overlap,
            disclosure=t.disclosure,
        )
        result.fairness = fairness
        metrics = pa.Table.from_pylist(
            [
                {
                    **fairness.model_dump(exclude={"disclosure", "flags"}),
                    "flags": ";".join(fairness.flags),
                    "snapshot_date": d,
                    "run_id": rid,
                }
            ],
            schema=GOLD_CHART_METRICS,
        )
        self._write(result, "gold", "chart_metrics", metrics, **write_kw)
        if fairness.flags:
            self.audit.record(
                "responsible_ai_flag", "pipeline", {"run_id": rid, "flags": fairness.flags}
            )

        docs = self.build_documents(snap, fairness, d)
        result.documents = docs
        table = pa.Table.from_pylist(
            [
                {
                    "snapshot_date": d,
                    "doc_id": doc.doc_id,
                    "doc_type": doc.doc_type,
                    "title": doc.title,
                    "text": doc.text,
                    "source": doc.source,
                    "classification": doc.classification,
                    "content_sha256": doc.content_sha256,
                    "run_id": rid,
                }
                for doc in docs
            ],
            schema=GOLD_DOCUMENTS,
        )
        self._gate(
            result,
            "gold.insight_documents",
            table,
            [
                dq.unique("doc_id"),
                dq.not_null("text"),
                dq.accepted_values("classification", {"public", "internal"}),
            ],
        )
        self._write(result, "gold", "insight_documents", table, **write_kw)
        self._lineage(
            result,
            "gold.build_insights",
            [
                "silver.top_tracks_monthly",
                "silver.releases",
                "silver.new_artists",
                "silver.playlists",
            ],
            ["gold.chart_metrics", "gold.insight_documents"],
        )

    @staticmethod
    def build_documents(
        snap: MusicSnapshot, fairness: ChartFairnessReport, d: str
    ) -> list[InsightDocument]:
        docs: list[InsightDocument] = []
        chart = snap.top_global
        if chart:
            lines = [
                f"{t.rank}. {t.title} - {t.artist} ({t.listen_count or 'n/a'} listens)"
                for t in chart.tracks
            ]
            period = (
                f"{chart.period_start:%Y-%m-%d} to {chart.period_end:%Y-%m-%d}"
                if (chart.period_start and chart.period_end)
                else "last month"
            )
            docs.append(
                InsightDocument(
                    f"chart:{d}",
                    "chart",
                    f"{chart.label} ({period})",
                    "Global top tracks by ListenBrainz sitewide listens, "
                    + period
                    + ".\n"
                    + "\n".join(lines),
                    "listenbrainz",
                )
            )
            docs.append(
                InsightDocument(
                    f"fairness:{d}",
                    "fairness",
                    f"Chart diversity report {d}",
                    f"Unique artists: {fairness.unique_artists}/{fairness.n_tracks}. "
                    f"Artist concentration HHI={fairness.hhi}. Top artist: {fairness.top_artist} "
                    f"with {fairness.top_artist_share:.0%} of positions. Agreement with Deezer "
                    f"current chart (Jaccard)={fairness.cross_source_overlap}. "
                    f"Flags: {', '.join(fairness.flags) or 'none'}.",
                    "derived",
                )
            )
            by_artist: dict[str, list[str]] = {}
            for tr in chart.tracks:
                by_artist.setdefault(tr.artist, []).append(f"#{tr.rank} {tr.title}")
            for artist, entries in by_artist.items():
                docs.append(
                    InsightDocument(
                        f"artist:{d}:{normalise_key(artist)}",
                        "artist_profile",
                        f"{artist} - chart presence {d}",
                        f"{artist} has {len(entries)} track(s) in the monthly global chart: "
                        + ", ".join(entries)
                        + ".",
                        "listenbrainz",
                    )
                )
        if snap.latest_releases:
            docs.append(
                InsightDocument(
                    f"releases:{d}",
                    "releases",
                    f"Latest releases as of {d}",
                    "\n".join(
                        f"{r.release_date or 'n/a'}: {r.title} by {r.artist} "
                        f"[{r.release_type or 'unknown'}] via {r.provenance.source}"
                        for r in snap.latest_releases
                    ),
                    "listenbrainz+deezer",
                )
            )
        if snap.new_artists:
            docs.append(
                InsightDocument(
                    f"new_artists:{d}",
                    "new_artists",
                    f"New and emerging artists {d}",
                    "Heuristic signal (not in all-time top artists):\n"
                    + "\n".join(
                        f"{a.name}: {a.signal}; {', '.join(a.evidence[:3])}"
                        for a in snap.new_artists
                    ),
                    "listenbrainz",
                )
            )
        if snap.playlists:
            docs.append(
                InsightDocument(
                    f"playlists:{d}",
                    "playlists",
                    f"Popular playlists {d}",
                    "\n".join(
                        f"{p.rank}. {p.title} by {p.curator or 'unknown'} "
                        f"({p.track_count or '?'} tracks, {p.fans or '?'} fans)"
                        for p in snap.playlists
                    ),
                    "deezer",
                )
            )
        return docs

    # ---------------------------------------------------------------------- run
    def run(self, snap: MusicSnapshot, run_id: str | None = None) -> PipelineResult:
        result = PipelineResult(
            run_id=run_id or uuid.uuid4().hex,
            snapshot_date=snap.generated_at.date().isoformat(),
        )
        self.audit.record("pipeline_started", "pipeline", {"run_id": result.run_id})
        try:
            self.bronze(snap, result)
            self.silver(snap, result)
            self.gold(snap, result)
        except dq.DataQualityError as exc:
            self.audit.record(
                "pipeline_failed", "pipeline", {"run_id": result.run_id, "error": str(exc)}
            )
            raise
        self.audit.record(
            "pipeline_completed", "pipeline", {"run_id": result.run_id, "rows": result.rows}
        )
        return result
