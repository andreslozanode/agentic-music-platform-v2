"""Tool definitions for the music agent (re-used by the Level 3 autonomous agent)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent_core import Tool, ToolRegistry
from music_agent.models import Artist, Playlist, Release, TopChart, Track
from music_agent.service import MusicIntelligenceService


class LatestReleasesArgs(BaseModel):
    days: int = Field(default=14, ge=1, le=90, description="Look-back window in days")
    limit: int = Field(default=20, ge=1, le=50)
    release_type: Literal["album", "single", "ep"] | None = None


class NewArtistsArgs(BaseModel):
    days: int = Field(default=30, ge=7, le=90)
    limit: int = Field(default=15, ge=1, le=50)


class PlaylistsArgs(BaseModel):
    query: str | None = Field(
        default=None, max_length=100, description="Search term; omit for chart playlists"
    )
    limit: int = Field(default=10, ge=1, le=25)


class TopGlobalArgs(BaseModel):
    source: Literal["listenbrainz_last_month", "deezer_current"] = "listenbrainz_last_month"
    limit: int = Field(default=50, ge=1, le=100)


class SpotifySearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=100)
    limit: int = Field(default=5, ge=1, le=10)


class MusicTools:
    def __init__(self, service: MusicIntelligenceService) -> None:
        self.service = service

    async def latest_releases(self, a: LatestReleasesArgs) -> list[Release]:
        return await self.service.latest_releases(a.days, a.limit, a.release_type)

    async def new_artists(self, a: NewArtistsArgs) -> list[Artist]:
        return await self.service.new_artists(a.days, a.limit)

    async def playlists(self, a: PlaylistsArgs) -> list[Playlist]:
        return await self.service.playlists(a.limit, a.query)

    async def top_global(self, a: TopGlobalArgs) -> TopChart | list[Track]:
        if a.source == "deezer_current":
            return await self.service.deezer_chart(a.limit)
        return await self.service.top_global(a.limit)

    async def spotify_search(self, a: SpotifySearchArgs) -> list[Track]:
        return await self.service.search_spotify_tracks(a.query, a.limit)

    def tools(self) -> list[Tool]:
        tools = [
            Tool(
                "get_latest_releases",
                "Latest released tracks/albums (ListenBrainz fresh releases + Deezer editorial).",
                LatestReleasesArgs,
                self.latest_releases,
                keywords=(
                    "latest",
                    "new",
                    "releases",
                    "tracks",
                    "lanzamientos",
                    "ultimos",
                    "últimos",
                    "recent",
                ),
            ),
            Tool(
                "get_new_artists",
                "New/emerging artists: fresh releases or monthly chart presence, not in the "
                "all-time top artists.",
                NewArtistsArgs,
                self.new_artists,
                keywords=("new", "emerging", "artists", "nuevos", "artistas", "rising"),
            ),
            Tool(
                "get_playlists",
                "Popular playlists (Deezer chart) or playlist search by keyword.",
                PlaylistsArgs,
                self.playlists,
                keywords=("playlist", "playlists", "listas"),
            ),
            Tool(
                "get_global_top_chart",
                "Global Top-N tracks for the last month (ListenBrainz sitewide listens) or the "
                "current Deezer global chart.",
                TopGlobalArgs,
                self.top_global,
                keywords=("top", "global", "chart", "ranking", "top50", "month", "mes"),
            ),
        ]
        if self.service.spotify is not None:
            tools.append(
                Tool(
                    "search_spotify_tracks",
                    "Search Spotify's catalogue for tracks (max 10 results) to get Spotify links.",
                    SpotifySearchArgs,
                    self.spotify_search,
                    keywords=("spotify", "link", "search"),
                )
            )
        return tools

    def registry(self) -> ToolRegistry:
        return ToolRegistry(self.tools())
