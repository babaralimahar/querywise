"""QueryWise HTTP API. Roles (template API keys): viewer = analyst, admin = audit access."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.api.auth import Principal, require_role
from app.api.deps import SessionDep
from app.api.errors import DomainError
from querywise.guard import SqlGuard
from querywise.pipeline import Answer
from querywise.present import jsonable
from querywise.service import QueryService

router = APIRouter(prefix="/v1", tags=["query"])
Analyst = Annotated[Principal, Depends(require_role("viewer"))]
Admin = Annotated[Principal, Depends(require_role("admin"))]


class RateLimiter:
    """Sliding one-minute window per API key (in memory: one process; Redis for several)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            raise DomainError(
                429, "rate-limited", f"More than {self.per_minute} queries per minute"
            )
        q.append(now)


def service(request: Request) -> QueryService:
    return cast(QueryService, request.app.state.query_service)


def answer_json(qid: int, ans: Answer, conversation_id: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "query_id": qid,
        "conversation_id": conversation_id,
        "status": ans.status,
    }
    if ans.status == "needs_clarification":
        return base | {
            "question": ans.clarification_question,
            "options": ans.options,
            "assumptions": ans.assumptions,
        }
    if ans.status in ("blocked", "refused", "failed"):
        return base | {"reason": ans.reason, "detail": ans.detail, "sql": ans.sql}
    return base | {
        "sql": ans.sql,
        "assumptions": ans.assumptions,
        "columns": ans.columns,
        "rows": [[jsonable(v) for v in r] for r in ans.rows],
        "row_count": len(ans.rows),
        "chart": ans.chart,
        "summary": ans.summary,
        "summary_verified": ans.summary_verified,
        "warnings": [ans.detail] if ans.detail else [],
        "cached": ans.cached,
        "timings_ms": ans.timings_ms,
    }


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=1000)
    conversation_id: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")


@router.post("/query")
async def query(body: QueryIn, request: Request, p: Analyst) -> dict[str, Any]:
    request.app.state.rate_limiter.check(p.key_id)
    conv = body.conversation_id or uuid.uuid4().hex[:16]
    qid, ans = await service(request).ask(body.question, p.key_id, conv)
    return answer_json(qid, ans, conv)


@router.get("/schema")
async def schema(request: Request, _: Analyst) -> dict[str, Any]:
    layer = service(request).pipeline.layer
    return {
        "schema": layer.schema,
        "tables": {
            t: {"description": s["description"], "columns": s["columns"]}
            for t, s in layer.tables.items()
        },
        "metrics": {
            m.name: {"description": m.description, "sql": m.render()}
            for m in layer.metrics.values()
        },
        "restricted_columns": layer.restricted,
    }


@router.get("/audit")
async def audit(
    session: SessionDep, _: Admin, since_id: int = 0, limit: int = 100
) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, user_id, conversation_id, question, status, reason, sql_hash, sql, row_count, attempts, "
                    "cached, timings_ms, cost_usd, created_at FROM query_log WHERE id > :s ORDER BY id LIMIT :n"
                ),
                {"s": since_id, "n": min(max(limit, 1), 500)},
            )
        )
        .mappings()
        .all()
    )
    return {"items": [{k: jsonable(v) for k, v in r.items()} for r in rows]}


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query_id: int
    verdict: Literal["correct", "incorrect"]
    corrected_sql: str | None = Field(None, max_length=5000)
    note: str | None = Field(None, max_length=1000)


@router.post("/feedback", status_code=201)
async def feedback(
    body: FeedbackIn, request: Request, session: SessionDep, p: Analyst
) -> dict[str, Any]:
    if body.corrected_sql:
        g: SqlGuard = service(request).pipeline.guard
        res = g.check(body.corrected_sql)
        if not res.ok:
            raise DomainError(
                422, "unsafe-sql", f"corrected SQL rejected by the guard: {res.reason}"
            )
    exists = (
        await session.execute(text("SELECT 1 FROM query_log WHERE id = :q"), {"q": body.query_id})
    ).first()
    if exists is None:
        raise DomainError(404, "not-found", "Unknown query_id")
    fid = (
        await session.execute(
            text(
                "INSERT INTO feedback (query_id, user_id, verdict, corrected_sql, note) "
                "VALUES (:q, :u, :v, :s, :n) RETURNING id"
            ),
            {
                "q": body.query_id,
                "u": p.key_id,
                "v": body.verdict,
                "s": body.corrected_sql,
                "n": body.note,
            },
        )
    ).scalar_one()
    await session.commit()
    # Corrected SQL becomes a few-shot example only after a data-team review (not automatic).
    return {"feedback_id": fid, "status": "recorded"}
