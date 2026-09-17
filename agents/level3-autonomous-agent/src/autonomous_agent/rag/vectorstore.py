"""Qdrant vector store (in-memory, embedded on-disk, or server/Qdrant Cloud)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from autonomous_agent.config import Settings

ID_NAMESPACE = uuid.UUID("6f1c0b8e-6a8d-4d7e-9a57-0c0de5a1a001")


@dataclass
class StoredDocument:
    doc_id: str
    title: str
    text: str
    doc_type: str
    source: str
    classification: str
    content_sha256: str
    as_of: str


@dataclass
class ScoredDocument:
    doc: StoredDocument
    score: float


def point_id(doc_id: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, doc_id))


def build_client(settings: Settings) -> AsyncQdrantClient:
    if settings.vector_backend == "server":
        key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
        return AsyncQdrantClient(url=settings.qdrant_url, api_key=key, timeout=10)
    if settings.vector_backend == "local":
        settings.qdrant_path.mkdir(parents=True, exist_ok=True)
        return AsyncQdrantClient(path=str(settings.qdrant_path))
    return AsyncQdrantClient(location=":memory:")


class QdrantStore:
    def __init__(self, client: AsyncQdrantClient, collection: str, dim: int) -> None:
        self.client = client
        self.collection = collection
        self.dim = dim

    async def ensure(self) -> None:
        if not await self.client.collection_exists(self.collection):
            await self.client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE),
            )
            for field_name in ("classification", "doc_type", "as_of"):
                await self.client.create_payload_index(
                    self.collection, field_name, models.PayloadSchemaType.KEYWORD
                )

    async def healthy(self) -> bool:
        try:
            await self.client.get_collections()
        except Exception:
            return False
        return True

    async def existing_hashes(self, doc_ids: list[str]) -> dict[str, str]:
        if not doc_ids:
            return {}
        points = await self.client.retrieve(
            self.collection, [point_id(d) for d in doc_ids], with_payload=True
        )
        return {
            str(p.payload["doc_id"]): str(p.payload["content_sha256"]) for p in points if p.payload
        }

    async def upsert(self, docs: list[StoredDocument], vectors: list[list[float]]) -> int:
        if not docs:
            return 0
        await self.client.upsert(
            self.collection,
            points=[
                models.PointStruct(id=point_id(d.doc_id), vector=v, payload=d.__dict__)
                for d, v in zip(docs, vectors, strict=True)
            ],
            wait=True,
        )
        return len(docs)

    async def search(
        self, vector: list[float], k: int, classifications: list[str]
    ) -> list[ScoredDocument]:
        flt = models.Filter(
            must=[
                models.FieldCondition(
                    key="classification", match=models.MatchAny(any=classifications)
                )
            ]
        )
        res = await self.client.query_points(
            self.collection, query=vector, limit=k, query_filter=flt, with_payload=True
        )
        out: list[ScoredDocument] = []
        for p in res.points:
            payload: dict[str, Any] = dict(p.payload or {})
            out.append(ScoredDocument(StoredDocument(**payload), float(p.score)))
        return out

    async def count(self) -> int:
        return int((await self.client.count(self.collection, exact=True)).count)

    async def close(self) -> None:
        await self.client.close()
