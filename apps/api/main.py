from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apps.api.routes_cases import case_router, operator_case_router
from apps.api.routes_webhook import (
    database_path,
    operator_router,
    webhook_router,
)
from apps.api.settings import get_settings
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
app.include_router(webhook_router)
app.include_router(case_router)
app.include_router(operator_router, dependencies=[Depends(require_operator)])
app.include_router(operator_case_router, dependencies=[Depends(require_operator)])


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
