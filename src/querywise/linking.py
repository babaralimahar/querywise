"""Schema linking and verified-example retrieval over embeddings (cached in Postgres).

Every text is embedded once per model: column docs at startup, each question the first time it
is asked. On the Gemini free tier that keeps embedding calls to a few hundred in total.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from querywise.adapters.embeddings import Embedder
from querywise.semantic import SemanticLayer


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class CachedEmbedder:
    """Wraps an Embedder with a Postgres cache keyed by sha256(model + text)."""

    def __init__(self, inner: Embedder, sm: async_sessionmaker[AsyncSession] | None) -> None:
        self.inner, self.sm = inner, sm
        self.model, self.dim = inner.model, inner.dim
        self._mem: dict[str, list[float]] = {}

    def _key(self, t: str) -> str:
        return hashlib.sha256(f"{self.model}\x00{t}".encode()).hexdigest()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        keys = [self._key(t) for t in texts]
        missing = [k for k in dict.fromkeys(keys) if k not in self._mem]
        if missing and self.sm is not None:
            async with self.sm() as ss:
                rows = (
                    await ss.execute(
                        text("SELECT key, embedding FROM embed_cache WHERE key = ANY(:k)"),
                        {"k": missing},
                    )
                ).all()
            for k, v in rows:
                self._mem[k] = v if isinstance(v, list) else json.loads(v)
        todo = [
            (k, t) for k, t in dict(zip(keys, texts, strict=True)).items() if k not in self._mem
        ]
        if todo:
            vecs = await self.inner.embed([t for _, t in todo])
            for (k, _), v in zip(todo, vecs, strict=True):
                self._mem[k] = v
            if self.sm is not None:
                async with self.sm() as ss:
                    await ss.execute(
                        text(
                            "INSERT INTO embed_cache (key, embedding) VALUES (:k, CAST(:v AS jsonb)) ON CONFLICT DO NOTHING"
                        ),
                        [{"k": k, "v": json.dumps(self._mem[k])} for k, _ in todo],
                    )
                    await ss.commit()
        return [self._mem[k] for k in keys]


@dataclass
class Linked:
    tables: set[str]
    columns: set[tuple[str, str]]
    scores: list[tuple[float, str]]


class SchemaLinker:
    def __init__(
        self, layer: SemanticLayer, embedder: CachedEmbedder, top_columns: int = 14
    ) -> None:
        self.layer, self.embedder, self.top = layer, embedder, top_columns
        self.docs = layer.docs()
        self._vecs: list[list[float]] | None = None

    async def warm(self) -> None:
        if self._vecs is None:
            self._vecs = await self.embedder.embed([d[2] for d in self.docs])

    async def link(self, question: str, qvec: list[float] | None = None) -> Linked:
        await self.warm()
        if self._vecs is None:
            raise RuntimeError("schema docs not embedded")
        q = qvec or (await self.embedder.embed([question]))[0]
        scored = sorted(((cosine(q, v), i) for i, v in enumerate(self._vecs)), reverse=True)
        tables: set[str] = set()
        cols: set[tuple[str, str]] = set()
        for _, i in scored[: self.top]:
            t, c, _ = self.docs[i]
            tables.add(t)
            if c:
                cols.add((t, c))
        tables = self.layer.connect(tables)
        return Linked(
            tables,
            cols,
            [(round(s, 3), f"{self.docs[i][0]}.{self.docs[i][1]}") for s, i in scored[:8]],
        )


@dataclass(frozen=True)
class Example:
    id: str
    question: str
    sql: str


class ExampleStore:
    """Verified question/SQL pairs for few-shot prompting. Built from the DEV split only; the
    eval excludes the question being asked (leave-one-out), and a test asserts no test-split
    question is in the store."""

    def __init__(self, examples: list[Example], embedder: CachedEmbedder) -> None:
        self.examples, self.embedder = examples, embedder
        self._vecs: list[list[float]] | None = None

    async def search(
        self, qvec: list[float], k: int, exclude: set[str] | None = None
    ) -> list[Example]:
        if not self.examples or k <= 0:
            return []
        if self._vecs is None:
            self._vecs = await self.embedder.embed([e.question for e in self.examples])
        scored = sorted(((cosine(qvec, v), i) for i, v in enumerate(self._vecs)), reverse=True)
        out = [self.examples[i] for _, i in scored if self.examples[i].id not in (exclude or set())]
        return out[:k]


def load_examples(rows: list[dict[str, Any]]) -> list[Example]:
    return [Example(r["id"], r["question"], r["sql"]) for r in rows if r.get("sql")]
