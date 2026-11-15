"""Presentation: deterministic chart choice, a compact result digest for the summary, and a
number verifier that rejects summaries containing numbers not present in/derivable from the
result. The LLM only chooses words; the chart and every number come from the data."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

VEGA = "https://vega.github.io/schema/vega-lite/v5.json"
_NUM = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.])-?\d+(?:\.\d+)?%?")
TIME_NAMES = {"month", "year", "quarter", "day", "week", "date", "hour", "dow"}


def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


def _kind(col: str, values: list[Any]) -> str:
    vals = [v for v in values if v is not None]
    if vals and all(isinstance(v, datetime | date) for v in vals):
        return "temporal"
    if vals and all(isinstance(v, int | float | Decimal) and not isinstance(v, bool) for v in vals):
        return "ordinal" if col.lower() in TIME_NAMES else "quantitative"
    return "nominal"


def chart_spec(columns: list[str], rows: list[tuple[Any, ...]]) -> dict[str, Any] | None:
    """time + number -> line; category + number -> bar; anything else -> table only."""
    if len(rows) < 2 or len(columns) != 2:
        return None
    kinds = [_kind(c, [r[i] for r in rows]) for i, c in enumerate(columns)]
    data = [{c: jsonable(r[i]) for i, c in enumerate(columns)} for r in rows]
    x, y = columns
    if kinds[1] != "quantitative":
        return None
    if kinds[0] in ("temporal", "ordinal"):
        return {
            "$schema": VEGA,
            "mark": {"type": "line", "point": True},
            "data": {"values": data},
            "encoding": {
                "x": {"field": x, "type": kinds[0]},
                "y": {"field": y, "type": "quantitative"},
            },
        }
    if kinds[0] == "nominal" and len(rows) <= 40:
        return {
            "$schema": VEGA,
            "mark": "bar",
            "data": {"values": data},
            "encoding": {
                "y": {"field": x, "type": "nominal", "sort": "-x"},
                "x": {"field": y, "type": "quantitative"},
            },
        }
    return None


def digest(columns: list[str], rows: list[tuple[Any, ...]], max_rows: int = 20) -> dict[str, Any]:
    out: dict[str, Any] = {
        "columns": columns,
        "row_count": len(rows),
        "rows": [[jsonable(v) for v in r] for r in rows[:max_rows]],
    }
    stats = {}
    for i, c in enumerate(columns):
        nums = [
            float(r[i])
            for r in rows
            if isinstance(r[i], int | float | Decimal) and not isinstance(r[i], bool)
        ]
        if len(nums) > 1:
            stats[c] = {
                "sum": sum(nums),
                "min": min(nums),
                "max": max(nums),
                "mean": sum(nums) / len(nums),
            }
    if stats:
        out["stats"] = stats
    return out


def _parse(tok: str) -> tuple[float, int, bool]:
    pct = tok.endswith("%")
    s = tok.rstrip("%").replace(",", "")
    dec = len(s.split(".")[1]) if "." in s else 0
    return float(s), dec, pct


def allowed_numbers(
    columns: list[str], rows: list[tuple[Any, ...]], question: str = ""
) -> set[float]:
    vals: set[float] = {float(len(rows)), float(len(columns))}
    for r in rows:
        for v in r:
            if isinstance(v, int | float | Decimal) and not isinstance(v, bool):
                vals.add(float(v))
            elif isinstance(v, datetime | date):
                vals.update({float(v.year), float(v.month), float(v.day)})
            elif isinstance(v, str):
                vals.update(float(m) for m in re.findall(r"\d+(?:\.\d+)?", v))
    for st in digest(columns, rows, 0).get("stats", {}).values():
        vals.update(st.values())
    vals.update(float(m) for m in re.findall(r"\d+(?:\.\d+)?", question.replace(",", "")))
    return vals


def unverified_numbers(
    summary: str, columns: list[str], rows: list[tuple[Any, ...]], question: str = ""
) -> list[str]:
    """Numbers in the summary that are not in/derivable from the result (rounding-aware:
    a number written with d decimals matches a value rounded to d decimals; a percentage
    matches a value or value*100)."""
    allowed = allowed_numbers(columns, rows, question)
    bad = []
    for m in _NUM.finditer(summary):
        tok = m.group(0)
        x, dec, pct = _parse(tok)
        cands = {a for a in allowed} | ({a * 100 for a in allowed} if pct else set())
        if not any(round(c, dec) == round(x, dec) or abs(c - x) <= 1e-9 for c in cands):
            bad.append(tok)
    return bad


def template_summary(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    if not rows:
        return "The query returned no rows for these filters."
    if len(rows) == 1 and len(columns) == 1:
        return f"Result: {fmt(rows[0][0])}."
    if len(rows) == 1:
        return (
            "Result: "
            + ", ".join(f"{c} = {fmt(v)}" for c, v in zip(columns, rows[0], strict=True))
            + "."
        )
    return f"The query returned {len(rows)} rows with columns {', '.join(columns)}."


def fmt(v: Any) -> str:
    if isinstance(v, float | Decimal):
        return f"{float(v):,.2f}"
    return str(jsonable(v))
