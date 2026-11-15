"""Embedding clients: OpenAI-compatible HTTP (Gemini) and a deterministic fake for tests."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.adapters.llm import LLMTransient


class EmbeddingError(Exception): ...


class Embedder(Protocol):
    dim: int
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def est_tokens(texts: list[str]) -> int:
    """The Gemini endpoint returns no usage for embeddings; chars/4 is the documented estimate."""
    return sum(len(t) for t in texts) // 4


class OpenAICompatEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        timeout_s: float = 30.0,
        batch: int = 16,
    ) -> None:
        self.model, self.dim, self._batch = model, dim, batch
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _one_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            r = await self._http.post(
                "/embeddings",
                headers=self._headers,
                json={"model": self.model, "input": texts, "dimensions": self.dim},
            )
        except httpx.HTTPError as e:
            raise LLMTransient(type(e).__name__) from e
        if r.status_code == 429 or r.status_code >= 500:
            raise LLMTransient(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise EmbeddingError(f"HTTP {r.status_code}: {r.text[:300]}")
        raw = r.json()["data"]
        # OpenAI returns an index per item; Gemini returns them in input order without one.
        data = sorted(raw, key=lambda d: d["index"]) if all("index" in d for d in raw) else raw
        vecs = [d["embedding"] for d in data]
        if len(vecs) != len(texts) or any(len(v) != self.dim for v in vecs):
            raise EmbeddingError("unexpected embedding shape")
        return vecs

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            part = texts[i : i + self._batch]
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception_type(LLMTransient),
                    stop=stop_after_attempt(8),
                    wait=wait_exponential_jitter(multiplier=2, max=30),
                    reraise=True,
                ):
                    with attempt:
                        vecs = await self._one_batch(part)
            except LLMTransient as e:
                raise EmbeddingError(f"embedding provider unavailable: {e}") from e
            out.extend(vecs)
        return out


_WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Hashed bag of words, L2-normalised: deterministic, offline, and similar texts really
    are close, so vector retrieval behaves sensibly in tests."""

    model = "fake"

    def __init__(self, dim: int = 768, fail: bool = False) -> None:
        self.dim, self.fail = dim, fail

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingError("fake embedder forced to fail")
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in _WORD.findall(t.lower()):
                h = int(hashlib.md5(w.encode(), usedforsecurity=False).hexdigest(), 16)
                v[h % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out
