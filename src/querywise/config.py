"""QueryWise settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field

from app.config import Settings


class QuerySettings(Settings):
    app_name: str = "querywise"
    worker_mode: str = "off"  # type: ignore[assignment]  # requests are synchronous

    # Generated SQL runs ONLY on this connection: role analyst_ro, SELECT on schema curated,
    # statement_timeout 5 s (set on the role by migration 0002). Never the owner DSN.
    analytics_database_url: str = (
        "postgresql+asyncpg://analyst_ro:analyst-ro-dev@127.0.0.1:5437/app"  # dev default
    )

    # Embeddings for schema linking and example retrieval
    embed_provider: Literal["fake", "openai_compat"] = "fake"
    embed_model: str = "gemini-embedding-001"
    embed_dim: int = 768

    # Pipeline
    semantic_layer_path: str = "config/semantic_layer.yaml"
    prompt_version: str = "sql_v1"
    use_linking: bool = True
    use_semantic_layer: bool = True
    max_repairs: int = Field(2, ge=0, le=2)  # hard cap: the manual's "<= 2"
    few_shot_k: int = Field(0, ge=0, le=5)
    link_top_columns: int = Field(14, ge=4, le=60)
    summary_mode: Literal["llm", "template"] = "llm"

    # Guard / execution
    max_rows: int = Field(500, ge=1, le=5000)
    # 24x the most expensive gold query (2,073); a cartesian join of two views costs ~1.5M.
    explain_cost_ceiling: float = Field(50_000, gt=0)
    statement_timeout_ms: int = Field(5000, ge=100, le=30_000)
    cache_ttl_s: int = Field(600, ge=0)
    queries_per_minute: int = Field(20, ge=1)  # per API key


@lru_cache
def get_query_settings() -> QuerySettings:
    return QuerySettings()
