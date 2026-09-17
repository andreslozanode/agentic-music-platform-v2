"""Music intelligence service: cross-source aggregation with graceful degradation."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from agent_core.observability import AsyncTTLCache
from music_agent.config import MusicSettings
from music_agent.models import (
    Artist,
    MusicSnapshot,
    Playlist,
    Provenance,
    Release,
    TopChart,
    Track,
    normalise_key,
)
from music_agent.sources.clients import DeezerClient, ListenBrainzClient, SpotifyClient
from music_agent.sources.fixtures import fixture_transport


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


class MusicIntelligenceService:
    def __init__(
        self,
        settings: MusicSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or MusicSettings()
        if transport is None and self.settings.mode == "fixtures":
            transport = fixture_transport()
        self.listenbrainz = ListenBrainzClient(self.settings, transport)
        self.deezer = DeezerClient(self.settings, transport)
        self.spotify = (
            SpotifyClient(self.settings, transport) if self.settings.spotify_enabled else None
        )
        self._cache: AsyncTTLCache[Any] = AsyncTTLCache(ttl_s=self.settings.cache_ttl_s)

    # ------------------------------------------------------------------ releases
    async def latest_releases(
        self, days: int = 14, limit: int = 25, release_type: str | None = None
    ) -> list[Release]:
        async def load() -> list[Release]:
            lb, dz = await asyncio.gather(
                self.listenbrainz.fresh_releases(days),
                self.deezer.editorial_releases(limit=limit),
                return_exceptions=True,
            )
            merged: dict[str, Release] = {}
            for batch in (lb, dz):
                if isinstance(batch, BaseException):
                    continue
                for rel in batch:
                    merged.setdefault(rel.key, rel)
            return list(merged.values())

        releases: list[Release] = await self._cache.get_or_set(f"releases:{days}:{limit}", load)
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        today = datetime.now(UTC).date()
        out = [
            r
            for r in releases
            if (r.release_date is None or cutoff <= r.release_date <= today)
            and (release_type is None or (r.release_type or "").lower() == release_type.lower())
        ]
        out.sort(key=lambda r: r.release_date or cutoff, reverse=True)
        return out[:limit]

    # ------------------------------------------------------------------ artists
    async def _established_artist_keys(self) -> set[str]:
        async def load() -> set[str]:
            depth = self.settings.known_artists_depth
            pages = await asyncio.gather(
                *(
                    self.listenbrainz.sitewide_artists("all_time", count=100, offset=o)
                    for o in range(0, depth, 100)
                )
            )
            return {a.key for page in pages for a in page}

        keys: set[str] = await self._cache.get_or_set("established", load)
        return keys

    async def new_artists(self, days: int = 30, limit: int = 20) -> list[Artist]:
        """Heuristic: artists with fresh releases or monthly chart presence who are not
        among the all-time top-N sitewide artists."""
        established, releases, monthly = await asyncio.gather(
            self._established_artist_keys(),
            self.listenbrainz.fresh_releases(days),
            self.listenbrainz.sitewide_artists("month", count=100),
        )
        counts: Counter[str] = Counter()
        names: dict[str, str] = {}
        evidence: dict[str, list[str]] = {}
        for rel in releases:
            akey = normalise_key(rel.artist)
            if not akey or akey in established:
                continue
            counts[akey] += 1
            names.setdefault(akey, rel.artist)
            evidence.setdefault(akey, []).append(f"release:{rel.title}")
        monthly_rank = {a.key: a for a in monthly}
        results: list[Artist] = []
        prov = Provenance(
            source="listenbrainz",
            endpoint="derived:new_artists",
            note=f"not in all-time top {self.settings.known_artists_depth}",
        )
        for key, n in counts.most_common():
            chart = monthly_rank.get(key)
            results.append(
                Artist(
                    name=names[key],
                    rank=chart.rank if chart else None,
                    listen_count=chart.listen_count if chart else None,
                    signal="fresh_release" + ("+monthly_chart" if chart else ""),
                    evidence=[*evidence[key][:5], f"fresh_releases={n}"],
                    provenance=prov,
                )
            )
        for key, art in monthly_rank.items():
            if key not in established and key not in counts:
                results.append(
                    art.model_copy(
                        update={
                            "signal": "rising_monthly_chart",
                            "evidence": [f"monthly_rank={art.rank}"],
                            "provenance": prov,
                        }
                    )
                )
        return results[:limit]

    # ------------------------------------------------------------------ playlists
    async def playlists(self, limit: int = 20, query: str | None = None) -> list[Playlist]:
        if query:
            found: list[Playlist] = await self.deezer.search_playlists(query, limit)
            if self.spotify:
                sp = await self.spotify.search(query, "playlist", limit)
                found.extend(p for p in sp if isinstance(p, Playlist))
            return found[: limit * 2]
        cached: list[Playlist] = await self._cache.get_or_set(
            f"playlists:{limit}", lambda: self.deezer.chart_playlists(limit)
        )
        return cached

    # ------------------------------------------------------------------ top 50
    async def top_global(self, limit: int = 50) -> TopChart:
        async def load() -> TopChart:
            (lb_tracks, start, end), dz_tracks = await asyncio.gather(
                self.listenbrainz.sitewide_recordings("month", count=limit),
                self.deezer.chart_tracks(limit),
            )
            return TopChart(
                label=f"Global Top {limit} - last month (ListenBrainz sitewide listens)",
                period_start=start,
                period_end=end,
                tracks=lb_tracks,
                cross_source_overlap=jaccard(
                    {t.key for t in lb_tracks}, {t.key for t in dz_tracks}
                ),
                secondary_source="deezer:/chart/0/tracks (current)",
            )

        chart: TopChart = await self._cache.get_or_set(f"top:{limit}", load)
        return chart

    async def deezer_chart(self, limit: int = 50) -> list[Track]:
        return await self.deezer.chart_tracks(limit)

    async def search_spotify_tracks(self, query: str, limit: int = 10) -> list[Track]:
        if not self.spotify:
            raise RuntimeError("Spotify enrichment is disabled (no credentials configured)")
        return [t for t in await self.spotify.search(query, "track", limit) if isinstance(t, Track)]

    # ------------------------------------------------------------------ snapshot
    async def snapshot(self) -> MusicSnapshot:
        tasks: dict[str, Awaitable[Any]] = {
            "latest_releases": self.latest_releases(),
            "new_artists": self.new_artists(),
            "playlists": self.playlists(),
            "top_global": self.top_global(),
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        snap = MusicSnapshot()
        for name, value in zip(tasks, results, strict=True):
            if isinstance(value, BaseException):
                snap.errors[name] = f"{type(value).__name__}: {value}"[:300]
            else:
                setattr(snap, name, value)
        return snap

    async def aclose(self) -> None:
        await self.listenbrainz.aclose()
        await self.deezer.aclose()
        if self.spotify:
            await self.spotify.aclose()
