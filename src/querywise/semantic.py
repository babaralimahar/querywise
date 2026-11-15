"""Semantic layer: loads config/semantic_layer.yaml and renders schema/metric context."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Metric:
    name: str
    description: str
    sql: str
    from_: str
    filter: str | None = None

    def render(self) -> str:
        where = f" WHERE {self.filter}" if self.filter else ""
        return f"- {self.name}: {self.description}\n  SELECT {self.sql} FROM {self.from_}{where}"  # noqa: S608  (prompt text, never executed)


@dataclass
class SemanticLayer:
    schema: str
    tables: dict[str, dict[str, Any]]
    restricted: dict[str, str]
    joins: list[tuple[str, str]]
    metrics: dict[str, Metric]
    max_rows: int = 500
    _graph: dict[str, set[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for a, b in self.joins:
            ta, tb = a.split(".")[0], b.split(".")[0]
            self._graph.setdefault(ta, set()).add(tb)
            self._graph.setdefault(tb, set()).add(ta)

    # ------------------------------------------------------------- allowlists for the guard
    def columns(self, table: str) -> set[str]:
        return set(self.tables[table]["columns"])

    @property
    def all_columns(self) -> set[str]:
        return {c for t in self.tables for c in self.columns(t)}

    @property
    def sensitive_tables(self) -> set[str]:
        return {t for t, spec in self.tables.items() if spec.get("sensitive")}

    # ------------------------------------------------------------- linking helpers
    def docs(self) -> list[tuple[str, str, str]]:
        """(table, column, text) for embedding: one document per column plus one per table."""
        out = []
        for t, spec in self.tables.items():
            out.append((t, "", f"table {t}: {spec['description']}"))
            for c, d in spec["columns"].items():
                out.append((t, c, f"{t}.{c}: {d}"))
        return out

    def connect(self, tables: set[str]) -> set[str]:
        """Add the tables on approved join paths needed to connect the chosen tables."""
        chosen = set(tables)
        if len(chosen) < 2:
            return chosen
        start = next(iter(sorted(chosen)))
        for target in sorted(chosen - {start}):
            prev: dict[str, str | None] = {start: None}
            q = deque([start])
            while q:
                cur = q.popleft()
                if cur == target:
                    break
                for nxt in sorted(self._graph.get(cur, ())):
                    if nxt not in prev:
                        prev[nxt] = cur
                        q.append(nxt)
            node: str | None = target if target in prev else None
            while node is not None:
                chosen.add(node)
                node = prev[node]
        return chosen

    # ------------------------------------------------------------- prompt context
    def render_schema(
        self, tables: set[str] | None = None, columns: set[tuple[str, str]] | None = None
    ) -> str:
        lines = []
        for t, spec in self.tables.items():
            if tables is not None and t not in tables:
                continue
            lines.append(f"{self.schema}.{t} -- {spec['description']}")
            for c, d in spec["columns"].items():
                keep = columns is None or (t, c) in columns or c.endswith("_id")
                if keep:
                    lines.append(f"  {c}: {d}")
        joins = [
            f"  {a} = {b}"
            for a, b in self.joins
            if tables is None or (a.split(".")[0] in tables and b.split(".")[0] in tables)
        ]
        if joins:
            lines.append("Approved joins:")
            lines += joins
        return "\n".join(lines)

    def render_metrics(self) -> str:
        return "\n".join(m.render() for m in self.metrics.values())

    def render_restricted(self) -> str:
        return "\n".join(f"- {c}: {why}" for c, why in self.restricted.items())


def load_semantic_layer(path: Path | str = "config/semantic_layer.yaml") -> SemanticLayer:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    metrics = {
        name: Metric(name, m["description"], m["sql"], m["from"], m.get("filter"))
        for name, m in raw["metrics"].items()
    }
    return SemanticLayer(
        schema=raw["schema"],
        tables=raw["tables"],
        restricted=raw.get("restricted", {}),
        joins=[(a, b) for a, b in raw["joins"]],
        metrics=metrics,
        max_rows=int(raw.get("max_rows", 500)),
    )
