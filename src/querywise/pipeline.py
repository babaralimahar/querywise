"""The bounded text-to-SQL pipeline (not an open agent):

link -> generate -> guard -> EXPLAIN cost -> execute (analyst_ro) -> [repair <= 2] -> present

Each added component (linking, semantic layer, repair, few-shot) is a flag, so the ablation
measures what it is worth.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.llm import LLMClient, LLMError, LLMResult, complete_with_retries
from querywise.executor import ExecutionError, QueryResult, execute
from querywise.guard import Reason, SqlGuard
from querywise.linking import CachedEmbedder, ExampleStore, SchemaLinker
from querywise.present import chart_spec, digest, template_summary, unverified_numbers
from querywise.semantic import SemanticLayer

Status = Literal["answered", "needs_clarification", "blocked", "refused", "failed", "empty"]
PROMPTS = Path("prompts")

SQL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "refusal": {"type": "string"},
    },
    "required": [
        "sql",
        "assumptions",
        "needs_clarification",
        "clarification_question",
        "options",
        "refusal",
    ],
}
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


class ResultCache(Protocol):
    async def cache_get(self, sql: str) -> dict[str, Any] | None: ...
    async def cache_put(
        self, sql: str, columns: list[str], rows: list[tuple[Any, ...]]
    ) -> None: ...


@dataclass
class PipelineConfig:
    use_linking: bool = True
    use_semantic_layer: bool = True
    max_repairs: int = 2
    few_shot_k: int = 0
    summary: Literal["llm", "template", "off"] = "llm"
    prompt_version: str = "sql_v1"

    def __post_init__(self) -> None:
        if not 0 <= self.max_repairs <= 2:
            raise ValueError("max_repairs must be between 0 and 2")


@dataclass
class Attempt:
    sql: str | None
    guard: str | None = None  # reason code if blocked
    error: str | None = None
    rows: int | None = None
    result: QueryResult | None = None


@dataclass
class Answer:
    status: Status
    question: str
    sql: str | None = None
    assumptions: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    chart: dict[str, Any] | None = None
    summary: str | None = None
    summary_verified: bool | None = None
    clarification_question: str | None = None
    options: list[str] = field(default_factory=list)
    reason: str | None = None
    detail: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    linked_tables: list[str] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cached: bool = False

    def add_usage(self, r: LLMResult) -> None:
        self.tokens_in += r.usage.tokens_in
        self.tokens_out += r.usage.tokens_out
        self.cost_usd += float(r.usage.cost_usd)

    def tick(self, name: str, t0: float) -> None:
        self.timings_ms[name] = self.timings_ms.get(name, 0) + int(
            (time.perf_counter() - t0) * 1000
        )


def _parse(res: LLMResult) -> dict[str, Any]:
    if res.parsed is not None:
        return res.parsed
    raw = (res.text or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return v if isinstance(v, dict) else {}


class Pipeline:
    def __init__(
        self,
        *,
        llm: LLMClient,
        layer: SemanticLayer,
        guard: SqlGuard,
        analytics: AsyncEngine,
        embedder: CachedEmbedder,
        linker: SchemaLinker,
        examples: ExampleStore | None = None,
        cfg: PipelineConfig | None = None,
        max_attempts: int = 3,
        timeout_ms: int = 5000,
        cost_ceiling: float = 50_000,
        max_rows: int = 500,
    ) -> None:
        self.llm, self.layer, self.guard, self.analytics = llm, layer, guard, analytics
        self.embedder, self.linker, self.examples = embedder, linker, examples
        self.cfg = cfg or PipelineConfig()
        self.max_attempts, self.timeout_ms = max_attempts, timeout_ms
        self.cost_ceiling, self.max_rows = cost_ceiling, max_rows
        self.sql_prompt = (PROMPTS / f"{self.cfg.prompt_version}.md").read_text(encoding="utf-8")
        self.summary_prompt = (PROMPTS / "summary_v1.md").read_text(encoding="utf-8")

    # ---------------------------------------------------------------- context
    async def _context(self, ans: Answer, question: str, exclude: set[str]) -> str:
        t0 = time.perf_counter()
        need_vec = self.cfg.use_linking or (self.cfg.few_shot_k > 0 and self.examples is not None)
        qvec = (await self.embedder.embed([question]))[0] if need_vec else None
        parts = []
        if self.cfg.use_linking:
            linked = await self.linker.link(question, qvec)
            ans.linked_tables = sorted(linked.tables)
            parts.append(
                "SCHEMA (linked to this question):\n" + self.layer.render_schema(linked.tables)
            )
        else:
            parts.append("SCHEMA:\n" + self.layer.render_schema())
        if self.cfg.use_semantic_layer:
            parts.append("METRICS (approved definitions):\n" + self.layer.render_metrics())
        parts.append("RESTRICTED (never select):\n" + self.layer.render_restricted())
        if self.cfg.few_shot_k and self.examples is not None and qvec is not None:
            exs = await self.examples.search(qvec, self.cfg.few_shot_k, exclude)
            if exs:
                parts.append(
                    "VERIFIED EXAMPLES:\n"
                    + "\n".join(f"Q: {e.question}\nSQL: {e.sql}" for e in exs)
                )
        ans.tick("link", t0)
        return "\n\n".join(parts)

    async def _llm(
        self, ans: Answer, messages: list[dict[str, Any]], schema: dict[str, Any], version: str
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            res = await complete_with_retries(
                self.llm,
                self.max_attempts,
                prompt_version=version,
                messages=messages,
                schema=schema,
            )
        finally:
            ans.tick("llm", t0)
        ans.add_usage(res)
        return _parse(res)

    # ---------------------------------------------------------------- main
    async def answer(
        self,
        question: str,
        *,
        previous: tuple[str, str] | None = None,
        exclude_examples: set[str] | None = None,
        cache: ResultCache | None = None,
    ) -> Answer:
        ans = Answer(status="failed", question=question)
        t_all = time.perf_counter()
        try:
            await self._run(ans, question, previous, exclude_examples or set(), cache)
        except LLMError as e:
            ans.status, ans.reason, ans.detail = (
                "failed",
                "LLM_UNAVAILABLE",
                f"model unavailable: {type(e).__name__}",
            )
        ans.tick("total", t_all)
        return ans

    async def _run(
        self,
        ans: Answer,
        question: str,
        previous: tuple[str, str] | None,
        exclude: set[str],
        cache: ResultCache | None,
    ) -> None:
        context = await self._context(ans, question, exclude)
        user = context
        if previous:
            user += f"\n\nPREVIOUS QUESTION IN THIS CONVERSATION: {previous[0]}\nPREVIOUS SQL: {previous[1]}"
        user += f"\n\n<question>{question}</question>"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.sql_prompt},
            {"role": "user", "content": user},
        ]
        out = await self._llm(ans, messages, SQL_SCHEMA, self.cfg.prompt_version)
        ans.assumptions = [str(a) for a in out.get("assumptions") or []][:10]

        if out.get("refusal"):
            ans.status, ans.reason, ans.detail = "refused", "REFUSED", str(out["refusal"])[:300]
            return
        if out.get("needs_clarification"):
            ans.status = "needs_clarification"
            ans.clarification_question = str(
                out.get("clarification_question") or "Could you be more specific?"
            )
            ans.options = [str(o) for o in out.get("options") or []][:4]
            return

        sql = str(out.get("sql") or "")
        for attempt in range(self.cfg.max_repairs + 1):
            att = Attempt(sql=sql)
            ans.attempts.append(att)
            t0 = time.perf_counter()
            g = self.guard.check(sql)
            ans.tick("guard", t0)
            if not g.ok or g.sql is None:
                att.guard = g.reason.value if g.reason else "BLOCKED"
                if g.repairable and attempt < self.cfg.max_repairs:
                    sql = await self._repair(
                        ans, messages, sql, f"guard rejected it: {g.reason}: {g.detail}"
                    )
                    continue
                ans.status, ans.reason, ans.detail = "blocked", att.guard, g.detail
                return
            att.sql = g.sql
            hit = await cache.cache_get(g.sql) if cache is not None else None
            if hit is not None:  # same normalised SQL ran recently: no DB round trip
                ans.cached = True
                ans.sql, ans.columns, ans.rows = (
                    g.sql,
                    list(hit["columns"]),
                    [tuple(r) for r in hit["rows"]],
                )
                att.rows = len(ans.rows)
                ans.status = "answered" if ans.rows else "empty"
                await self._present(ans, question)
                return
            t0 = time.perf_counter()
            try:
                res = await execute(
                    self.analytics,
                    g.sql,
                    timeout_ms=self.timeout_ms,
                    cost_ceiling=self.cost_ceiling,
                    max_rows=self.max_rows,
                )
            except ExecutionError as e:
                ans.tick("db", t0)
                att.error = str(e)
                if e.kind == "cost":
                    ans.status, ans.reason, ans.detail, ans.sql = (
                        "blocked",
                        Reason.COST_TOO_HIGH.value,
                        str(e),
                        g.sql,
                    )
                    return
                if e.kind == "db_error" and attempt < self.cfg.max_repairs:
                    sql = await self._repair(
                        ans, messages, g.sql, f"the database returned an error: {e}"
                    )
                    continue
                ans.status = "failed"
                ans.reason = "TIMEOUT" if e.kind == "timeout" else "DB_ERROR"
                ans.detail, ans.sql = str(e), g.sql
                return
            ans.tick("db", t0)
            att.rows, att.result = len(res.rows), res
            ans.sql, ans.columns, ans.rows = g.sql, res.columns, res.rows
            ans.status = "answered" if res.rows else "empty"
            if cache is not None:
                await cache.cache_put(g.sql, res.columns, res.rows)
            await self._present(ans, question)
            return

    async def _repair(
        self, ans: Answer, messages: list[dict[str, Any]], sql: str, problem: str
    ) -> str:
        messages.append({"role": "assistant", "content": json.dumps({"sql": sql})})
        messages.append(
            {
                "role": "user",
                "content": f"That SQL failed: {problem[:500]}\nReturn corrected JSON in the same format. Same rules apply.",
            }
        )
        out = await self._llm(ans, messages, SQL_SCHEMA, self.cfg.prompt_version + "+repair")
        return str(out.get("sql") or "")

    async def _present(self, ans: Answer, question: str) -> None:
        t0 = time.perf_counter()
        ans.chart = chart_spec(ans.columns, ans.rows)
        fallback = template_summary(ans.columns, ans.rows)
        if self.cfg.summary == "off":
            ans.summary = None
        elif self.cfg.summary == "template" or not ans.rows:
            ans.summary, ans.summary_verified = fallback, True
        else:
            msgs = [
                {"role": "system", "content": self.summary_prompt},
                {
                    "role": "user",
                    "content": f"<question>{question}</question>\n<result>{json.dumps(digest(ans.columns, ans.rows), default=str)[:6000]}</result>",
                },
            ]
            try:
                text_ = str(
                    (await self._llm(ans, msgs, SUMMARY_SCHEMA, "summary_v1")).get("summary") or ""
                )
            except LLMError:
                text_ = ""
            bad = (
                unverified_numbers(text_, ans.columns, ans.rows, question) if text_ else ["(empty)"]
            )
            if bad:
                ans.summary, ans.summary_verified = fallback, False
                ans.detail = f"summary replaced by template: unverified numbers {bad[:5]}"
            else:
                ans.summary, ans.summary_verified = text_, True
        ans.tick("present", t0)
