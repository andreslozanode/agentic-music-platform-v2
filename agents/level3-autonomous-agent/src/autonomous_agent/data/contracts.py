"""Data contracts: the Arrow schemas are the published interface of each table."""

from __future__ import annotations

import pyarrow as pa

TS = pa.timestamp("us", tz="UTC")

BRONZE_SNAPSHOTS = pa.schema(
    [
        ("run_id", pa.string()),
        ("ingest_date", pa.string()),
        ("ingested_at", TS),
        ("section", pa.string()),
        ("record_count", pa.int64()),
        ("payload_json", pa.large_string()),
        ("payload_sha256", pa.string()),
        ("source_errors", pa.string()),
    ]
)

SILVER_TOP_TRACKS = pa.schema(
    [
        ("snapshot_date", pa.string()),
        ("rank", pa.int32()),
        ("track_key", pa.string()),
        ("title", pa.string()),
        ("artist", pa.string()),
        ("album", pa.string()),
        ("listen_count", pa.int64()),
        ("mbid", pa.string()),
        ("source", pa.string()),
        ("period_start", TS),
        ("period_end", TS),
        ("run_id", pa.string()),
    ]
)

SILVER_RELEASES = pa.schema(
    [
        ("snapshot_date", pa.string()),
        ("release_key", pa.string()),
        ("title", pa.string()),
        ("artist", pa.string()),
        ("release_date", pa.date32()),
        ("release_type", pa.string()),
        ("source", pa.string()),
        ("url", pa.string()),
        ("run_id", pa.string()),
    ]
)

SILVER_NEW_ARTISTS = pa.schema(
    [
        ("snapshot_date", pa.string()),
        ("artist_key", pa.string()),
        ("name", pa.string()),
        ("signal", pa.string()),
        ("evidence", pa.string()),
        ("monthly_rank", pa.int32()),
        ("listen_count", pa.int64()),
        ("run_id", pa.string()),
    ]
)

SILVER_PLAYLISTS = pa.schema(
    [
        ("snapshot_date", pa.string()),
        ("playlist_id", pa.string()),
        ("title", pa.string()),
        ("curator", pa.string()),
        ("track_count", pa.int64()),
        ("fans", pa.int64()),
        ("rank", pa.int32()),
        ("source", pa.string()),
        ("url", pa.string()),
        ("run_id", pa.string()),
    ]
)

GOLD_CHART_METRICS = pa.schema(
    [
        ("snapshot_date", pa.string()),
        ("n_tracks", pa.int32()),
        ("unique_artists", pa.int32()),
        ("unique_artist_ratio", pa.float64()),
        ("hhi", pa.float64()),
        ("top_artist", pa.string()),
        ("top_artist_share", pa.float64()),
        ("gini_listens", pa.float64()),
        ("cross_source_overlap", pa.float64()),
        ("flags", pa.string()),
        ("run_id", pa.string()),
    ]
)

GOLD_DOCUMENTS = pa.schema(
    [
        ("snapshot_date", pa.string()),
        ("doc_id", pa.string()),
        ("doc_type", pa.string()),
        ("title", pa.string()),
        ("text", pa.large_string()),
        ("source", pa.string()),
        ("classification", pa.string()),
        ("content_sha256", pa.string()),
        ("run_id", pa.string()),
    ]
)

CONTRACTS: dict[str, pa.Schema] = {
    "bronze.music_snapshots": BRONZE_SNAPSHOTS,
    "silver.top_tracks_monthly": SILVER_TOP_TRACKS,
    "silver.releases": SILVER_RELEASES,
    "silver.new_artists": SILVER_NEW_ARTISTS,
    "silver.playlists": SILVER_PLAYLISTS,
    "gold.chart_metrics": GOLD_CHART_METRICS,
    "gold.insight_documents": GOLD_DOCUMENTS,
}
