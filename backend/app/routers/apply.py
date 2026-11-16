from fastapi import APIRouter
from pydantic import BaseModel

from app.services import apply_service as svc

router = APIRouter(prefix="/apply", tags=["apply"])


class ClockIn(BaseModel):
    business_clock: str


class PolicyIn(BaseModel):
    policy: str


@router.get("/clock")
def get_clock():
    return svc.get_clock()


@router.put("/clock")
def set_clock(body: ClockIn):
    return svc.set_clock(body.business_clock)


@router.get("/batches")
def list_batches():
    return svc.list_batches()


@router.get("/batches/{batch_no}")
def get_batch(batch_no: str):
    return svc.get_batch_detail(batch_no)


@router.get("/fence")
def fence_status():
    return svc.fence_status()


@router.get("/fence/events")
def fence_events():
    return {"events": svc.list_fence_events()}


@router.get("/fence/locks")
def fence_locks():
    return {"locks": svc.list_fence_locks()}


@router.get("/fence/policy")
def get_fence_policy():
    return svc.get_fence_policy()


@router.put("/fence/policy")
def set_fence_policy(body: PolicyIn):
    return svc.set_fence_policy(body.policy)


@router.post("/due")
def apply_due():
    return svc.apply_due()
