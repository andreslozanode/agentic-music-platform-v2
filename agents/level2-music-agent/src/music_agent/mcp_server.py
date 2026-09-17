"""MCP server exposing the music tools to any MCP client (Claude Desktop, IDEs, agents).

Run over stdio (default) or streamable HTTP::

    music-agent mcp --transport stdio
    music-agent mcp --transport streamable-http
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ValidationError

from music_agent.service import MusicIntelligenceService
from music_agent.tools import (
    LatestReleasesArgs,
    MusicTools,
    NewArtistsArgs,
    PlaylistsArgs,
    TopGlobalArgs,
)

Transport = Literal["stdio", "sse", "streamable-http"]


def _args[M: BaseModel](model: type[M], data: dict[str, Any]) -> M:
    """Validate arguments; invalid input becomes an ``is_error`` result for the client."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        details = exc.errors(include_url=False, include_input=False)
        raise ToolError(f"invalid arguments: {details}") from exc


def _dump(value: Any) -> Any:
    if isinstance(value, list):
        return [v.model_dump(mode="json") for v in value]
    return value.model_dump(mode="json")


def build_mcp_server(service: MusicIntelligenceService | None = None) -> MCPServer:
    tools = MusicTools(service or MusicIntelligenceService())
    server = MCPServer(
        name="music-intelligence",
        instructions="Read-only music intelligence tools. Results carry source provenance.",
        version="0.1.0",
    )

    @server.tool(description="Latest releases from ListenBrainz and Deezer editorial.")
    async def get_latest_releases(
        days: int = 14, limit: int = 20, release_type: str | None = None
    ) -> list[dict[str, Any]]:
        args = _args(
            LatestReleasesArgs, {"days": days, "limit": limit, "release_type": release_type}
        )
        result: list[dict[str, Any]] = _dump(await tools.latest_releases(args))
        return result

    @server.tool(description="Heuristically detected new/emerging artists.")
    async def get_new_artists(days: int = 30, limit: int = 15) -> list[dict[str, Any]]:
        args = _args(NewArtistsArgs, {"days": days, "limit": limit})
        result: list[dict[str, Any]] = _dump(await tools.new_artists(args))
        return result

    @server.tool(description="Chart playlists, or playlist search when query is given.")
    async def get_playlists(query: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = _dump(
            await tools.playlists(_args(PlaylistsArgs, {"query": query, "limit": limit}))
        )
        return result

    @server.tool(description="Global top tracks: ListenBrainz last month or Deezer current.")
    async def get_global_top_chart(
        source: str = "listenbrainz_last_month", limit: int = 50
    ) -> dict[str, Any]:
        args = _args(TopGlobalArgs, {"source": source, "limit": limit})
        return {"result": _dump(await tools.top_global(args))}

    return server


def run(
    transport: Transport = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    allowed_hosts: list[str] | None = None,
) -> None:  # pragma: no cover - blocking entrypoint
    server = build_mcp_server()
    if transport == "stdio":
        server.run("stdio")
        return
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts or [f"127.0.0.1:{port}", f"localhost:{port}"],
        allowed_origins=[],
    )
    if transport == "streamable-http":
        server.run(
            "streamable-http",
            host=host,
            port=port,
            stateless_http=True,
            json_response=True,
            transport_security=security,
        )
    else:
        server.run("sse", host=host, port=port, transport_security=security)
