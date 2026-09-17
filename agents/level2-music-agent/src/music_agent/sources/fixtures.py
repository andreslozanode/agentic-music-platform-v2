"""Offline fixture transport.

Serves synthetic, API-shaped payloads through ``httpx.MockTransport`` so the real
parsing/aggregation code paths run in CI and demos without network access.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib import resources
from typing import Any

import httpx

_ROUTES = {
    ("api.listenbrainz.org", "/1/explore/fresh-releases/"): "lb_fresh_releases",
    ("api.listenbrainz.org", "/1/stats/sitewide/recordings"): "lb_sitewide_recordings",
    ("api.deezer.com", "/chart/0/tracks"): "dz_chart_tracks",
    ("api.deezer.com", "/chart/0/artists"): "dz_chart_artists",
    ("api.deezer.com", "/chart/0/playlists"): "dz_chart_playlists",
    ("api.deezer.com", "/search/playlist"): "dz_chart_playlists",
    ("api.deezer.com", "/editorial/0/releases"): "dz_editorial_releases",
}


@cache
def load_fixtures() -> dict[str, Any]:
    raw = resources.files("music_agent.fixtures").joinpath("music.json").read_text("utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def _resolve_dates(value: Any) -> Any:
    """Replace ``"@-N"`` placeholders with the ISO date N days ago (keeps fixtures fresh)."""
    if isinstance(value, dict):
        return {k: _resolve_dates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_dates(v) for v in value]
    if isinstance(value, str) and value.startswith("@-") and value[2:].isdigit():
        return (datetime.now(UTC).date() - timedelta(days=int(value[2:]))).isoformat()
    return value


def _handler(request: httpx.Request) -> httpx.Response:
    fx = load_fixtures()
    key = (request.url.host, request.url.path)
    if key == ("api.listenbrainz.org", "/1/stats/sitewide/artists"):
        rng = request.url.params.get("range", "month")
        offset = int(request.url.params.get("offset", "0"))
        count = int(request.url.params.get("count", "100"))
        artists = fx[f"lb_sitewide_artists_{'all_time' if rng == 'all_time' else 'month'}"]
        return httpx.Response(
            200, json={"payload": {"artists": artists[offset : offset + count], "range": rng}}
        )
    name = _ROUTES.get(key)
    if name is None:
        return httpx.Response(404, json={"error": f"no fixture for {key}"})
    body: dict[str, Any] = _resolve_dates(fx[name])
    limit = request.url.params.get("limit") or request.url.params.get("count")
    if limit:
        n = int(limit)
        if "data" in body:
            body["data"] = body["data"][:n]
        payload = body.get("payload", {})
        if "recordings" in payload:
            payload["recordings"] = payload["recordings"][:n]
    return httpx.Response(200, json=body)


def fixture_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_handler)
