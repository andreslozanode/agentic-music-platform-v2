"""Hybrid retrieval: dense (Qdrant) + BM25 re-scoring fused with Reciprocal Rank Fusion.

Retrieved chunks are screened for prompt injection before reaching the LLM
(OWASP LLM08 - vector/embedding weaknesses & indirect injection).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from autonomous_agent.governance.guardrails import PromptInjectionDetector
from autonomous_agent.pipeline_types import IndexableDocument
from autonomous_agent.rag.embeddings import Embedder, tokenize
from autonomous_agent.rag.vectorstore import QdrantStore, ScoredDocument, StoredDocument


@dataclass
class RetrievedChunk:
    citation: str
    doc: StoredDocument
    dense_score: float
    fused_score: float


def bm25_scores(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    q = set(tokenize(query))
    toks = [tokenize(d) for d in docs]
    n = len(docs)
    if n == 0:
        return []
    avgdl = sum(len(t) for t in toks) / n or 1.0
    df = Counter(term for t in toks for term in set(t))
    scores = []
    for t in toks:
        tf = Counter(t)
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(t) / avgdl))
        scores.append(s)
    return scores


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused


class HybridRetriever:
    def __init__(
        self,
        embedder: Embedder,
        store: QdrantStore,
        classifications: list[str],
        injection_threshold: float = 0.6,
    ) -> None:
        self.embedder = embedder
        self.store = store
        self.classifications = classifications
        self.detector = PromptInjectionDetector()
        self.injection_threshold = injection_threshold
        self.quarantined: list[str] = []

    async def retrieve(self, query: str, k: int = 5) -> list[RetrievedChunk]:
        [vector] = await self.embedder.embed([query])
        dense: list[ScoredDocument] = await self.store.search(vector, k * 4, self.classifications)
        safe: list[ScoredDocument] = []
        for hit in dense:
            if self.detector.assess(hit.doc.text).score >= self.injection_threshold:
                self.quarantined.append(hit.doc.doc_id)
                continue
            safe.append(hit)
        if not safe:
            return []
        lexical = bm25_scores(query, [f"{h.doc.title} {h.doc.text}" for h in safe])
        dense_rank = [h.doc.doc_id for h in safe]
        lex_rank = [
            h.doc.doc_id for _, h in sorted(zip(lexical, safe, strict=True), key=lambda x: -x[0])
        ]
        fused = rrf([dense_rank, lex_rank])
        by_id = {h.doc.doc_id: h for h in safe}
        ordered = sorted(fused, key=lambda d: -fused[d])[:k]
        return [
            RetrievedChunk(f"[S{i}]", by_id[d].doc, by_id[d].score, round(fused[d], 6))
            for i, d in enumerate(ordered, start=1)
        ]


class Indexer:
    """Incremental indexing keyed by content hash (only changed documents are embedded)."""

    def __init__(self, embedder: Embedder, store: QdrantStore) -> None:
        self.embedder = embedder
        self.store = store

    async def index(self, docs: Sequence[IndexableDocument], as_of: str) -> dict[str, int]:
        await self.store.ensure()
        existing = await self.store.existing_hashes([d.doc_id for d in docs])
        changed = [d for d in docs if existing.get(d.doc_id) != d.content_sha256]
        stored = [
            StoredDocument(
                doc_id=d.doc_id,
                title=d.title,
                text=d.text,
                doc_type=d.doc_type,
                source=d.source,
                classification=d.classification,
                content_sha256=d.content_sha256,
                as_of=as_of,
            )
            for d in changed
        ]
        vectors = await self.embedder.embed([f"{d.title}\n{d.text}" for d in stored])
        written = await self.store.upsert(stored, vectors)
        return {"received": len(docs), "embedded": written, "skipped": len(docs) - written}
