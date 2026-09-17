"""Upstream connectors.

Source strategy (verified September 2026):

* **ListenBrainz** (MetaBrainz, open source, open data) - primary source for
  fresh releases and *sitewide* monthly charts (``range=month``).
* **Deezer public API** - no-auth global chart, chart playlists and editorial releases.
* **Spotify Web API** - optional enrichment only. Since the February 2026 Development
  Mode changes, browse/new-releases, artist top-tracks and batch endpoints are not
  available to new dev-mode apps, search is capped at 10 results and the app owner
  needs Premium. Spotify-owned editorial playlists (e.g. "Top 50 - Global") have not
  been readable by new apps since November 2024. We therefore only use ``/v1/search``.
"""

from __future__ import annotations

import base64
import time
from datetime import UTC, date, datetime
from typing import Any, Literal

import httpx

from agent_core.http import RateLimiter, build_http_client, request_json
from music_agent.config import MusicSettings
from music_agent.models import Artist, Playlist, Provenance, Release, Track

StatsRange = Literal["week", "month", "quarter", "year", "all_time", "this_month"]


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=UTC) if value else None


class ListenBrainzClient:
    BASE = "https://api.listenbrainz.org"

    def __init__(
        self, settings: MusicSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        headers = {}
        if settings.listenbrainz_token:
            headers["Authorization"] = f"Token {settings.listenbrainz_token.get_secret_value()}"
        self._http = build_http_client(
            allowed_hosts=["api.listenbrainz.org"],
            user_agent=settings.user_agent,
            headers=headers,
            transport=transport,
        )
        self._limiter = RateLimiter(
            rate=settings.listenbrainz_rps * settings.rate_limit_factor, per=1.0
        )

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        return await request_json(
            self._http, "GET", f"{self.BASE}{path}", params=params, limiter=self._limiter
        )

    async def fresh_releases(self, days: int, *, future: bool = False) -> list[Release]:
        endpoint = "/1/explore/fresh-releases/"
        data = await self._get(
            endpoint,
            {
                "days": min(days, 90),
                "past": "true",
                "future": str(future).lower(),
                "sort": "release_date",
            },
        )
        items = data.get("payload", {}).get("releases", []) if isinstance(data, dict) else data
        prov = Provenance(source="listenbrainz", endpoint=endpoint)
        return [
            Release(
                title=str(r.get("release_name", "")),
                artist=str(r.get("artist_credit_name", "")),
                release_date=_parse_date(r.get("release_date")),
                release_type=r.get("release_group_primary_type"),
                mbid=r.get("release_mbid"),
                url=f"https://musicbrainz.org/release/{r['release_mbid']}"
                if r.get("release_mbid")
                else None,
                provenance=prov,
            )
            for r in items or []
        ]

    async def sitewide_recordings(
        self, stats_range: StatsRange = "month", count: int = 50
    ) -> tuple[list[Track], datetime | None, datetime | None]:
        endpoint = "/1/stats/sitewide/recordings"
        data = await self._get(endpoint, {"range": stats_range, "count": count})
        payload = (data or {}).get("payload", {})
        prov = Provenance(
            source="listenbrainz", endpoint=endpoint, note=f"sitewide range={stats_range}"
        )
        tracks = [
            Track(
                title=str(r.get("track_name", "")),
                artist=str(r.get("artist_name", "")),
                album=r.get("release_name"),
                rank=i,
                listen_count=r.get("listen_count"),
                mbid=r.get("recording_mbid"),
                url=f"https://musicbrainz.org/recording/{r['recording_mbid']}"
                if r.get("recording_mbid")
                else None,
                provenance=prov,
            )
            for i, r in enumerate(payload.get("recordings", []), start=1)
        ]
        return tracks, _ts(payload.get("from_ts")), _ts(payload.get("to_ts"))

    async def sitewide_artists(
        self, stats_range: StatsRange = "month", count: int = 100, offset: int = 0
    ) -> list[Artist]:
        endpoint = "/1/stats/sitewide/artists"
        data = await self._get(endpoint, {"range": stats_range, "count": count, "offset": offset})
        prov = Provenance(
            source="listenbrainz", endpoint=endpoint, note=f"sitewide range={stats_range}"
        )
        return [
            Artist(
                name=str(a.get("artist_name", "")),
                mbid=a.get("artist_mbid"),
                rank=offset + i,
                listen_count=a.get("listen_count"),
                provenance=prov,
            )
            for i, a in enumerate(((data or {}).get("payload", {})).get("artists", []), start=1)
        ]

    async def aclose(self) -> None:
        await self._http.aclose()


class DeezerClient:
    BASE = "https://api.deezer.com"

    def __init__(
        self, settings: MusicSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = build_http_client(
            allowed_hosts=["api.deezer.com"], user_agent=settings.user_agent, transport=transport
        )
        self._limiter = RateLimiter(rate=settings.deezer_rps * settings.rate_limit_factor, per=1.0)

    async def _data(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        data = await request_json(
            self._http, "GET", f"{self.BASE}{path}", params=params, limiter=self._limiter
        )
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"deezer error: {data['error']}")
        return list((data or {}).get("data", []))

    async def chart_tracks(self, limit: int = 50) -> list[Track]:
        endpoint = "/chart/0/tracks"
        prov = Provenance(source="deezer", endpoint=endpoint, note="current global chart")
        return [
            Track(
                title=str(t.get("title", "")),
                artist=str((t.get("artist") or {}).get("name", "")),
                album=(t.get("album") or {}).get("title"),
                rank=int(t.get("position") or i),
                external_id=str(t.get("id")),
                url=t.get("link"),
                provenance=prov,
            )
            for i, t in enumerate(await self._data(endpoint, {"limit": limit}), start=1)
        ]

    async def chart_artists(self, limit: int = 50) -> list[Artist]:
        endpoint = "/chart/0/artists"
        prov = Provenance(source="deezer", endpoint=endpoint)
        return [
            Artist(
                name=str(a.get("name", "")),
                rank=int(a.get("position") or i),
                fans=a.get("nb_fan"),
                url=a.get("link"),
                provenance=prov,
            )
            for i, a in enumerate(await self._data(endpoint, {"limit": limit}), start=1)
        ]

    def _playlist(self, p: dict[str, Any], rank: int, prov: Provenance) -> Playlist:
        return Playlist(
            title=str(p.get("title", "")),
            external_id=str(p.get("id")),
            curator=(p.get("user") or {}).get("name"),
            track_count=p.get("nb_tracks"),
            fans=p.get("fans"),
            rank=rank,
            url=p.get("link"),
            provenance=prov,
        )

    async def chart_playlists(self, limit: int = 20) -> list[Playlist]:
        endpoint = "/chart/0/playlists"
        prov = Provenance(source="deezer", endpoint=endpoint)
        items = await self._data(endpoint, {"limit": limit})
        return [self._playlist(p, i, prov) for i, p in enumerate(items, start=1)]

    async def search_playlists(self, query: str, limit: int = 20) -> list[Playlist]:
        endpoint = "/search/playlist"
        prov = Provenance(source="deezer", endpoint=endpoint, note=f"q={query}")
        items = await self._data(endpoint, {"q": query, "limit": limit})
        return [self._playlist(p, i, prov) for i, p in enumerate(items, start=1)]

    async def editorial_releases(self, limit: int = 25) -> list[Release]:
        endpoint = "/editorial/0/releases"
        prov = Provenance(source="deezer", endpoint=endpoint)
        return [
            Release(
                title=str(r.get("title", "")),
                artist=str((r.get("artist") or {}).get("name", "")),
                release_date=_parse_date(r.get("release_date")),
                release_type=r.get("record_type"),
                external_id=str(r.get("id")),
                url=r.get("link"),
                provenance=prov,
            )
            for r in await self._data(endpoint, {"limit": limit})
        ]

    async def aclose(self) -> None:
        await self._http.aclose()


class SpotifyClient:
    """Client-credentials search only (see module docstring for API restrictions)."""

    TOKEN_URL = "https://accounts.spotify.com/api/token"  # noqa: S105 - URL
    API = "https://api.spotify.com/v1"
    MAX_SEARCH_LIMIT = 10

    def __init__(
        self, settings: MusicSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not (settings.spotify_client_id and settings.spotify_client_secret):
            raise ValueError("Spotify credentials are not configured")
        self._id = settings.spotify_client_id
        self._secret = settings.spotify_client_secret
        self._http = build_http_client(
            allowed_hosts=["accounts.spotify.com", "api.spotify.com"],
            user_agent=settings.user_agent,
            transport=transport,
        )
        self._limiter = RateLimiter(rate=5, per=1.0)
        self._token: str | None = None
        self._expiry = 0.0

    async def _headers(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._expiry:
            raw = f"{self._id}:{self._secret.get_secret_value()}"
            data = await request_json(
                self._http,
                "POST",
                self.TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {base64.b64encode(raw.encode()).decode()}"},
                limiter=self._limiter,
            )
            self._token = str(data["access_token"])
            self._expiry = time.monotonic() + float(data.get("expires_in", 3600)) - 60
        return {"Authorization": f"Bearer {self._token}"}

    async def search(
        self, query: str, kind: Literal["track", "playlist"], limit: int = 10
    ) -> list[Track] | list[Playlist]:
        endpoint = "/v1/search"
        data = await request_json(
            self._http,
            "GET",
            f"{self.API}/search",
            params={"q": query, "type": kind, "limit": min(limit, self.MAX_SEARCH_LIMIT)},
            headers=await self._headers(),
            limiter=self._limiter,
        )
        prov = Provenance(source="spotify", endpoint=endpoint, note=f"q={query}")
        items = [i for i in (data.get(f"{kind}s") or {}).get("items", []) if i]
        if kind == "track":
            return [
                Track(
                    title=str(t.get("name", "")),
                    artist=", ".join(a.get("name", "") for a in t.get("artists", [])),
                    album=(t.get("album") or {}).get("name"),
                    rank=i,
                    external_id=t.get("id"),
                    url=(t.get("external_urls") or {}).get("spotify"),
                    provenance=prov,
                )
                for i, t in enumerate(items, start=1)
            ]
        return [
            Playlist(
                title=str(p.get("name", "")),
                external_id=str(p.get("id")),
                curator=(p.get("owner") or {}).get("display_name"),
                track_count=((p.get("tracks") or p.get("items") or {}) or {}).get("total"),
                rank=i,
                url=(p.get("external_urls") or {}).get("spotify"),
                provenance=prov,
            )
            for i, p in enumerate(items, start=1)
        ]

    async def aclose(self) -> None:
        await self._http.aclose()
