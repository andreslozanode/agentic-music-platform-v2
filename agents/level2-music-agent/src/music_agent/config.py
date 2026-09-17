"""Level 2 configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_core.config import secrets_dir


class MusicSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSIC_", env_file=".env", extra="ignore", secrets_dir=secrets_dir()
    )

    mode: Literal["live", "fixtures"] = "live"
    environment: Literal["dev", "ci", "staging", "prod"] = Field(
        default="dev", validation_alias="ENVIRONMENT"
    )
    # MetaBrainz asks for a meaningful User-Agent with contact information.
    user_agent: str = (
        "agentic-ai-platform-music-agent/0.1.0 "
        "( https://github.com/agentic-ai-platform/agentic-ai-platform )"
    )
    listenbrainz_token: SecretStr | None = None
    listenbrainz_rps: int = Field(default=2, ge=1, le=10)
    deezer_rps: int = Field(default=8, ge=1, le=10, description="Deezer allows 50 req / 5 s")
    spotify_client_id: str | None = None
    spotify_client_secret: SecretStr | None = None
    cache_ttl_s: float = Field(default=900, ge=0)
    known_artists_depth: int = Field(
        default=300, ge=100, le=1000, description="All-time top artists considered 'established'"
    )

    @property
    def rate_limit_factor(self) -> int:
        """Fixtures are served locally, so client-side throttling is pointless there."""
        return 1000 if self.mode == "fixtures" else 1

    @property
    def spotify_enabled(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @model_validator(mode="after")
    def _no_fixtures_in_prod(self) -> MusicSettings:
        if self.environment == "prod" and self.mode == "fixtures":
            raise ValueError("fixture mode is not allowed in prod")
        return self
