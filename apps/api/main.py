from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apps.api.routes_cases import (
    case_router,
    get_case as get_case_json,
    operator_case_router,
)
from apps.api.routes_web import case_page, landing_page
from apps.api.routes_webhook import (
    database_path,
    operator_router,
    webhook_router,
)
from apps.api.settings import Surface, get_settings
from packages.domain.db import create_all

_operator_bearer = HTTPBearer(auto_error=False)


def require_operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(_operator_bearer),
) -> None:
    """Require the configured operator bearer token for operator-only routes."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="operator bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    expected = get_settings().operator_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operator token is not configured",
        )
    if not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid operator bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    create_all(database_path())
    yield


app = FastAPI(title="Rebound", version="0.1.0", lifespan=lifespan)


@app.get("/")
async def surface_landing(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_operator_bearer),
) -> Any:
    """Select the landing surface while keeping the operator console protected."""
    if get_settings().surface is Surface.OPERATOR:
        require_operator(credentials)
    return await landing_page(request)


@app.get("/cases/{case_ref}", response_model=None)
async def case_representation(
    case_ref: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_operator_bearer),
) -> Any:
    """Serve HTML to browsers and preserve Task 21's JSON API otherwise."""
    if _accepts_html(request.headers.get("accept")):
        require_operator(credentials)
        return await case_page(request, case_ref)
    return await get_case_json(case_ref)


app.include_router(webhook_router)
app.include_router(case_router)
app.include_router(operator_router, dependencies=[Depends(require_operator)])
app.include_router(operator_case_router, dependencies=[Depends(require_operator)])


def _accepts_html(accept: str | None) -> bool:
    if not accept:
        return False
    return any(
        media_type.split(";", 1)[0].strip().lower() == "text/html"
        for media_type in accept.split(",")
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "mode": settings.rebound_mode.value,
        "executor": settings.executor.value,
        "surface": settings.surface.value,
        "chaos": settings.chaos_profile.value,
    }
