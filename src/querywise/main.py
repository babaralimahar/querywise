"""QueryWise app: uv run uvicorn querywise.main:create_app --factory --port 8007"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import normalize_database_url
from app.main import AppContext
from app.main import create_app as create_base_app
from app.worker import Handler
from querywise import __version__
from querywise.api import RateLimiter, router
from querywise.config import QuerySettings, get_query_settings
from querywise.guard import SqlGuard
from querywise.pipeline import Pipeline, PipelineConfig
from querywise.semantic import load_semantic_layer
from querywise.service import QueryService, build_embedder, build_linker
from querywise.ui import router as ui_router


def _no_handlers(_: AppContext) -> Mapping[str, Handler]:
    return {}


def create_app(settings: QuerySettings | None = None) -> FastAPI:
    s = settings or get_query_settings()
    app = create_base_app(
        s,
        routers=[router, ui_router],
        handlers=_no_handlers,
        title="QueryWise",
        version=__version__,
    )
    layer = load_semantic_layer(s.semantic_layer_path)
    # Generated SQL only ever runs on this engine (role analyst_ro); see executor.assert_role.
    analytics = create_async_engine(
        normalize_database_url(s.analytics_database_url), pool_size=5, max_overflow=5
    )
    embedder = build_embedder(s, app.state.sessionmaker)
    cfg = PipelineConfig(
        use_linking=s.use_linking,
        use_semantic_layer=s.use_semantic_layer,
        max_repairs=s.max_repairs,
        few_shot_k=s.few_shot_k,
        summary=s.summary_mode,
        prompt_version=s.prompt_version,
    )
    pipeline = Pipeline(
        llm=app.state.llm,
        layer=layer,
        guard=SqlGuard(layer, max_rows=s.max_rows),
        analytics=analytics,
        embedder=embedder,
        linker=build_linker(layer, embedder, s),
        cfg=cfg,
        max_attempts=s.llm_max_attempts,
        timeout_ms=s.statement_timeout_ms,
        cost_ceiling=s.explain_cost_ceiling,
        max_rows=s.max_rows,
    )
    app.state.analytics_engine = analytics
    app.state.query_service = QueryService(
        pipeline, app.state.sessionmaker, cache_ttl_s=s.cache_ttl_s
    )
    app.state.rate_limiter = RateLimiter(s.queries_per_minute)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/ui")

    return app
