"""Server-rendered operator console routes."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import col, select

from apps.api.routes_webhook import database_path
from apps.api.settings import Surface, get_settings
from packages.domain.db import session_factory
from packages.domain.models import Order, RecoveryCase

web_router = APIRouter(tags=["web"])

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "web" / "templates"
_TRAVELLER_LANDING = "/t/demo"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


@web_router.get("/", response_class=HTMLResponse)
async def landing_page(request: Request) -> Response:
    """Render the operator shell or select the traveller surface via SURFACE."""
    if get_settings().surface is Surface.TRAVELLER:
        return RedirectResponse(_TRAVELLER_LANDING, status_code=307)

    with session_factory(database_path())() as session:
        case = session.exec(
            select(RecoveryCase).order_by(col(RecoveryCase.opened_at).desc())
        ).first()
        if case is None:
            context = _empty_context()
        else:
            order = session.get(Order, case.order_id)
            context = _case_context(case, order)
    return templates.TemplateResponse(
        request=request,
        name="case.html",
        context=context,
    )


@web_router.get("/cases/{case_ref}", response_class=HTMLResponse)
async def case_page(request: Request, case_ref: str) -> Response:
    """Render one recovery case as the three-pane operator console."""
    with session_factory(database_path())() as session:
        case = session.exec(
            select(RecoveryCase).where(RecoveryCase.case_ref == case_ref)
        ).first()
        if case is None:
            raise HTTPException(status_code=404, detail=f"case {case_ref!r} not found")
        order = session.get(Order, case.order_id)
        context = _case_context(case, order)
    return templates.TemplateResponse(
        request=request,
        name="case.html",
        context=context,
    )


def _empty_context() -> dict[str, Any]:
    return {
        "case_ref": "No active case",
        "status": "waiting",
        "status_label": "Waiting",
        "traveller_name": "No active traveller",
        "passenger_count": 0,
        "itinerary": [],
        "opened_at": "—",
        "stream_url": "",
    }


def _case_context(case: RecoveryCase, order: Order | None) -> dict[str, Any]:
    passengers = _json_list(order.passengers_json if order is not None else "[]")
    itinerary = _json_list(order.itinerary_json if order is not None else "[]")
    status = case.status.strip().lower()
    return {
        "case_ref": case.case_ref,
        "status": status,
        "status_label": status.replace("_", " "),
        "traveller_name": _traveller_name(passengers),
        "passenger_count": len(passengers),
        "itinerary": [_segment_view(segment) for segment in itinerary],
        "opened_at": _display_time(case.opened_at),
        "stream_url": f"/cases/{case.case_ref}/stream",
    }


def _json_list(raw: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _traveller_name(passengers: list[dict[str, Any]]) -> str:
    names: list[str] = []
    for passenger in passengers:
        raw_name = (
            passenger.get("name")
            or passenger.get("fullName")
            or passenger.get("full_name")
        )
        if not raw_name:
            parts = [
                passenger.get("firstName") or passenger.get("first_name"),
                passenger.get("lastName") or passenger.get("last_name"),
            ]
            raw_name = " ".join(str(part) for part in parts if part)
        name = str(raw_name or "").replace("/", " ").strip()
        if name:
            names.append(name.title())
    return ", ".join(names) if names else "Traveller"


def _segment_view(segment: dict[str, Any]) -> dict[str, str]:
    carrier = str(segment.get("carrier") or "").strip()
    flight_number = str(segment.get("flight_number") or "").strip()
    flight = " ".join(part for part in (carrier, flight_number) if part)
    return {
        "origin": str(segment.get("origin") or "—"),
        "destination": str(segment.get("destination") or "—"),
        "flight": flight or "Flight",
        "departure": _display_time(segment.get("departure_at")),
        "arrival": _display_time(segment.get("arrival_at")),
    }


def _display_time(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return "—"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    return parsed.strftime("%d %b · %H:%M")
