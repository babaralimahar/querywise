"""QueryWise: raw warehouse, curated views, read-only analyst role, audit/cache/feedback tables.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import os
import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

RAW = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE raw.category_translation (
  product_category_name text PRIMARY KEY, product_category_name_english text NOT NULL);
CREATE TABLE raw.customers (
  customer_id text PRIMARY KEY, customer_unique_id text NOT NULL,
  customer_zip_code_prefix text NOT NULL, customer_city text NOT NULL, customer_state char(2) NOT NULL);
CREATE TABLE raw.sellers (
  seller_id text PRIMARY KEY, seller_zip_code_prefix text NOT NULL,
  seller_city text NOT NULL, seller_state char(2) NOT NULL);
CREATE TABLE raw.products (
  product_id text PRIMARY KEY, product_category_name text,
  product_weight_g integer, product_photos_qty integer);
CREATE TABLE raw.orders (
  order_id text PRIMARY KEY, customer_id text NOT NULL REFERENCES raw.customers,
  order_status text NOT NULL, order_purchase_timestamp timestamp NOT NULL,
  order_approved_at timestamp, order_delivered_carrier_date timestamp,
  order_delivered_customer_date timestamp, order_estimated_delivery_date timestamp NOT NULL);
CREATE TABLE raw.order_items (
  order_id text NOT NULL REFERENCES raw.orders, order_item_id integer NOT NULL,
  product_id text NOT NULL REFERENCES raw.products, seller_id text NOT NULL REFERENCES raw.sellers,
  shipping_limit_date timestamp NOT NULL, price numeric(10,2) NOT NULL, freight_value numeric(10,2) NOT NULL,
  PRIMARY KEY (order_id, order_item_id));
CREATE TABLE raw.order_payments (
  order_id text NOT NULL REFERENCES raw.orders, payment_sequential integer NOT NULL,
  payment_type text NOT NULL, payment_installments integer NOT NULL, payment_value numeric(10,2) NOT NULL,
  PRIMARY KEY (order_id, payment_sequential));
CREATE TABLE raw.order_reviews (
  review_id text PRIMARY KEY, order_id text NOT NULL REFERENCES raw.orders,
  review_score smallint NOT NULL CHECK (review_score BETWEEN 1 AND 5),
  review_comment_message text, review_creation_date timestamp NOT NULL);
CREATE INDEX ON raw.orders (customer_id);
CREATE INDEX ON raw.orders (order_purchase_timestamp);
CREATE INDEX ON raw.order_items (product_id);
CREATE INDEX ON raw.order_items (seller_id);
CREATE INDEX ON raw.order_reviews (order_id);
"""

# Curated views: the ONLY objects analyst_ro can read. Restricted columns (customer zip/city,
# review comments: free text that contains phone numbers) are simply not in any view.
CURATED = """
CREATE SCHEMA IF NOT EXISTS curated;
CREATE VIEW curated.v_orders AS
  SELECT order_id, customer_id, order_status, order_purchase_timestamp AS purchased_at,
         order_approved_at AS approved_at, order_delivered_carrier_date AS delivered_carrier_at,
         order_delivered_customer_date AS delivered_customer_at,
         order_estimated_delivery_date AS estimated_delivery_at
  FROM raw.orders;
CREATE VIEW curated.v_customers AS
  SELECT customer_id, customer_unique_id, customer_state FROM raw.customers;
CREATE VIEW curated.v_order_items AS
  SELECT order_id, order_item_id, product_id, seller_id, price, freight_value,
         shipping_limit_date AS shipping_limit_at
  FROM raw.order_items;
CREATE VIEW curated.v_payments AS
  SELECT order_id, payment_sequential, payment_type, payment_installments, payment_value
  FROM raw.order_payments;
CREATE VIEW curated.v_products AS
  SELECT p.product_id, coalesce(t.product_category_name_english, p.product_category_name) AS category,
         p.product_category_name AS category_pt, p.product_weight_g AS weight_g,
         p.product_photos_qty AS photos_qty
  FROM raw.products p LEFT JOIN raw.category_translation t USING (product_category_name);
CREATE VIEW curated.v_sellers AS
  SELECT seller_id, seller_city, seller_state FROM raw.sellers;
CREATE VIEW curated.v_reviews AS
  SELECT review_id, order_id, review_score, review_creation_date AS created_at FROM raw.order_reviews;
"""

