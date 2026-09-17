"""Embedding providers.

* ``hashing`` - deterministic feature hashing (dev/CI; lexical, no downloads).
* ``fastembed`` - local ONNX models (e.g. BAAI/bge-small-en-v1.5), no data leaves the pod.
* ``openai_compatible`` - any ``/v1/embeddings`` server (Ollama, vLLM, TEI, gateways).
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import math
import re
from collections import Counter
from typing import Any, Protocol
from urllib.parse import urlparse

from agent_core.http import build_http_client, request_json
from autonomous_agent.config import Settings

_TOKEN = re.compile(r"[a-záéíóúñü0-9]+", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


class Embedder(Protocol):
    dim: int
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    name = "hashing-v1"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        toks = tokenize(text)
        feats = Counter(toks + [f"{a}_{b}" for a, b in itertools.pairwise(toks)])
        vec = [0.0] * self.dim
        for feat, count in feats.items():
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
            sign = 1.0 if h & 1 else -1.0
            vec[(h >> 1) % self.dim] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


class FastEmbedEmbedder:  # pragma: no cover - requires model download
    def __init__(self, model: str, dim: int) -> None:
        from fastembed import TextEmbedding  # noqa: PLC0415 - optional extra

        self._model = TextEmbedding(model_name=model)
        self.dim = dim
        self.name = f"fastembed:{model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        def run() -> list[list[float]]:
            return [list(map(float, v)) for v in self._model.embed(texts)]

        return await asyncio.to_thread(run)


class OpenAICompatibleEmbedder:
    def __init__(
        self, base_url: str, model: str, dim: int, api_key: str | None = None, transport: Any = None
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https":
            raise ValueError("embedding endpoint must use https")
        self._url = base_url.rstrip("/") + "/embeddings"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = build_http_client(
            allowed_hosts=[parsed.hostname or ""],
            user_agent="autonomous-agent/0.1",
            headers=headers,
            transport=transport,
        )
        self.model, self.dim, self.name = model, dim, f"openai_compatible:{model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        data = await request_json(
            self._http, "POST", self._url, json={"model": self.model, "input": texts}
        )
        vectors = [list(map(float, item["embedding"])) for item in data["data"]]
        if any(len(v) != self.dim for v in vectors):
            raise ValueError(f"embedding dimension mismatch (expected {self.dim})")
        return vectors


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "fastembed":  # pragma: no cover
        return FastEmbedEmbedder(settings.embedding_model, settings.embedding_dim)
    if settings.embedding_provider == "openai_compatible":
        key = settings.embedding_api_key.get_secret_value() if settings.embedding_api_key else None
        return OpenAICompatibleEmbedder(
            settings.embedding_base_url or "",
            settings.embedding_model,
            settings.embedding_dim,
            key,
        )
    return HashingEmbedder(settings.embedding_dim)
