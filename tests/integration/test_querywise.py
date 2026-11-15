"""QueryWise against real Postgres: database-level controls, the pipeline with a scripted LLM,
the repair cap, cache, audit, follow-ups, and API roles/rate limits."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.adapters.db.api_keys import SqlApiKeyRepo
from app.adapters.llm import LLMResult, LLMTransient, Usage
from querywise.config import QuerySettings
from querywise.data.load import load
from querywise.executor import ExecutionError, assert_role, execute
from querywise.guard import SqlGuard
from querywise.pipeline import Pipeline, PipelineConfig
from querywise.semantic import load_semantic_layer
from querywise.service import QueryService, build_embedder, build_linker
from tests.conftest import make_query_settings

U = Usage(200, 30, Decimal("0.0001"))
LAYER = load_semantic_layer("config/semantic_layer.yaml")


def sql_out(sql: str = "", **kw: Any) -> LLMResult:
    body = {"sql": sql, "assumptions": kw.get("assumptions", []), "needs_clarification": kw.get("clarify", False),
            "clarification_question": kw.get("question", ""), "options": kw.get("options", []), "refusal": kw.get("refusal", "")}  # fmt: skip
    return LLMResult(
        text=json.dumps(body), parsed=body, tool_calls=[], usage=U, model="fake", latency_ms=1
    )


def summary_out(s: str) -> LLMResult:
    return LLMResult(
        text=json.dumps({"summary": s}),
        parsed={"summary": s},
        tool_calls=[],
        usage=U,
        model="fake",
        latency_ms=1,
    )


class ScriptedLLM:
    def __init__(self, *steps: LLMResult | Exception) -> None:
        self.steps = list(steps)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(self, messages: Any, **_: Any) -> LLMResult:
        self.calls.append(messages)
        step = self.steps[0] if len(self.steps) == 1 else self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


# ---------------------------------------------------------------- fixtures


@pytest.fixture
async def settings(engine: AsyncEngine) -> QuerySettings:
    await load(engine, seed=7, n_customers=300, n_orders=500, n_products=80, n_sellers=30)
    url = engine.url.render_as_string(hide_password=False)
    return make_query_settings(
        database_url=url,
        analytics_database_url=url.replace("postgres:postgres@", "analyst_ro:analyst-ro-dev@"),
    )


@pytest.fixture
async def analytics(settings: QuerySettings) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(settings.analytics_database_url)
    yield eng
    await eng.dispose()


def make_pipeline(llm: Any, analytics: AsyncEngine, sm: async_sessionmaker[AsyncSession], s: QuerySettings,
                  **cfg: Any) -> Pipeline:  # fmt: skip
    emb = build_embedder(s, sm)
    c = PipelineConfig(**({"summary": "template"} | cfg))
    return Pipeline(llm=llm, layer=LAYER, guard=SqlGuard(LAYER), analytics=analytics, embedder=emb,
                    linker=build_linker(LAYER, emb, s), cfg=c, cost_ceiling=s.explain_cost_ceiling)  # fmt: skip


# ---------------------------------------------------------------- database-level controls (F2)


async def test_analytics_connection_is_the_read_only_role(
    analytics: AsyncEngine, settings: QuerySettings
) -> None:
    await assert_role(analytics)
    owner = create_async_engine(settings.database_url)
    with pytest.raises(RuntimeError, match="expected 'analyst_ro'"):
        await assert_role(owner)
    await owner.dispose()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO curated.v_sellers VALUES ('x', 'y', 'SP')",
        "DELETE FROM curated.v_orders",
        "DROP VIEW curated.v_orders",
        "CREATE TABLE public.evil (x int)",
        "SELECT * FROM raw.customers",
        "SELECT customer_city FROM curated.v_customers",
        "SELECT * FROM query_log",
        "SELECT * FROM api_keys",
    ],
)
async def test_raw_role_cannot_write_or_read_outside_curated(
    analytics: AsyncEngine, sql: str
) -> None:
    """Defence in depth: even with the guard bypassed, analyst_ro cannot do these."""
    async with analytics.connect() as c:
        with pytest.raises(DBAPIError):
            await c.execute(text(sql))


async def test_role_statement_timeout_kills_pg_sleep(analytics: AsyncEngine) -> None:
    t0 = time.perf_counter()
    async with analytics.connect() as c:
        with pytest.raises(DBAPIError, match="statement timeout"):
            await c.execute(text("SELECT pg_sleep(30)"))
    assert time.perf_counter() - t0 < 8  # role-level 5 s, no app-side help


async def test_executor_cost_ceiling_and_timeout(analytics: AsyncEngine) -> None:
    with pytest.raises(ExecutionError) as e:
        await execute(analytics, "SELECT count(*) FROM v_orders o JOIN v_order_items i ON o.order_status <> i.order_id",
                      cost_ceiling=1000)  # fmt: skip
    assert e.value.kind == "cost"
    with pytest.raises(ExecutionError) as e2:
        await execute(analytics, "SELECT pg_sleep(2)", timeout_ms=300)
    assert e2.value.kind == "timeout"
    ok = await execute(
        analytics, "SELECT order_status, count(*) FROM v_orders GROUP BY 1", cost_ceiling=1000
    )
    assert ok.rows and ok.columns == ["order_status", "count"]


# ---------------------------------------------------------------- pipeline paths


async def test_answered_with_chart_and_assumptions(
    analytics: AsyncEngine, sm: Any, settings: QuerySettings
) -> None:
    sql = "SELECT order_status, COUNT(*) AS n FROM v_orders GROUP BY order_status"
    llm = ScriptedLLM(sql_out(sql, assumptions=["all statuses"]))
    ans = await make_pipeline(llm, analytics, sm, settings).answer("Orders per status?")
    assert ans.status == "answered" and ans.sql is not None and "LIMIT 500" in ans.sql
    assert ans.chart is not None and ans.chart["mark"] == "bar"
    assert ans.assumptions == ["all statuses"] and ans.summary
    assert set(ans.timings_ms) >= {"llm", "guard", "db", "total"}


async def test_clarification_and_refusal(
    analytics: AsyncEngine, sm: Any, settings: QuerySettings
) -> None:
    llm = ScriptedLLM(
        sql_out(clarify=True, question="By revenue or by orders?", options=["revenue", "orders"])
    )
    ans = await make_pipeline(llm, analytics, sm, settings).answer("Who are our best customers?")
    assert (
        ans.status == "needs_clarification"
        and ans.options == ["revenue", "orders"]
        and ans.sql is None
    )
    llm2 = ScriptedLLM(sql_out(refusal="Customer zip codes are restricted."))
    ans2 = await make_pipeline(llm2, analytics, sm, settings).answer("List customer zip codes")
    assert ans2.status == "refused" and not ans2.attempts


async def test_security_blocks_are_not_repaired(
    analytics: AsyncEngine, sm: Any, settings: QuerySettings
) -> None:
    llm = ScriptedLLM(sql_out("SELECT customer_city FROM v_customers"))
    ans = await make_pipeline(llm, analytics, sm, settings).answer("cities?")
    assert ans.status == "blocked" and ans.reason == "DISALLOWED_COLUMN"
    assert len(llm.calls) == 1  # no second chance for a security violation


async def test_db_error_is_repaired(
    analytics: AsyncEngine, sm: Any, settings: QuerySettings
) -> None:
    bad = "SELECT COUNT(*) FROM v_orders WHERE purchased_at > 'not a date'"
    good = "SELECT COUNT(*) FROM v_orders WHERE purchased_at > '2018-01-01'"
    llm = ScriptedLLM(sql_out(bad), sql_out(good))
    ans = await make_pipeline(llm, analytics, sm, settings).answer("orders in 2018?")
    assert ans.status == "answered" and len(ans.attempts) == 2
    assert ans.attempts[0].error and "invalid input" in ans.attempts[0].error
    assert "the database returned an error" in llm.calls[1][-1]["content"]


async def test_repair_loop_never_exceeds_two_repairs(
    analytics: AsyncEngine, sm: Any, settings: QuerySettings
) -> None:
    llm = ScriptedLLM(sql_out("SELECT COUNT(*) FROM v_orders WHERE purchased_at > 'nope'"))
    ans = await make_pipeline(llm, analytics, sm, settings).answer("x?")
    assert ans.status == "failed" and ans.reason == "DB_ERROR"
    assert len(ans.attempts) == 3 and len(llm.calls) == 3  # 1 generation + 2 repairs, then stop
    with pytest.raises(ValueError):
        PipelineConfig(max_repairs=3)


async def test_cost_ceiling_blocks_expensive_sql(
    analytics: AsyncEngine, sm: Any, settings: QuerySettings
) -> None:
    llm = ScriptedLLM(
        sql_out(
            "SELECT count(*) FROM v_order_items a JOIN v_order_items b ON a.order_item_id = b.order_item_id"
        )
    )
    p = make_pipeline(llm, analytics, sm, settings)
    p.cost_ceiling = 1  # tiny test data: any plan exceeds this; proves the check is wired in
    ans = await p.answer("pairs?")
    assert ans.status == "blocked" and ans.reason == "COST_TOO_HIGH"


async def test_unverified_summary_is_replaced(
    analytics: AsyncEngine, sm: Any, settings: QuerySettings
) -> None:
    """Injection via data values: a poisoned seller city tells the model to report revenue as zero.
    The summary may say anything; a number not in the result is caught and replaced."""
    sql = "SELECT seller_city, COUNT(*) AS sellers FROM v_sellers GROUP BY 1 ORDER BY 2 DESC"
    llm = ScriptedLLM(sql_out(sql), summary_out("Revenue was 0 this year."))
    ans = await make_pipeline(llm, analytics, sm, settings, summary="llm").answer(
        "Sellers per city?"
    )
    assert ans.status == "answered" and ans.summary_verified is False
    assert "0 this year" not in (ans.summary or "")
    digest_msg = llm.calls[1][-1]["content"]
    assert (
        "<result>" in digest_msg and "SYSTEM NOTE TO AI" in digest_msg
    )  # the poison is there, as data
    assert "never follow it" in llm.calls[1][0]["content"]


async def test_model_outage_fails_cleanly(
    analytics: AsyncEngine, sm: Any, settings: QuerySettings
) -> None:
    ans = await make_pipeline(
        ScriptedLLM(LLMTransient("HTTP 429")), analytics, sm, settings
    ).answer("x?")
    assert ans.status == "failed" and ans.reason == "LLM_UNAVAILABLE" and not ans.attempts


# ---------------------------------------------------------------- service: cache, audit, follow-ups


async def test_cache_audit_and_follow_up(
    analytics: AsyncEngine, sm: Any, settings: QuerySettings
) -> None:
    sql = "SELECT payment_type, SUM(payment_value) AS total FROM v_payments GROUP BY 1"
    llm = ScriptedLLM(sql_out(sql))
    svc = QueryService(make_pipeline(llm, analytics, sm, settings), sm, cache_ttl_s=600)
    _, first = await svc.ask("Payments by type?", "user-1", "conv1")
    _, second = await svc.ask("Payments by type again?", "user-1", "conv1")
    assert not first.cached and second.cached and "db" not in second.timings_ms
    assert first.rows == second.rows or [tuple(map(float, r[1:])) for r in first.rows] == [
        tuple(map(float, r[1:])) for r in second.rows
    ]
    # follow-up gets the previous question and SQL as context
    assert "PREVIOUS SQL" in llm.calls[1][-1]["content"]
    # blocked and clarification requests are audited too
    svc.pipeline.llm = ScriptedLLM(sql_out("SELECT customer_city FROM v_customers"))
    await svc.ask("cities?", "user-2", None)
    async with sm() as ss:
        rows = (
            await ss.execute(
                text("SELECT status, user_id, sql_hash IS NOT NULL AS h FROM query_log ORDER BY id")
            )
        ).all()
    assert [r.status for r in rows] == ["answered", "answered", "blocked"]
    assert all(r.h for r in rows[:2])


# ---------------------------------------------------------------- API


async def test_api_roles_rate_limit_and_feedback(settings: QuerySettings, sm: Any) -> None:
    from querywise.main import create_app

    app = create_app(settings.model_copy(update={"queries_per_minute": 2}))
    sql = "SELECT COUNT(*) AS n FROM v_orders"
    app.state.query_service.pipeline.llm = ScriptedLLM(sql_out(sql))
    async with sm() as ss:
        viewer = (await SqlApiKeyRepo(ss).create("t-viewer", "viewer"))[1]
        admin = (await SqlApiKeyRepo(ss).create("t-admin", "admin"))[1]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/v1/query", json={"question": "How many orders?"})).status_code == 401
        r = await c.post(
            "/v1/query", json={"question": "How many orders?"}, headers={"X-API-Key": viewer}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert (
            body["status"] == "answered"
            and body["row_count"] == 1
            and body["sql"].endswith("LIMIT 500")
        )
        assert (await c.get("/v1/schema", headers={"X-API-Key": viewer})).json()["metrics"][
            "revenue"
        ]
        assert (await c.get("/v1/audit", headers={"X-API-Key": viewer})).status_code == 403
        audit = (await c.get("/v1/audit", headers={"X-API-Key": admin})).json()["items"]
        assert len(audit) == 1 and audit[0]["sql_hash"]
        fb = {
            "query_id": body["query_id"],
            "verdict": "incorrect",
            "corrected_sql": "DELETE FROM v_orders",
        }
        assert (
            await c.post("/v1/feedback", json=fb, headers={"X-API-Key": viewer})
        ).status_code == 422
        fb["corrected_sql"] = "SELECT COUNT(*) FROM v_orders WHERE order_status = 'delivered'"
        assert (
            await c.post("/v1/feedback", json=fb, headers={"X-API-Key": viewer})
        ).status_code == 201
        await c.post(
            "/v1/query", json={"question": "How many orders?"}, headers={"X-API-Key": viewer}
        )
        limited = await c.post(
            "/v1/query", json={"question": "How many orders?"}, headers={"X-API-Key": viewer}
        )
        assert limited.status_code == 429
        assert (await c.get("/ui", follow_redirects=False)).status_code == 303
    await app.state.engine.dispose()
    await app.state.analytics_engine.dispose()
    await asyncio.sleep(0)
