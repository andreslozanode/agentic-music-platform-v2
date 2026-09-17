"""Composition root: builds every component from configuration and enforces the
runtime policy (allowed LLM provider and data mode per environment) at startup."""

from __future__ import annotations

from dataclasses import dataclass

from agent_core import LLMProvider, build_provider
from agent_core.config import LLMSettings
from autonomous_agent.config import Settings
from autonomous_agent.data.pipeline import FairnessThresholds, MedallionPipeline
from autonomous_agent.data.storage import LakehouseStorage
from autonomous_agent.governance.approvals import ApprovalStore
from autonomous_agent.governance.audit import AuditLog
from autonomous_agent.governance.policy import Policy, PolicyEngine, load_policy
from autonomous_agent.graph.workflow import GovernedAgent
from autonomous_agent.rag.embeddings import Embedder, build_embedder
from autonomous_agent.rag.retriever import HybridRetriever, Indexer
from autonomous_agent.rag.vectorstore import QdrantStore, build_client
from music_agent.config import MusicSettings
from music_agent.service import MusicIntelligenceService
from music_agent.tools import MusicTools


@dataclass
class Runtime:
    settings: Settings
    music_settings: MusicSettings
    policy: Policy
    engine: PolicyEngine
    audit: AuditLog
    approvals: ApprovalStore
    storage: LakehouseStorage
    pipeline: MedallionPipeline
    service: MusicIntelligenceService
    embedder: Embedder
    store: QdrantStore
    retriever: HybridRetriever
    indexer: Indexer
    provider: LLMProvider
    agent: GovernedAgent

    @classmethod
    async def create(
        cls,
        settings: Settings | None = None,
        *,
        llm_settings: LLMSettings | None = None,
        music_settings: MusicSettings | None = None,
        provider: LLMProvider | None = None,
        service: MusicIntelligenceService | None = None,
    ) -> Runtime:
        settings = settings or Settings()
        music_settings = music_settings or (service.settings if service else MusicSettings())
        policy = load_policy(settings.policy_path)
        engine = PolicyEngine(policy, settings.environment)
        provider = provider or build_provider(llm_settings or LLMSettings())
        engine.enforce_runtime(provider.name, music_settings.mode)

        state = settings.state_dir
        key = (
            settings.audit_hmac_key.get_secret_value().encode() if settings.audit_hmac_key else None
        )
        audit = AuditLog(state / "audit" / f"audit-{settings.pod_name}.jsonl", key)
        approvals = ApprovalStore(state / "approvals.json")
        storage = LakehouseStorage(settings.storage_root, settings.storage_options)
        rai = policy.responsible_ai
        pipeline = MedallionPipeline(
            storage,
            audit,
            state / "lineage" / "openlineage.jsonl",
            FairnessThresholds(
                rai.max_hhi,
                rai.max_top_artist_share,
                rai.min_cross_source_overlap,
                rai.disclosure,
            ),
        )
        service = service or MusicIntelligenceService(music_settings)
        embedder = build_embedder(settings)
        store = QdrantStore(build_client(settings), settings.collection, embedder.dim)
        await store.ensure()
        retriever = HybridRetriever(
            embedder,
            store,
            policy.data.classifications_for_llm,
            policy.guardrails.injection_threshold,
        )
        indexer = Indexer(embedder, store)
        agent = GovernedAgent(provider, MusicTools(service), retriever, engine, audit)
        audit.record(
            "runtime_started",
            "system",
            {
                "environment": settings.environment,
                "cloud": settings.cloud,
                "provider": provider.name,
                "model": provider.model,
                "music_mode": music_settings.mode,
                "policy": policy.name,
                "policy_version": policy.version,
                "vector_backend": settings.vector_backend,
                "embedder": embedder.name,
            },
        )
        return cls(
            settings,
            music_settings,
            policy,
            engine,
            audit,
            approvals,
            storage,
            pipeline,
            service,
            embedder,
            store,
            retriever,
            indexer,
            provider,
            agent,
        )

    async def close(self) -> None:
        await self.service.aclose()
        await self.store.close()
