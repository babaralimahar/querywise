"""Execution-accuracy comparator: are two result sets the same answer?

- Rows are a multiset (order ignored) unless `ordered=True`.
- Column order and column names are ignored: every gold column must match a distinct predicted
  column with the same multiset of values. Extra predicted columns are allowed (an extra label
  column next to the right numbers is still the right answer).
- Numbers match within 1e-6 relative tolerance, or when equal after rounding the more precise
  one to the other's decimals (2 at most) - so 0.0654 vs 6.54% is NOT equal but 337.82 vs
  337.823 is.
- Timestamps/dates compare on their ISO form, with a date equal to the same timestamp at midnight.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from itertools import permutations
from typing import Any


def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, Decimal | int | float):
        return float(v)
    if isinstance(v, datetime):
        return v.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day).isoformat(sep=" ")
    return str(v)


def _decimals(x: float) -> int:
    s = repr(x)
    return len(s.split(".")[1]) if "." in s and "e" not in s else 0


def num_equal(a: float, b: float) -> bool:
    if math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9):
        return True
    da, db = _decimals(a), _decimals(b)
    d = min(da, db)
    if d <= 2 and da != db:
        return round(a, d) == round(b, d)
    return False


def val_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return num_equal(a, b)
    return bool(a == b)


def _key(v: Any) -> tuple[int, Any]:
    if v is None:
        return (0, 0)
    if isinstance(v, float):
        return (1, round(v, 4))
    return (2, str(v))


def _col_equal(a: list[Any], b: list[Any], ordered: bool) -> bool:
    if len(a) != len(b):
        return False
    if not ordered:
        a, b = sorted(a, key=_key), sorted(b, key=_key)
    return all(val_equal(x, y) for x, y in zip(a, b, strict=True))


def _rows_equal(gold: list[tuple[Any, ...]], pred: list[tuple[Any, ...]], ordered: bool) -> bool:
    if len(gold) != len(pred):
        return False
    if not ordered:
        gold = sorted(gold, key=lambda r: tuple(_key(v) for v in r))
        pred = sorted(pred, key=lambda r: tuple(_key(v) for v in r))
    return all(
        all(val_equal(x, y) for x, y in zip(g, p, strict=True))
        for g, p in zip(gold, pred, strict=True)
    )


def results_match(
    gold: Sequence[Sequence[Any]], pred: Sequence[Sequence[Any]], *, ordered: bool = False
) -> bool:
    g = [tuple(_norm(v) for v in r) for r in gold]
    p = [tuple(_norm(v) for v in r) for r in pred]
    if len(g) != len(p):
        return False
    if not g:
        return True
    ng, np_ = len(g[0]), len(p[0])
    if np_ < ng:
        return False
    gcols = [[r[i] for r in g] for i in range(ng)]
    pcols = [[r[j] for r in p] for j in range(np_)]
    # candidate predicted columns for each gold column (column-wise multiset equality)
    cands = [[j for j in range(np_) if _col_equal(gcols[i], pcols[j], ordered)] for i in range(ng)]
    if any(not c for c in cands):
        return False
    # try assignments (tiny: results have a handful of columns); rows must match jointly
    if np_ > 8:  # keep the search bounded
        assigns = [list(range(ng))] if all(i in cands[i] for i in range(ng)) else []
    else:
        assigns = [
            list(pm)
            for pm in permutations(range(np_), ng)
            if all(pm[i] in cands[i] for i in range(ng))
        ]
    for a in assigns:
        proj = [tuple(r[j] for j in a) for r in p]
        if _rows_equal(g, proj, ordered):
            return True
    return False


def multiset(rows: Sequence[Sequence[Any]]) -> Counter[tuple[Any, ...]]:
    return Counter(tuple(_norm(v) for v in r) for r in rows)
