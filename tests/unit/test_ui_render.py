"""The answer partial renders every outcome the API can return (answered with chart,
clarification with option buttons, blocked with reason) without template errors."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from querywise.pipeline import Answer
from querywise.present import chart_spec
from querywise.ui import templates

T = templates.env.get_template("_answer.html")


def render(a: Answer) -> str:
    return T.render(a=a, qid=7, conversation_id="conv1")


def test_answered_with_chart_table_sql_and_assumptions() -> None:
    cols = ["month", "revenue"]
    rows = [(datetime(2018, 1, 1), Decimal("1000.5")), (datetime(2018, 2, 1), Decimal("1200.25"))]
    a = Answer(status="answered", question="Monthly revenue 2018", sql="SELECT 1 LIMIT 500", columns=cols,
               rows=rows, chart=chart_spec(cols, rows), summary="Revenue rose from 1,000.50 to 1,200.25.",
               summary_verified=True, assumptions=["revenue excludes canceled orders"],
               timings_ms={"llm": 900, "db": 12})  # fmt: skip
    html = render(a)
    assert (
        "vegaEmbed" in html
        and "1,200.25" in html
        and "LIMIT 500" in html
        and "excludes canceled" in html
    )


def test_clarification_renders_option_buttons() -> None:
    a = Answer(status="needs_clarification", question="Top products?", clarification_question="By revenue or units?",
               options=["revenue", "units"])  # fmt: skip
    html = render(a)
    assert "By revenue or units?" in html and html.count('hx-post="/ui/ask"') == 2


def test_blocked_shows_reason_and_escapes_html() -> None:
    a = Answer(status="blocked", question="<script>x</script>", reason="DISALLOWED_COLUMN",
               detail="customer_city is restricted")  # fmt: skip
    html = render(a)
    assert "DISALLOWED_COLUMN" in html and "<script>x</script>" not in html
