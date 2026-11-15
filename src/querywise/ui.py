"""Demo UI (Jinja2 + HTMX + vega-embed). Same service and role checks as the API."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.adapters.db.api_keys import SqlApiKeyRepo, hash_key
from app.api.auth import Principal
from app.api.deps import SessionDep
from app.api.errors import DomainError
from app.domain.roles import has_role
from querywise.api import service
from querywise.present import fmt

COOKIE = "qw_key"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["fmt"] = fmt
router = APIRouter(prefix="/ui", include_in_schema=False)


async def _me(request: Request, session: Any) -> Principal | None:
    raw = request.cookies.get(COOKIE)
    rec = await SqlApiKeyRepo(session).find_by_hash(hash_key(raw)) if raw else None
    if rec is None or rec.disabled or not has_role(rec.role, "viewer"):
        return None
    return Principal(rec.id, rec.role)


@router.get("/login")
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"me": None, "error": None})


@router.post("/login")
async def login(
    request: Request, session: SessionDep, api_key: Annotated[str, Form(max_length=200)]
) -> Response:
    rec = await SqlApiKeyRepo(session).find_by_hash(hash_key(api_key.strip()))
    if rec is None or rec.disabled or not has_role(rec.role, "viewer"):
        return templates.TemplateResponse(
            request, "login.html", {"me": None, "error": "Key not recognised."}, status_code=401
        )
    resp = RedirectResponse("/ui", status_code=303)
    resp.set_cookie(COOKIE, api_key.strip(), httponly=True, samesite="strict", max_age=8 * 3600)
    return resp


@router.post("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/ui/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("")
async def home(request: Request, session: SessionDep) -> Response:
    me = await _me(request, session)
    if me is None:
        return RedirectResponse("/ui/login", status_code=303)
    ctx = {
        "me": me,
        "conversation_id": uuid.uuid4().hex[:16],
        "sample": "Top 5 product categories by revenue in 2018",
    }
    return templates.TemplateResponse(request, "home.html", ctx)


@router.get("/schema")
async def schema_page(request: Request, session: SessionDep) -> Response:
    me = await _me(request, session)
    if me is None:
        return RedirectResponse("/ui/login", status_code=303)
    return templates.TemplateResponse(
        request, "schema.html", {"me": me, "layer": service(request).pipeline.layer}
    )


@router.post("/ask")
async def ask(
    request: Request,
    session: SessionDep,
    question: Annotated[str, Form(min_length=3, max_length=1000)],
    conversation_id: Annotated[str, Form(max_length=40, pattern=r"^[A-Za-z0-9_-]+$")],
) -> Response:
    me = await _me(request, session)
    if me is None:
        return HTMLResponse(
            '<p class="note err">Session expired. <a href="/ui/login">Sign in</a></p>',
            status_code=401,
        )
    try:
        request.app.state.rate_limiter.check(me.key_id)
    except DomainError as e:
        return HTMLResponse(f'<p class="note err">{e.detail}</p>', status_code=429)
    qid, ans = await service(request).ask(question.strip(), me.key_id, conversation_id)
    return templates.TemplateResponse(
        request, "_answer.html", {"a": ans, "qid": qid, "conversation_id": conversation_id}
    )
