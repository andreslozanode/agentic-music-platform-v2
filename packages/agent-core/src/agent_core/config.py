"""Shared configuration models."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["anthropic", "openai_compatible", "heuristic"]

#: Plain-HTTP endpoints are only acceptable for local or in-cluster model servers.
_PLAIN_HTTP_ALLOWED = ("http://localhost", "http://127.0.0.1", "http://ollama")


SECRETS_DIR_ENV = "AGENTIC_SECRETS_DIR"


def secrets_dir() -> str | None:
    """Directory of mounted secret files (one file per setting, e.g. ``LLM_API_KEY``).

    Kubernetes mounts secrets as files so they never appear in the pod spec or process
    environment; environment variables still work for local development.
    """
    path = os.environ.get(SECRETS_DIR_ENV)
    return path if path and Path(path).is_dir() else None


class LLMSettings(BaseSettings):
    """LLM provider configuration.

    ``heuristic`` is a deterministic, network-free provider intended for local demos,
    CI and ephemeral e2e clusters. Governance policies forbid it in production.
    """

    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=".env", extra="ignore", secrets_dir=secrets_dir()
    )

    provider: ProviderName = "anthropic"
    model: str = "claude-sonnet-5"
    api_key: SecretStr | None = None
    base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible base URL, e.g. http://localhost:11434/v1 for Ollama.",
    )
    max_tokens: int = Field(default=1024, ge=64, le=16_000)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    timeout_s: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _validate_provider(self) -> LLMSettings:
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("LLM_BASE_URL is required for the openai_compatible provider")
        if self.base_url and not self.base_url.startswith(("https://", *_PLAIN_HTTP_ALLOWED)):
            raise ValueError("LLM_BASE_URL must use https (plain http only for local/in-cluster)")
        return self
