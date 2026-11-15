"""Load the synthetic warehouse into schema raw (owner connection; run after migrations).

uv run python -m querywise.data.load
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import normalize_database_url
from querywise.data.generate import LOAD_ORDER, generate


async def load(engine: AsyncEngine, seed: int = 42, **sizes: Any) -> dict[str, int]:
    ds = generate(seed, **sizes)
    async with engine.begin() as c:
        await c.execute(
            text(f"TRUNCATE {', '.join('raw.' + t for t in reversed(LOAD_ORDER))} CASCADE")
        )
        for name in LOAD_ORDER:
            rows = ds.tables[name]
            cols = list(rows[0])
            stmt = text(
                f"INSERT INTO raw.{name} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
            )
            for i in range(0, len(rows), 2000):
                await c.execute(stmt, rows[i : i + 2000])
    return {k: len(v) for k, v in ds.tables.items()}


async def _main() -> None:
    from app.config import Settings

    engine = create_async_engine(normalize_database_url(Settings().database_url))
    try:
        print(await load(engine))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_main())
