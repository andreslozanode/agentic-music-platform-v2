from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from typer.testing import CliRunner

from agent_core import LLMResponse, ToolCall
from agent_core.llm import HeuristicProvider, ScriptedProvider
from music_agent.agent import build_agent
from music_agent.cli import app
from music_agent.config import MusicSettings
from music_agent.mcp_server import build_mcp_server
from music_agent.models import normalise_key
from music_agent.service import MusicIntelligenceService, jaccard
from music_agent.sources.clients import DeezerClient, ListenBrainzClient, SpotifyClient
from music_agent.sources.fixtures import fixture_transport
from music_agent.tools import (
    LatestReleasesArgs,
    MusicTools,
    NewArtistsArgs,
    PlaylistsArgs,
    TopGlobalArgs,
)


def _svc(**kw: object) -> MusicIntelligenceService:
    return MusicIntelligenceService(MusicSettings(mode="fixtures", **kw))  # type: ignore[arg-type]


def test_normalise_key_is_accent_and_feature_insensitive() -> None:
    assert normalise_key("Luz de Neón", "Song (Remix)") == normalise_key("luz de neon", "Song")
    assert normalise_key("Artist", "Track feat. Someone") == "artist track"
    assert jaccard(set(), set()) == 0.0
    assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3, rel=1e-3)


async def test_top_global_last_month_with_cross_source_overlap() -> None:
    svc = _svc()
    chart = await svc.top_global(50)
    assert len(chart.tracks) == 50
    assert chart.tracks[0].rank == 1
    assert chart.tracks[0].provenance.source == "listenbrainz"
    assert "range=month" in (chart.tracks[0].provenance.note or "")
    assert chart.period_start is not None
    assert chart.period_end is not None
    assert chart.period_start < chart.period_end
    assert chart.cross_source_overlap is not None
    assert 0 < chart.cross_source_overlap < 1
    assert await svc.top_global(50) is chart  # cached
    await svc.aclose()


async def test_latest_releases_merge_filter_sort() -> None:
    svc = _svc()
    releases = await svc.latest_releases(days=14, limit=50)
    sources = {r.provenance.source for r in releases}
    assert sources == {"listenbrainz", "deezer"}
    dates = [r.release_date for r in releases if r.release_date]
    assert dates == sorted(dates, reverse=True)
    cutoff = datetime.now(UTC).date() - timedelta(days=14)
    assert all(d >= cutoff for d in dates)
    assert len({r.key for r in releases}) == len(releases)
    singles = await svc.latest_releases(days=14, limit=50, release_type="single")
    assert singles
    assert all((r.release_type or "").lower() == "single" for r in singles)
    narrow = await svc.latest_releases(days=3, limit=50)
    assert len(narrow) < len(releases)
    await svc.aclose()


async def test_new_artists_excludes_established() -> None:
    svc = _svc()
    artists = await svc.new_artists(days=30, limit=50)
    names = {a.name for a in artists}
    assert "Echo Parkway" not in names  # in all-time top
    assert {"Selva Digital", "Brasa Norte"} <= names
    assert any(a.signal == "rising_monthly_chart" for a in artists)
    assert any((a.signal or "").startswith("fresh_release") for a in artists)
    assert all(a.provenance.endpoint == "derived:new_artists" for a in artists)
    await svc.aclose()


async def test_playlists_chart_and_search() -> None:
    svc = _svc()
    chart = await svc.playlists(5)
    assert chart[0].title == "Top Global Synthetic"
    found = await svc.playlists(5, query="latin")
    assert found[0].provenance.endpoint == "/search/playlist"
    await svc.aclose()


async def test_snapshot_degrades_gracefully() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.deezer.com" and "playlists" in request.url.path:
            return httpx.Response(400, json={"error": "boom"})
        return fixture_transport().handler(request)  # type: ignore[attr-defined,no-any-return]

    svc = MusicIntelligenceService(
        MusicSettings(mode="live"), transport=httpx.MockTransport(failing)
    )
    snap = await svc.snapshot()
    assert "playlists" in snap.errors
    assert snap.top_global is not None
    assert snap.latest_releases
    assert snap.new_artists
    await svc.aclose()


async def test_deezer_error_payload_raises() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"error": {"type": "Exception", "code": 4}})
    )
    client = DeezerClient(MusicSettings(), transport)
    with pytest.raises(RuntimeError, match="deezer error"):
        await client.chart_tracks()
    await client.aclose()


async def test_listenbrainz_token_header_and_empty_stats() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = ListenBrainzClient(
        MusicSettings(listenbrainz_token="lb-token"), httpx.MockTransport(handler)
    )
    tracks, start, end = await client.sitewide_recordings()
    assert tracks == []
    assert start is None
    assert end is None
    assert seen[0].headers["Authorization"] == "Token lb-token"
    assert await client.sitewide_artists() == []
    await client.aclose()


