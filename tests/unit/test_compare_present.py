"""Result comparator, chart rules, number verifier, semantic layer, few-shot leakage guarantee."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from querywise.evals.compare import num_equal, results_match
from querywise.present import chart_spec, template_summary, unverified_numbers
from querywise.semantic import load_semantic_layer

# ---------------------------------------------------------------- comparator


def test_row_order_is_ignored_unless_ordered() -> None:
    g = [("a", 1), ("b", 2)]
    p = [("b", 2), ("a", 1)]
    assert results_match(g, p)
    assert not results_match(g, p, ordered=True)


def test_column_order_and_names_are_ignored() -> None:
    assert results_match([("a", 1), ("b", 2)], [(1, "a"), (2, "b")])


def test_extra_predicted_columns_are_allowed_but_missing_ones_are_not() -> None:
    assert results_match([(10,)], [("total", 10)])
    assert not results_match([("a", 10)], [(10,)])


def test_duplicates_count() -> None:
    assert not results_match([(1,), (1,), (2,)], [(1,), (2,), (2,)])
    assert results_match([(1,), (1,), (2,)], [(2,), (1,), (1,)])


def test_numeric_tolerance_and_types() -> None:
    assert results_match([(Decimal("337.8230795186"),)], [(337.82307951860001,)])
    assert results_match([(Decimal("337.8230795186"),)], [(337.82,)])  # rounded to 2 dp
    assert not results_match([(0.0654,)], [(6.54,)])  # a percentage is a different answer
    assert not results_match([(100.0,)], [(101.0,)])
    assert num_equal(1e9, 1e9 + 0.5)


def test_rows_must_match_jointly_not_just_columns() -> None:
    # same column multisets, different pairing
    assert not results_match([("a", 1), ("b", 2)], [("a", 2), ("b", 1)])


def test_dates_and_timestamps() -> None:
    assert results_match([(datetime(2017, 1, 1), 5)], [(date(2017, 1, 1), 5)])


def test_empty_results() -> None:
    assert results_match([], [])
    assert not results_match([(1,)], [])


# ---------------------------------------------------------------- presentation


def test_chart_rules() -> None:
    line = chart_spec(
        ["month", "revenue"],
        [(datetime(2018, 1, 1), Decimal(10)), (datetime(2018, 2, 1), Decimal(12))],
    )
    assert line is not None and line["mark"]["type"] == "line"
    bar = chart_spec(["category", "revenue"], [("toys", 10.0), ("baby", 12.0)])
    assert bar is not None and bar["mark"] == "bar"
    assert chart_spec(["revenue"], [(10,)]) is None  # single value: KPI, no chart
    assert chart_spec(["a", "b", "c"], [(1, 2, 3), (4, 5, 6)]) is None  # 3 columns: table
    assert chart_spec(["year", "orders"], [(2017, 10), (2018, 12)])["mark"]["type"] == "line"  # type: ignore[index]
    json.dumps(line)  # spec must be serialisable


def test_number_verifier_accepts_rounding_percentages_and_stats() -> None:
    cols, rows = (
        ["category", "revenue"],
        [("toys", Decimal("1234.5678")), ("baby", Decimal("100.10"))],
    )
    assert unverified_numbers("Toys led with 1,234.57 BRL, baby had 100.1.", cols, rows) == []
    assert (
        unverified_numbers("Together 1334.67 across 2 categories.", cols, rows) == []
    )  # sum, row count
    rate = ["late_rate"], [(Decimal("0.0654"),)]
    assert unverified_numbers("6.54% of orders were late.", *rate) == []
    assert unverified_numbers("The rate was 0.07.", *rate) == []


def test_number_verifier_rejects_invented_numbers() -> None:
    cols, rows = ["category", "revenue"], [("toys", Decimal("1234.57"))]
    assert unverified_numbers("Revenue was 0 and grew 15% year on year.", cols, rows) == [
        "0",
        "15%",
    ]
    assert unverified_numbers("Toys made 1,300 BRL.", cols, rows) == ["1,300"]


def test_template_summary() -> None:
    assert template_summary(["n"], [(5,)]) == "Result: 5."
    assert "no rows" in template_summary(["n"], [])


# ---------------------------------------------------------------- semantic layer and datasets


def test_join_paths_connect_tables() -> None:
    layer = load_semantic_layer("config/semantic_layer.yaml")
    assert layer.connect({"v_products", "v_customers"}) == {
        "v_products",
        "v_order_items",
        "v_orders",
        "v_customers",
    }
    assert layer.connect({"v_sellers"}) == {"v_sellers"}


def test_restricted_columns_are_not_in_any_view() -> None:
    layer = load_semantic_layer("config/semantic_layer.yaml")
    assert not set(layer.restricted) & layer.all_columns


def test_metrics_and_schema_render() -> None:
    layer = load_semantic_layer("config/semantic_layer.yaml")
    assert len(layer.metrics) >= 10
    assert "SUM(oi.price)" in layer.render_metrics()
    assert "customer_city" not in layer.render_schema()


DATA = Path("evals/datasets/querywise")


@pytest.mark.skipif(not (DATA / "dev.jsonl").exists(), reason="datasets not built")
def test_few_shot_store_cannot_contain_test_questions() -> None:
    """Few-shot examples come from dev only; no test question or SQL can leak into prompts."""
    dev = [json.loads(line)["expected"] for line in (DATA / "dev.jsonl").read_text().splitlines()]
    test = [json.loads(line)["expected"] for line in (DATA / "test.jsonl").read_text().splitlines()]
    assert {x["id"] for x in dev}.isdisjoint({x["id"] for x in test})
    assert {x["question"] for x in dev}.isdisjoint({x["question"] for x in test})
    assert {x["sql"] for x in dev if x["sql"]}.isdisjoint({x["sql"] for x in test if x["sql"]})
