"""Wiring and the request-level service: cache, conversation follow-ups, audit log."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from querywise.adapters.embeddings import FakeEmbedder, OpenAICompatEmbedder
from querywise.config import QuerySettings
from querywise.linking import CachedEmbedder, SchemaLinker
from querywise.pipeline import Answer, Pipeline
from querywise.present import jsonable
from querywise.semantic import SemanticLayer


def build_embedder(s: QuerySettings, sm: async_sessionmaker[AsyncSession] | None) -> CachedEmbedder:
    if s.embed_provider == "fake":
        return CachedEmbedder(FakeEmbedder(s.embed_dim), sm)
    key = s.embed_api_key or s.llm_api_key
    if key is None:
        raise ValueError("EMBED_PROVIDER=openai_compat needs EMBED_API_KEY or LLM_API_KEY")
    inner = OpenAICompatEmbedder(
        base_url=s.embed_base_url or s.llm_base_url,
        api_key=key.get_secret_value(),
        model=s.embed_model,
        dim=s.embed_dim,
    )
    return CachedEmbedder(inner, sm)


def build_linker(layer: SemanticLayer, embedder: CachedEmbedder, s: QuerySettings) -> SchemaLinker:
    return SchemaLinker(layer, embedder, top_columns=s.link_top_columns)


def _revive(v: Any) -> Any:
    """JSON loses timestamps; turn ISO strings back into datetimes so chart rules still apply."""
    if isinstance(v, str) and len(v) >= 10 and v[4:5] == "-" and v[7:8] == "-":
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return v
    return v


def sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


@dataclass
class QueryService:
    pipeline: Pipeline
    sm: async_sessionmaker[AsyncSession]
    cache_ttl_s: int = 600

    async def previous(self, conversation_id: str | None) -> tuple[str, str] | None:
        if not conversation_id:
            return None
        async with self.sm() as ss:
            r = (
                await ss.execute(
                    text(
                        "SELECT question, sql FROM query_log WHERE conversation_id = :c AND status = 'answered' "
                        "AND sql IS NOT NULL ORDER BY id DESC LIMIT 1"
                    ),
                    {"c": conversation_id},
                )
            ).first()
        return (r.question, r.sql) if r else None

    async def ask(
        self, question: str, user_id: str, conversation_id: str | None = None
    ) -> tuple[int, Answer]:
        t0 = time.perf_counter()
        prev = await self.previous(conversation_id)
        ans = await self.pipeline.answer(question, previous=prev, cache=self)
        ans.timings_ms["total"] = int((time.perf_counter() - t0) * 1000)
        qid = await self.audit(ans, user_id, conversation_id)
        return qid, ans

    # -------------------------------------------------------------- cache (normalised SQL hash)
    async def cache_get(self, sql: str) -> dict[str, Any] | None:
        if self.cache_ttl_s <= 0:
            return None
        async with self.sm() as ss:
            r = (
                await ss.execute(
                    text(
                        "SELECT result FROM query_cache WHERE sql_hash = :h AND expires_at > now()"
                    ),
                    {"h": sql_hash(sql)},
                )
            ).first()
        if r is None:
            return None
        v = r.result if isinstance(r.result, dict) else dict(json.loads(r.result))
        v["rows"] = [[_revive(x) for x in row] for row in v["rows"]]
        return v

    async def cache_put(self, sql: str, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        if self.cache_ttl_s <= 0:
            return
        payload = json.dumps({"columns": columns, "rows": [[jsonable(v) for v in r] for r in rows]})
        exp = datetime.now(UTC) + timedelta(seconds=self.cache_ttl_s)
        async with self.sm() as ss:
            await ss.execute(
                text(
                    "INSERT INTO query_cache (sql_hash, result, expires_at) VALUES (:h, CAST(:r AS jsonb), :e) "
                    "ON CONFLICT (sql_hash) DO UPDATE SET result = EXCLUDED.result, created_at = now(), expires_at = EXCLUDED.expires_at"
                ),
                {"h": sql_hash(sql), "r": payload, "e": exp},
            )
            await ss.commit()

    # -------------------------------------------------------------- audit (every request)
    async def audit(self, ans: Answer, user_id: str, conversation_id: str | None) -> int:
        async with self.sm() as ss:
            qid = (
                await ss.execute(
                    text(
                        "INSERT INTO query_log (user_id, conversation_id, question, status, reason, sql, sql_hash, row_count, "
                        "attempts, cached, timings_ms, tokens_in, tokens_out, cost_usd) VALUES (:u, :c, :q, :s, :r, :sql, :h, :n, "
                        ":a, :cached, CAST(:t AS jsonb), :ti, :to, :cost) RETURNING id"
                    ),
                    {
                        "u": user_id,
                        "c": conversation_id,
                        "q": ans.question[:2000],
                        "s": ans.status,
                        "r": " ".join(x for x in (ans.reason, ans.detail) if x) or None,
                        "sql": ans.sql,
                        "h": sql_hash(ans.sql) if ans.sql else None,
                        "n": len(ans.rows) if ans.sql else None,
                        "a": len(ans.attempts),
                        "cached": ans.cached,
                        "t": json.dumps(ans.timings_ms),
                        "ti": ans.tokens_in,
                        "to": ans.tokens_out,
                        "cost": ans.cost_usd,
                    },
                )
            ).scalar_one()
            await ss.commit()
        return int(qid)
