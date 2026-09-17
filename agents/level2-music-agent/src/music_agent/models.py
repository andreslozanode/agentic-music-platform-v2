"""Normalised, provenance-carrying music domain models."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, Field

SourceName = Literal["listenbrainz", "deezer", "spotify", "musicbrainz"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalise_key(*parts: str) -> str:
    """Accent/case/punctuation-insensitive key used for cross-source deduplication."""
    text = " ".join(parts)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"\(.*?\)|\[.*?\]|feat\..*$", "", text.lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


class Provenance(BaseModel):
    source: SourceName
    endpoint: str
    retrieved_at: datetime = Field(default_factory=utcnow)
    note: str | None = None


class Track(BaseModel):
    title: str
    artist: str
    album: str | None = None
    rank: int | None = None
    listen_count: int | None = None
    external_id: str | None = None
    mbid: str | None = None
    url: str | None = None
    provenance: Provenance

    @property
    def key(self) -> str:
        return normalise_key(self.artist, self.title)


class Artist(BaseModel):
    name: str
    mbid: str | None = None
    rank: int | None = None
    listen_count: int | None = None
    fans: int | None = None
    url: str | None = None
    signal: str | None = Field(
        default=None, description="Why this artist is considered new/emerging"
    )
    evidence: list[str] = Field(default_factory=list)
    provenance: Provenance

    @property
    def key(self) -> str:
        return normalise_key(self.name)


class Release(BaseModel):
    title: str
    artist: str
    release_date: date | None = None
    release_type: str | None = None
    mbid: str | None = None
    external_id: str | None = None
    url: str | None = None
    provenance: Provenance

    @property
    def key(self) -> str:
        return normalise_key(self.artist, self.title)


class Playlist(BaseModel):
    title: str
    external_id: str
    curator: str | None = None
    track_count: int | None = None
    fans: int | None = None
    rank: int | None = None
    url: str | None = None
    provenance: Provenance


class TopChart(BaseModel):
    label: str
    period_start: datetime | None = None
    period_end: datetime | None = None
    tracks: list[Track]
    cross_source_overlap: float | None = Field(
        default=None, description="Jaccard overlap with the secondary chart (0-1)"
    )
    secondary_source: str | None = None


class MusicSnapshot(BaseModel):
    generated_at: datetime = Field(default_factory=utcnow)
    latest_releases: list[Release] = Field(default_factory=list)
    new_artists: list[Artist] = Field(default_factory=list)
    playlists: list[Playlist] = Field(default_factory=list)
    top_global: TopChart | None = None
    errors: dict[str, str] = Field(default_factory=dict)
