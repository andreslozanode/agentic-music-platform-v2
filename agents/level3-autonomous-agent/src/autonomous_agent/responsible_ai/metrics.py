"""Popularity-bias and diversity metrics for charts surfaced to users."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel

from music_agent.models import TopChart, normalise_key


class ChartFairnessReport(BaseModel):
    n_tracks: int
    unique_artists: int
    unique_artist_ratio: float
    hhi: float
    top_artist: str | None
    top_artist_share: float
    gini_listens: float | None
    cross_source_overlap: float | None
    flags: list[str]
    disclosure: str


def gini(values: Sequence[float]) -> float:
    xs = sorted(v for v in values if v >= 0)
    n = len(xs)
    if n == 0 or sum(xs) == 0:
        return 0.0
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    return round((2 * cum) / (n * sum(xs)) - (n + 1) / n, 4)


def assess_chart(
    chart: TopChart,
    *,
    max_hhi: float,
    max_top_artist_share: float,
    min_cross_source_overlap: float,
    disclosure: str,
) -> ChartFairnessReport:
    n = len(chart.tracks)
    counts = Counter(normalise_key(t.artist) for t in chart.tracks)
    names = {normalise_key(t.artist): t.artist for t in chart.tracks}
    shares = {k: v / n for k, v in counts.items()} if n else {}
    hhi = round(sum(s * s for s in shares.values()), 4)
    top_key, top_share = max(shares.items(), key=lambda kv: kv[1]) if shares else (None, 0.0)
    listens = [float(t.listen_count) for t in chart.tracks if t.listen_count is not None]
    flags: list[str] = []
    if hhi > max_hhi:
        flags.append(f"high_artist_concentration(hhi={hhi})")
    if top_share > max_top_artist_share:
        flags.append(f"dominant_artist({names.get(top_key or '', '')}={top_share:.0%})")
    if chart.cross_source_overlap is not None and (
        chart.cross_source_overlap < min_cross_source_overlap
    ):
        flags.append(f"low_cross_source_agreement({chart.cross_source_overlap})")
    return ChartFairnessReport(
        n_tracks=n,
        unique_artists=len(counts),
        unique_artist_ratio=round(len(counts) / n, 4) if n else 0.0,
        hhi=hhi,
        top_artist=names.get(top_key) if top_key else None,
        top_artist_share=round(top_share, 4),
        gini_listens=gini(listens) if listens else None,
        cross_source_overlap=chart.cross_source_overlap,
        flags=flags,
        disclosure=disclosure,
    )