async def test_spotify_client_search_and_limits() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host not in {"accounts.spotify.com", "api.spotify.com"}:
            return fixture_transport().handler(request)  # type: ignore[attr-defined,no-any-return]
        seen.append(request)
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        kind = request.url.params["type"]
        if kind == "track":
            return httpx.Response(
                200,
                json={
                    "tracks": {
                        "items": [
                            {
                                "id": "sp1",
                                "name": "Song",
                                "artists": [{"name": "A"}, {"name": "B"}],
                                "album": {"name": "Alb"},
                                "external_urls": {"spotify": "https://open.spotify.com/track/sp1"},
                            },
                            None,
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "playlists": {
                    "items": [
                        {
                            "id": "pl1",
                            "name": "Mix",
                            "owner": {"display_name": "someone"},
                            "tracks": {"total": 12},
                            "external_urls": {"spotify": "https://open.spotify.com/playlist/pl1"},
                        }
                    ]
                }
            },
        )

    settings = MusicSettings(spotify_client_id="id", spotify_client_secret="secret")
    svc = MusicIntelligenceService(settings, transport=httpx.MockTransport(handler))
    tracks = await svc.search_spotify_tracks("song", limit=50)
    assert tracks[0].artist == "A, B"
    assert "limit=10" in str(seen[-1].url)
    both = await svc.playlists(3, query="mix")
    assert any(p.provenance.source == "spotify" and p.track_count == 12 for p in both)
    assert "search_spotify_tracks" in MusicTools(svc).registry().names
    await svc.aclose()

    with pytest.raises(ValueError, match="not configured"):
        SpotifyClient(MusicSettings())
    plain = _svc()
    with pytest.raises(RuntimeError, match="disabled"):
        await plain.search_spotify_tracks("x")
    await plain.aclose()


def test_settings_forbid_fixtures_in_prod() -> None:
    with pytest.raises(ValueError, match="not allowed in prod"):
        MusicSettings(mode="fixtures", ENVIRONMENT="prod")  # type: ignore[call-arg]


async def test_tools_cover_all_requirements() -> None:
    svc = _svc()
    tools = MusicTools(svc)
    assert set(tools.registry().names) == {
        "get_latest_releases",
        "get_new_artists",
        "get_playlists",
        "get_global_top_chart",
    }
    assert await tools.latest_releases(LatestReleasesArgs(limit=3))
    assert await tools.new_artists(NewArtistsArgs(limit=3))
    assert await tools.playlists(PlaylistsArgs(limit=3))
    dz = await tools.top_global(TopGlobalArgs(source="deezer_current", limit=5))
    assert isinstance(dz, list)
    assert len(dz) == 5
    await svc.aclose()


async def test_agent_with_scripted_llm_uses_tools() -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="1", name="get_global_top_chart", arguments={"limit": 5}),
                    ToolCall(id="2", name="get_new_artists", arguments={"limit": 3}),
                ]
            ),
            LLMResponse(text="Top 5 and 3 emerging artists."),
        ]
    )
    agent, svc = build_agent(service=_svc(), provider=provider)
    result = await agent.run("¿Cuál es el top global del último mes y qué artistas nuevos hay?")
    assert result.answer.startswith("Top 5")
    assert [r.name for r in result.tool_results] == ["get_global_top_chart", "get_new_artists"]
    assert not any(r.is_error for r in result.tool_results)
    await svc.aclose()


async def test_agent_offline_routes_spanish_query() -> None:
    agent, svc = build_agent(service=_svc(), provider=HeuristicProvider())
    result = await agent.run("muéstrame el top global del mes")
    assert result.tool_results[0].name == "get_global_top_chart"
    await svc.aclose()


async def test_mcp_server_exposes_tools() -> None:
    svc = _svc()
    server = build_mcp_server(svc)
    names = {t.name for t in await server.list_tools()}
    assert names == {
        "get_latest_releases",
        "get_new_artists",
        "get_playlists",
        "get_global_top_chart",
    }
    res = await server.call_tool("get_global_top_chart", {"limit": 3})
    assert not res.is_error
    assert res.structured_content is not None
    assert len(res.structured_content["result"]["tracks"]) == 3
    res2 = await server.call_tool("get_latest_releases", {"days": 7, "limit": 2})
    assert not res2.is_error
    res3 = await server.call_tool("get_new_artists", {"limit": 2})
    assert not res3.is_error
    res4 = await server.call_tool("get_playlists", {"limit": 2})
    assert not res4.is_error
    # Direct calls raise ToolError; the MCP protocol layer turns it into isError=true.
    with pytest.raises(ToolError, match="invalid arguments"):
        await server.call_tool("get_latest_releases", {"days": 500})
    await svc.aclose()


def test_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSIC_MODE", "fixtures")
    monkeypatch.setenv("LLM_PROVIDER", "heuristic")
    runner = CliRunner()
    snap = runner.invoke(app, ["snapshot", "--json"])
    assert snap.exit_code == 0, snap.output
    data = json.loads(snap.stdout)
    assert len(data["top_global"]["tracks"]) == 50
    assert data["errors"] == {}
    table = runner.invoke(app, ["snapshot"])
    assert table.exit_code == 0
    assert "Global Top 50" in table.stdout
    ask = runner.invoke(app, ["ask", "latest releases", "-o", "json"])
    assert ask.exit_code == 0, ask.output
    assert json.loads(ask.stdout)["tools"] == ["get_latest_releases"]
    ask2 = runner.invoke(app, ["ask", "playlists"])
    assert ask2.exit_code == 0
