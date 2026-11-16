from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.repositories import skus as sku_repo
from app.repositories import tickets as ticket_repo
from app.repositories import fence as fence_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_tickets() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return ticket_repo.list_tickets(conn)
    finally:
        conn.close()


def get_ticket(ticket_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        t = ticket_repo.get_ticket(conn, ticket_id)
        if not t:
            raise HTTPException(404, "改价单不存在")
        t["lines"] = ticket_repo.list_lines(conn, ticket_id)
        return t
    finally:
        conn.close()


def create_ticket(body: dict[str, Any]) -> dict[str, Any]:
    title = (body.get("title") or "").strip()
    effective_at = (body.get("effective_at") or "").strip()
    note = (body.get("note") or None)
    lines = body.get("lines") or []
    if not title:
        raise HTTPException(400, "标题必填")
    if not effective_at:
        raise HTTPException(400, "生效时刻必填")
    if not lines:
        raise HTTPException(400, "至少一行改价明细")

    # All fence checks happen inside one IMMEDIATE transaction so the
    # observed lock and the insert are atomic (no TOCTOU window).
    conn = connect(immediate=True)
    try:
        lock = fence_repo.active_lock(conn)
        if lock is not None and fence_repo.get_policy(conn) == fence_repo.POLICY_REJECT:
            fence_repo.increment_counter(conn, lock["id"], "rejected_creates")
            fence_repo.record_event(
                conn,
                lock_id=lock["id"],
                batch_no=lock["batch_no"],
                event_type=fence_repo.EVENT_REJECTED_CREATE,
                ticket_no=None,
                policy=fence_repo.POLICY_REJECT,
                detail=effective_at,
                created_at=_now(),
            )
            conn.commit()
            raise HTTPException(
                409,
                f"应用批次 {lock['batch_no']} 持锁期间拒绝新建改价单（策略 reject），批次结束后再建",
            )

        deferred = lock is not None
        fence_lock_id = lock["id"] if deferred else None
        ticket_no = ticket_repo.next_ticket_no(conn)
        tid = ticket_repo.create_ticket(
            conn,
            ticket_no=ticket_no,
            title=title,
            effective_at=effective_at,
            note=note,
            created_at=_now(),
            fence_lock_id=fence_lock_id,
        )
        for line in lines:
            sku_code = (line.get("sku_code") or "").strip()
            new_price = line.get("new_price")
            if not sku_code:
                raise HTTPException(400, "明细缺少 sku_code")
            sku = sku_repo.get_by_code(conn, sku_code)
            if not sku:
                raise HTTPException(400, f"未知商品 {sku_code}")
            book = conn.execute(
                "SELECT current_price FROM price_book WHERE sku_code=?", (sku_code,)
            ).fetchone()
            old_price = float(book["current_price"]) if book else float(sku["list_price"])
            if new_price is None:
                raise HTTPException(400, f"{sku_code} 缺少 new_price")
            ticket_repo.add_line(
                conn,
                ticket_id=tid,
                sku_code=sku_code,
                old_price=old_price,
                new_price=float(new_price),
            )

        if deferred:
            # Allowed to land as scheduled, but it must not enter the running
            # batch: it is recorded as deferred for audit and left untouched.
            fence_repo.increment_counter(conn, lock["id"], "deferred_creates")
            fence_repo.record_event(
                conn,
                lock_id=lock["id"],
                batch_no=lock["batch_no"],
                event_type=fence_repo.EVENT_DEFERRED_CREATE,
                ticket_no=ticket_no,
                policy=fence_repo.POLICY_DEFER,
                detail=effective_at,
                created_at=_now(),
            )

        conn.commit()
        result = get_ticket(tid)
        if deferred:
            result["fenced"] = True
            result["deferred_to_later_batch"] = True
            result["lock_batch_no"] = lock["batch_no"]
        return result
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reschedule_ticket(ticket_id: int, effective_at: str) -> dict[str, Any]:
    effective_at = (effective_at or "").strip()
    if not effective_at:
        raise HTTPException(400, "生效时刻必填")

    conn = connect(immediate=True)
    try:
        t = ticket_repo.get_ticket(conn, ticket_id)
        if not t:
            raise HTTPException(404, "改价单不存在")
        if t["status"] != "scheduled":
            raise HTTPException(400, "仅待生效（scheduled）改价单可以改约")

        lock = fence_repo.active_lock(conn)
        if lock is not None:
            # Mutual exclusion: while a batch is pinned, changing effective
            # time could pull a ticket into / out of the pinned set, so every
            # reschedule fails regardless of the create policy.
            fence_repo.increment_counter(conn, lock["id"], "rejected_reschedules")
            fence_repo.record_event(
                conn,
                lock_id=lock["id"],
                batch_no=lock["batch_no"],
                event_type=fence_repo.EVENT_REJECTED_RESCHEDULE,
                ticket_no=t["ticket_no"],
                policy=lock["policy"],
                detail=effective_at,
                created_at=_now(),
            )
            conn.commit()
            raise HTTPException(
                409,
                f"应用批次 {lock['batch_no']} 持锁期间拒绝改约（{t['ticket_no']}），批次结束后再改",
            )

        ticket_repo.reschedule(conn, ticket_id, effective_at)
        conn.commit()
        return get_ticket(ticket_id)
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