VIEWS = [
    "v_orders",
    "v_customers",
    "v_order_items",
    "v_payments",
    "v_products",
    "v_sellers",
    "v_reviews",
]


def _run(script: str) -> None:
    """asyncpg prepares each statement, so multi-statement scripts are split. DO blocks are
    passed whole (they contain semicolons inside $$ ... $$)."""
    parts = script.split("$$")
    stmts: list[str] = []
    buf = ""
    for i, part in enumerate(parts):
        if i % 2:  # inside a $$ body
            buf += "$$" + part + "$$"
            continue
        pieces = part.split(";")
        buf += pieces[0]
        for piece in pieces[1:]:
            stmts.append(buf)
            buf = piece
    stmts.append(buf)
    for st in stmts:
        if st.strip():
            op.execute(st.strip())


def _password() -> str:
    pw = os.environ.get("ANALYST_RO_PASSWORD", "analyst-ro-dev")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,64}", pw):
        raise ValueError("ANALYST_RO_PASSWORD must be 8-64 chars of [A-Za-z0-9_-]")
    return pw


def upgrade() -> None:
    _run(RAW)
    _run(CURATED)
    pw = _password()
    # Roles are cluster-wide: create once, then (re)apply settings and this database's grants.
    _run(f"""
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst_ro') THEN
        CREATE ROLE analyst_ro LOGIN PASSWORD '{pw}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
      ELSE
        ALTER ROLE analyst_ro WITH LOGIN PASSWORD '{pw}';
      END IF;
    END $$;
    ALTER ROLE analyst_ro SET statement_timeout = '5s';
    ALTER ROLE analyst_ro SET default_transaction_read_only = on;
    ALTER ROLE analyst_ro SET idle_in_transaction_session_timeout = '10s';
    ALTER ROLE analyst_ro CONNECTION LIMIT 20;
    REVOKE ALL ON SCHEMA public FROM analyst_ro;
    REVOKE ALL ON SCHEMA raw FROM analyst_ro;
    GRANT USAGE ON SCHEMA curated TO analyst_ro;
    GRANT SELECT ON ALL TABLES IN SCHEMA curated TO analyst_ro;
    """)  # noqa: S608  (password validated above; DDL cannot take bind parameters)

    op.create_table(
        "query_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("conversation_id", sa.String(40)),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column(
            "status", sa.String(30), nullable=False
        ),  # answered|needs_clarification|blocked|failed|empty
        sa.Column("reason", sa.Text),
        sa.Column("sql", sa.Text),
        sa.Column("sql_hash", sa.String(64)),
        sa.Column("row_count", sa.Integer),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cached", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("timings_ms", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_query_log_created", "query_log", ["created_at"])
    op.create_index("ix_query_log_conv", "query_log", ["conversation_id", "id"])
    op.create_table(
        "query_cache",
        sa.Column("sql_hash", sa.String(64), primary_key=True),
        sa.Column("result", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("query_id", sa.BigInteger, sa.ForeignKey("query_log.id"), nullable=False),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("corrected_sql", sa.Text),
        sa.Column("note", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("verdict IN ('correct', 'incorrect')", name="ck_feedback_verdict"),
    )
    op.create_table(
        "embed_cache",
        sa.Column("key", sa.String(64), primary_key=True),  # sha256(model + text)
        sa.Column("embedding", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    for t in ("embed_cache", "feedback", "query_cache", "query_log"):
        op.drop_table(t)
    # The role is cluster-wide and may serve other databases: revoke here, never drop it.
    _run("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst_ro') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA curated FROM analyst_ro;
        REVOKE ALL ON SCHEMA curated FROM analyst_ro;
      END IF;
    END $$;
    """)
    op.execute("DROP SCHEMA curated CASCADE")
    op.execute("DROP SCHEMA raw CASCADE")
