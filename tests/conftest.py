from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest
from tenacity import wait_none

import app.adapters.llm as llm_module
from app.config import Settings
from querywise.config import QuerySettings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5437/app_test"
)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "database_url": TEST_DATABASE_URL,
        "llm_provider": "fake",
        "worker_mode": "off",
        "otel_endpoint": None,
        "_env_file": None,  # tests never read the developer's .env
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry tests fast: tenacity waits are zero in tests."""
    monkeypatch.setattr(llm_module, "_WAIT", wait_none())


def pytest_asyncio_loop_factories(config: Any, item: Any) -> dict[str, Any]:
    """psycopg's async driver (LangGraph checkpointer) cannot run on Windows' Proactor loop."""
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


def make_query_settings(**overrides: Any) -> QuerySettings:
    """QueryWise settings for tests: fake LLM and embeddings, never the dev .env."""
    values: dict[str, Any] = {
        "env": "test",
        "database_url": TEST_DATABASE_URL,
        "analytics_database_url": TEST_DATABASE_URL.replace(
            "postgres:postgres@", "analyst_ro:analyst-ro-dev@"
        ),
        "llm_provider": "fake",
        "embed_provider": "fake",
        "worker_mode": "off",
        "otel_endpoint": None,
        "summary_mode": "template",
        "_env_file": None,
    }
    values.update(overrides)
    return QuerySettings(**values)
