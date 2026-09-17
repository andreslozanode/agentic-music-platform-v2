"""Level 1 configuration."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_core.config import secrets_dir

# Reddit requires a descriptive, unique User-Agent: <platform>:<app id>:<version> (by /u/<user>)
USER_AGENT_RE = re.compile(r"^[\w.-]+:[\w.-]+:v?[\w.-]+ \(by /u/[\w-]+\)$")


class RedditSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REDDIT_", env_file=".env", extra="ignore", secrets_dir=secrets_dir()
    )

    mode: Literal["api", "fixtures"] = "api"
    environment: Literal["dev", "ci", "staging", "prod"] = Field(
        default="dev", validation_alias="ENVIRONMENT"
    )
    client_id: str | None = None
    client_secret: SecretStr | None = None
    user_agent: str = "python:agentic-ai-platform.reddit-agent:0.1.0 (by /u/agentic-platform)"
    requests_per_minute: int = Field(default=60, ge=1, le=100)
    include_nsfw: bool = False
    max_results: int = Field(default=10, ge=1, le=25)

    @field_validator("user_agent")
    @classmethod
    def _ua(cls, value: str) -> str:
        if not USER_AGENT_RE.fullmatch(value):
            raise ValueError("REDDIT_USER_AGENT must look like 'platform:app:version (by /u/name)'")
        return value

    @model_validator(mode="after")
    def _no_fixtures_in_prod(self) -> RedditSettings:
        if self.environment == "prod" and self.mode == "fixtures":
            raise ValueError("fixture mode is not allowed in prod")
        return self
