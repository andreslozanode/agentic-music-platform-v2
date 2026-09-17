"""Level 3 configuration with a single cloud toggle.

Cloud differences are isolated to three things: the lakehouse storage URI scheme,
the credential chain (workload identity - never static keys) and the Terraform/Helm
values that provision them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_core.config import secrets_dir

Environment = Literal["dev", "ci", "staging", "prod"]
Cloud = Literal["local", "aws", "gcp", "azure"]

STORAGE_SCHEMES: dict[str, tuple[str, ...]] = {
    "local": ("./", "/", "file://", "var/"),
    "aws": ("s3://",),
    "gcp": ("gs://",),
    "azure": ("abfss://", "az://"),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_", env_file=".env", extra="ignore", secrets_dir=secrets_dir()
    )

    environment: Environment = Field(default="dev", validation_alias="ENVIRONMENT")
    cloud: Cloud = "local"
    storage_root: str = "./var/lakehouse"
    storage_options_json: str = Field(
        default="{}", description="Extra object-store options (never credentials)"
    )
    state_dir: Path = Path("./var/state")
    policy_path: Path | None = None

    # Vector database
    vector_backend: Literal["memory", "local", "server"] = "memory"
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    qdrant_path: Path = Path("./var/qdrant")
    collection: str = Field(default="music_insights", pattern=r"^[a-z0-9_]{3,64}$")

    # Embeddings
    embedding_provider: Literal["hashing", "fastembed", "openai_compatible"] = "hashing"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = Field(default=384, ge=32, le=4096)
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None

    # Governance / security
    audit_hmac_key: SecretStr | None = None
    api_keys: SecretStr | None = Field(
        default=None, description="Comma list of name:role1|role2:sha256(key)"
    )
    jwt_jwks_url: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)
    max_body_bytes: int = Field(default=16_384, ge=1024, le=1_048_576)
    pod_name: str = Field(default="local", validation_alias="POD_NAME")

    # Autonomy / observability
    cycle_interval_s: int = Field(default=6 * 3600, ge=60)
    metrics_port: int = Field(default=9464, ge=1024, le=65535)
    otel_endpoint: str | None = None

    @field_validator("storage_options_json")
    @classmethod
    def _options(cls, value: str) -> str:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("AGENT_STORAGE_OPTIONS_JSON must be a JSON object")  # noqa: TRY004
        forbidden = [k for k in parsed if any(s in k.lower() for s in ("secret", "key", "token"))]
        if forbidden:
            raise ValueError(f"credentials must come from workload identity, not {forbidden}")
        return value

    @property
    def storage_options(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in json.loads(self.storage_options_json).items()}

    @property
    def is_production_like(self) -> bool:
        return self.environment in {"staging", "prod"}

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if not self.storage_root.startswith(STORAGE_SCHEMES[self.cloud]):
            raise ValueError(
                f"AGENT_STORAGE_ROOT '{self.storage_root}' does not match cloud '{self.cloud}'"
            )
        if self.vector_backend == "server" and not self.qdrant_url:
            raise ValueError("AGENT_QDRANT_URL is required for the server vector backend")
        if self.embedding_provider == "openai_compatible" and not self.embedding_base_url:
            raise ValueError("AGENT_EMBEDDING_BASE_URL is required for openai_compatible")
        if self.is_production_like:
            problems = []
            if self.audit_hmac_key is None:
                problems.append("AGENT_AUDIT_HMAC_KEY")
            if not (self.api_keys or self.jwt_jwks_url):
                problems.append("AGENT_API_KEYS or AGENT_JWT_JWKS_URL")
            if self.vector_backend != "server":
                problems.append("AGENT_VECTOR_BACKEND=server")
            if (
                self.qdrant_url
                and not self.qdrant_url.startswith("https://")
                and (self.environment == "prod")
            ):
                problems.append("https AGENT_QDRANT_URL")
            if self.environment == "prod" and self.cloud == "local":
                problems.append("AGENT_CLOUD != local")
            if self.environment == "prod" and self.embedding_provider == "hashing":
                problems.append("a semantic AGENT_EMBEDDING_PROVIDER")
            if self.jwt_jwks_url and not (self.jwt_issuer and self.jwt_audience):
                problems.append("AGENT_JWT_ISSUER and AGENT_JWT_AUDIENCE")
            if problems:
                raise ValueError(f"{self.environment} requires: {', '.join(problems)}")
        return self
