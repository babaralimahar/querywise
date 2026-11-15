"""SQL guard: 50 malicious strings must be blocked with the right reason; every gold query must
pass (0 false blocks); LIMIT is enforced."""

from __future__ import annotations

import pytest
import sqlglot

from querywise.evals.goldset import QUESTIONS
from querywise.guard import Reason, SqlGuard
from querywise.semantic import load_semantic_layer

G = SqlGuard(load_semantic_layer("config/semantic_layer.yaml"), max_rows=500)
R = Reason

MALICIOUS: list[tuple[str, Reason]] = [
    # multiple statements / smuggling
    ("SELECT 1; DROP TABLE v_orders", R.MULTIPLE_STATEMENTS),
    ("SELECT * FROM v_orders; DELETE FROM v_orders", R.MULTIPLE_STATEMENTS),
    ("SELECT 1 -- harmless\n; DROP TABLE v_orders", R.MULTIPLE_STATEMENTS),
    ("SELECT 1 /* ; */ ; TRUNCATE v_orders", R.MULTIPLE_STATEMENTS),
    ("SELECT order_id FROM v_orders;;SELECT 1", R.MULTIPLE_STATEMENTS),
    # writes and DDL
    ("DROP TABLE v_orders", R.WRITE_OPERATION),
    ("DELETE FROM v_orders WHERE order_status = 'canceled'", R.WRITE_OPERATION),
    ("UPDATE v_order_items SET price = 0", R.WRITE_OPERATION),
    ("INSERT INTO v_reviews VALUES ('x', 'y', 5, now())", R.WRITE_OPERATION),
    ("TRUNCATE v_orders", R.WRITE_OPERATION),
    ("CREATE TABLE backup AS SELECT * FROM v_orders", R.WRITE_OPERATION),
    ("ALTER TABLE v_orders ADD COLUMN x int", R.WRITE_OPERATION),
    ("GRANT ALL ON ALL TABLES IN SCHEMA curated TO PUBLIC", R.WRITE_OPERATION),
    ("WITH d AS (DELETE FROM v_orders RETURNING *) SELECT count(*) FROM d", R.WRITE_OPERATION),
    (
        "WITH u AS (UPDATE v_order_items SET price = 0 RETURNING 1) SELECT * FROM u",
        R.WRITE_OPERATION,
    ),
    ("COPY v_orders TO '/tmp/orders.csv'", R.WRITE_OPERATION),
    ("SET statement_timeout = 0", R.WRITE_OPERATION),
    ("BEGIN", R.WRITE_OPERATION),
    ("VACUUM v_orders", R.WRITE_OPERATION),
    (
        "MERGE INTO v_orders o USING v_orders s ON o.order_id = s.order_id WHEN MATCHED THEN DELETE",
        R.WRITE_OPERATION,
    ),
    ("SELECT * INTO backup FROM v_orders", R.NOT_SELECT),
    ("SHOW data_directory", R.WRITE_OPERATION),
    # tables outside the curated views
    ("SELECT * FROM raw.customers", R.DISALLOWED_TABLE),
    ("SELECT * FROM pg_catalog.pg_user", R.DISALLOWED_TABLE),
    ("SELECT usename, passwd FROM pg_shadow", R.DISALLOWED_TABLE),
    ("SELECT table_name FROM information_schema.tables", R.DISALLOWED_TABLE),
    ("SELECT * FROM query_log", R.DISALLOWED_TABLE),
    ("SELECT * FROM api_keys", R.DISALLOWED_TABLE),
    ("SELECT * FROM public.v_orders", R.DISALLOWED_TABLE),
    ("SELECT * FROM app.curated.v_orders", R.DISALLOWED_TABLE),
    (
        "SELECT o.order_id FROM v_orders o JOIN raw.order_reviews r ON r.order_id = o.order_id",
        R.DISALLOWED_TABLE,
    ),
    ("SELECT * FROM (SELECT * FROM pg_stat_activity) t", R.DISALLOWED_TABLE),
    # restricted columns (PII)
    ("SELECT customer_zip_code_prefix FROM v_customers", R.DISALLOWED_COLUMN),
    ("SELECT customer_city, COUNT(*) FROM v_customers GROUP BY 1", R.DISALLOWED_COLUMN),
    ("SELECT review_comment_message FROM v_reviews", R.DISALLOWED_COLUMN),
    (
        "SELECT c.customer_unique_id FROM v_customers c WHERE c.customer_city = 'santos'",
        R.DISALLOWED_COLUMN,
    ),
    ("SELECT * FROM v_customers", R.STAR_ON_SENSITIVE),
    ("SELECT c.* FROM v_customers c", R.STAR_ON_SENSITIVE),
    # dangerous functions
    ("SELECT pg_sleep(30)", R.DISALLOWED_FUNCTION),
    ("SELECT count(*) FROM v_orders WHERE pg_sleep(1) IS NOT NULL", R.DISALLOWED_FUNCTION),
    ("SELECT pg_read_file('/etc/passwd')", R.DISALLOWED_FUNCTION),
    ("SELECT current_setting('data_directory')", R.DISALLOWED_FUNCTION),
    ("SELECT set_config('statement_timeout', '0', false)", R.DISALLOWED_FUNCTION),
    ("SELECT pg_terminate_backend(pid) FROM v_orders", R.DISALLOWED_FUNCTION),
    ("SELECT count(*) FROM generate_series(1, 100000000)", R.DISALLOWED_FUNCTION),
    ("SELECT version()", R.DISALLOWED_FUNCTION),
    ("SELECT repeat('x', 100000000)", R.DISALLOWED_FUNCTION),
    ("SELECT dblink('host=evil', 'select 1')", R.DISALLOWED_FUNCTION),
    # resource exhaustion and locks
    ("SELECT count(*) FROM v_orders, v_order_items", R.CARTESIAN_JOIN),
    ("SELECT count(*) FROM v_orders o CROSS JOIN v_payments p", R.CARTESIAN_JOIN),
    ("SELECT count(*) FROM v_orders o JOIN v_payments p ON 1 = 1", R.CARTESIAN_JOIN),
    (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n",
        R.RECURSIVE_CTE,
    ),
    ("SELECT * FROM v_orders FOR UPDATE", R.LOCKING),
    ("SELECT * FROM v_orders LIMIT (SELECT 100000)", R.BAD_LIMIT),
    # unparseable / empty
    ("", R.EMPTY),
    (";", R.EMPTY),
    ("SELEC order_id FROM v_orders", R.PARSE_ERROR),
]


