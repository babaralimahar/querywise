"""Validate and freeze the gold set.

    uv run python -m querywise.evals.build

Checks every gold query: passes the guard (no false blocks), executes as analyst_ro, returns
rows without NULL-only answers, and for top-N questions has no tie at the cut-off (a tie would
make several answers correct). Then writes a stratified dev/test split with SHA256SUMS.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine

from app.config import normalize_database_url
from app.evals.dataset import write_sums
from querywise.config import get_query_settings
from querywise.evals.goldset import ADVERSARIAL, AMBIGUOUS, QUESTIONS
from querywise.executor import assert_role, execute
from querywise.guard import SqlGuard
from querywise.semantic import load_semantic_layer

OUT = Path("evals/datasets/querywise")


def items() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, (tier, q, sql) in enumerate(QUESTIONS):
        out.append(
            {
                "id": f"q{i:03d}",
                "kind": "answerable",
                "tier": tier,
                "question": q,
                "sql": sql,
                "ordered": False,
            }
        )
    for i, q in enumerate(AMBIGUOUS):
        out.append(
            {
                "id": f"amb{i:02d}",
                "kind": "ambiguous",
                "tier": "ambiguous",
                "question": q,
                "sql": None,
                "ordered": False,
            }
        )
    for i, q in enumerate(ADVERSARIAL):
        out.append(
            {
                "id": f"adv{i:02d}",
                "kind": "adversarial",
                "tier": "adversarial",
                "question": q,
                "sql": None,
                "ordered": False,
            }
        )
    return out


def split(
    xs: list[dict[str, Any]], seed: int = 4
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(seed)
    dev, test = [], []
    for tier in sorted({x["tier"] for x in xs}):
        group = [x for x in xs if x["tier"] == tier]
        rng.shuffle(group)
        k = round(len(group) * 0.3)
        test += group[:k]
        dev += group[k:]
    return sorted(dev, key=lambda x: x["id"]), sorted(test, key=lambda x: x["id"])


async def validate(xs: list[dict[str, Any]]) -> list[str]:
    s = get_query_settings()
    guard = SqlGuard(load_semantic_layer(s.semantic_layer_path), max_rows=s.max_rows)
    eng = create_async_engine(normalize_database_url(s.analytics_database_url))
    problems: list[str] = []
    try:
        await assert_role(eng)
        for x in xs:
            if not x["sql"]:
                continue
            g = guard.check(x["sql"])
            if not g.ok or g.sql is None:
                problems.append(f"{x['id']}: FALSE BLOCK {g.reason} {g.detail}")
                continue
            r = await execute(eng, g.sql, cost_ceiling=s.explain_cost_ceiling)
            x["gold_rows"] = len(r.rows)
            x["gold_cost"] = r.plan_cost
            if not r.rows or all(v is None for row in r.rows for v in row):
                problems.append(f"{x['id']}: empty or NULL result")
            if x["tier"] == "topn" and " LIMIT " in x["sql"]:
                n = int(x["sql"].rsplit(" LIMIT ", 1)[1])
                full = await execute(eng, x["sql"].rsplit(" LIMIT ", 1)[0] + f" LIMIT {n + 1}")
                if len(full.rows) > n and full.rows[n][-1] == full.rows[n - 1][-1]:
                    problems.append(f"{x['id']}: tie at the top-{n} cut-off")
    finally:
        await eng.dispose()
    return problems


def main() -> int:
    xs = items()
    counts = {t: sum(x["tier"] == t for x in xs) for t in sorted({x["tier"] for x in xs})}
    print("items:", counts, "total", len(xs))
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    problems = asyncio.run(validate(xs))
    costs = sorted(x.get("gold_cost", 0) for x in xs)
    print(f"gold plan cost: max {costs[-1]:,.0f}, p50 {costs[len(costs) // 2]:,.0f}")
    if problems:
        print("\n".join(problems))
        return 1
    dev, test = split(xs)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, part in (("dev", dev), ("test", test)):
        (OUT / f"{name}.jsonl").write_text(
            "".join(
                json.dumps({"id": x["id"], "input": x["question"], "expected": x}, sort_keys=True)
                + "\n"
                for x in part
            ),
            encoding="utf-8",
            newline="\n",
        )
    write_sums(OUT)
    print(
        {
            n: {
                k: sum(x["kind"] == k for x in p)
                for k in ("answerable", "ambiguous", "adversarial")
            }
            for n, p in (("dev", dev), ("test", test))
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
