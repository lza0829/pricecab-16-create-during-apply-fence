from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import ticket_service as svc

router = APIRouter(prefix="/tickets", tags=["tickets"])


class TicketLineIn(BaseModel):
    sku_code: str
    new_price: float


class TicketCreateIn(BaseModel):
    title: str
    effective_at: str
    note: str | None = None
    lines: list[TicketLineIn] = Field(default_factory=list)


class TicketRescheduleIn(BaseModel):
    effective_at: str


@router.get("")
def list_tickets():
    return svc.list_tickets()


@router.get("/{ticket_id}")
def get_ticket(ticket_id: int):
    return svc.get_ticket(ticket_id)


@router.post("")
def create_ticket(body: TicketCreateIn):
    return svc.create_ticket(body.model_dump())


@router.patch("/{ticket_id}/reschedule")
def reschedule_ticket(ticket_id: int, body: TicketRescheduleIn):
    return svc.reschedule_ticket(ticket_id, body.effective_at)