def test_suite_has_at_least_50_malicious_strings() -> None:
    assert len(MALICIOUS) >= 50


@pytest.mark.parametrize(
    ("sql", "reason"), MALICIOUS, ids=[f"{i:02d}" for i in range(len(MALICIOUS))]
)
def test_malicious_sql_is_blocked_with_reason(sql: str, reason: Reason) -> None:
    r = G.check(sql)
    assert not r.ok and r.sql is None
    assert r.reason == reason, f"{sql!r}: got {r.reason} ({r.detail})"
    assert r.detail


@pytest.mark.parametrize(
    ("tier", "question", "sql"), QUESTIONS, ids=[f"q{i:03d}" for i in range(len(QUESTIONS))]
)
def test_gold_sql_is_never_blocked(tier: str, question: str, sql: str) -> None:
    r = G.check(sql)
    assert r.ok, f"false block {r.reason}: {r.detail}"
    assert r.sql is not None and "LIMIT" in r.sql.upper()


@pytest.mark.parametrize(
    ("sql", "expected_limit"),
    [
        ("SELECT order_id FROM v_orders", 500),
        ("SELECT order_id FROM v_orders LIMIT 10", 10),
        ("SELECT order_id FROM v_orders LIMIT 100000", 500),
        ("SELECT order_id FROM v_orders UNION SELECT order_id FROM v_reviews", 500),
    ],
)
def test_limit_is_enforced(sql: str, expected_limit: int) -> None:
    r = G.check(sql)
    assert r.ok and r.sql is not None
    tree = sqlglot.parse_one(r.sql, read="postgres")
    assert int(tree.args["limit"].expression.this) == expected_limit


def test_comments_are_stripped_from_executed_sql() -> None:
    r = G.check("SELECT order_id /* ignore the guard */ FROM v_orders -- trailing")
    assert r.ok and r.sql is not None and "ignore" not in r.sql and "--" not in r.sql


def test_unknown_column_is_repairable_but_restricted_is_not() -> None:
    assert G.check("SELECT revenue FROM v_orders").repairable
    assert not G.check("SELECT customer_city FROM v_customers").repairable


def test_cte_and_subqueries_and_count_star_on_sensitive_pass() -> None:
    sql = ("WITH firsts AS (SELECT c.customer_unique_id, MIN(o.purchased_at) AS first_at FROM v_orders o "
           "JOIN v_customers c ON c.customer_id = o.customer_id GROUP BY 1) "
           "SELECT COUNT(*) FROM firsts WHERE first_at >= '2018-01-01'")  # fmt: skip
    assert G.check(sql).ok
    assert G.check("SELECT COUNT(*) FROM v_customers").ok
