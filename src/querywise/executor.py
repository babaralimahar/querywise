"""Runs guarded SQL as analyst_ro: EXPLAIN cost ceiling first, then a read-only transaction with
search_path pinned to the curated schema and a statement timeout.

The engine passed in MUST be the analytics (analyst_ro) engine; `assert_role` is called at
startup and in tests, so a misconfigured DSN fails loudly instead of running as the owner.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

ANALYST_ROLE = "analyst_ro"


class ExecutionError(Exception):
    def __init__(self, message: str, kind: str = "db_error") -> None:
        super().__init__(message)
        self.kind = kind  # db_error | timeout | cost


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    db_ms: int
    plan_cost: float


async def assert_role(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        who = (await c.execute(text("SELECT current_user"))).scalar_one()
    if who != ANALYST_ROLE:
        raise RuntimeError(f"analytics engine connects as {who!r}, expected {ANALYST_ROLE!r}")


def _clean_error(e: DBAPIError) -> str:
    msg = str(e.orig) if e.orig is not None else str(e)
    msg = msg.split("\n[SQL:")[0]
    return msg.replace("<class 'asyncpg.exceptions.", "").replace("'>:", ":")[:400]


async def execute(
    engine: AsyncEngine,
    sql: str,
    *,
    timeout_ms: int = 5000,
    cost_ceiling: float | None = None,
    max_rows: int = 500,
) -> QueryResult:
    t0 = time.perf_counter()
    async with engine.connect() as c:
        tx = await c.begin()
        try:
            await c.execute(text("SET TRANSACTION READ ONLY"))
            await c.execute(text("SET LOCAL search_path = curated"))
            await c.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            try:
                plan = (await c.execute(text("EXPLAIN (FORMAT JSON) " + sql))).scalar_one()
            except DBAPIError as e:
                raise ExecutionError(_clean_error(e)) from e
            plan_doc = plan if isinstance(plan, list) else __import__("json").loads(plan)
            cost = float(plan_doc[0]["Plan"]["Total Cost"])
            if cost_ceiling is not None and cost > cost_ceiling:
                raise ExecutionError(
                    f"estimated cost {cost:,.0f} exceeds the ceiling {cost_ceiling:,.0f}; add filters or aggregate",
                    kind="cost",
                )
            try:
                res = await c.execute(text(sql))
            except DBAPIError as e:
                msg = _clean_error(e)
                kind = "timeout" if "statement timeout" in msg else "db_error"
                raise ExecutionError(msg, kind=kind) from e
            columns = list(res.keys())
            rows = [tuple(r) for r in res.fetchmany(max_rows)]
        finally:
            await tx.rollback()  # read-only anyway; never leave anything open
    return QueryResult(columns, rows, int((time.perf_counter() - t0) * 1000), cost)
