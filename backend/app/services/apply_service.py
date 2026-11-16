from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import HTTPException

from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import tickets as ticket_repo
from app.repositories import prices as price_repo
from app.repositories import batches as batch_repo
from app.repositories import fence as fence_repo


# Test seam: called after the pin transaction commits and before the pinned
# tickets are applied, with a still-active, durable lock in the database.
_on_pinned: Callable[[dict[str, Any], list[dict[str, Any]]], None] | None = None

# Serializes applies within this process. The deployment is single-process
# against one SQLite file; the durable apply_locks row still fences ticket
# creation across every connection, while this lock makes two applies
# mutually exclusive and lets recovery treat any leftover active lock as
# belonging to a crashed prior run.
_apply_guard = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_clock() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            "business_clock": settings_repo.get(conn, "business_clock"),
            "clock_label": settings_repo.get(conn, "clock_label"),
        }
    finally:
        conn.close()


def set_clock(clock_at: str) -> dict[str, Any]:
    clock_at = (clock_at or "").strip()
    if not clock_at:
        raise HTTPException(400, "业务钟不能为空")
    conn = connect(immediate=True)
    try:
        settings_repo.set_value(conn, "business_clock", clock_at)
        conn.commit()
        return get_clock()
    finally:
        conn.close()


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_batches(conn)
    finally:
        conn.close()


def get_batch_detail(batch_no: str) -> dict[str, Any]:
    conn = connect()
    try:
        batch = batch_repo.get_batch_by_no(conn, batch_no)
        if not batch:
            raise HTTPException(404, "批次不存在")
        details = fence_repo.list_batch_tickets(conn, batch["id"])
        return {
            **batch,
            "tickets": details,
            "ticket_nos": [d["ticket_no"] for d in details],
        }
    finally:
        conn.close()


def fence_status() -> dict[str, Any]:
    conn = connect()
    try:
        lock = fence_repo.active_lock(conn)
        return {
            "active": lock is not None,
            "policy": fence_repo.get_policy(conn),
            "lock": lock,
        }
    finally:
        conn.close()


def list_fence_events(limit: int = 200) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return fence_repo.list_events(conn, limit=limit)
    finally:
        conn.close()


def list_fence_locks() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return fence_repo.list_locks(conn)
    finally:
        conn.close()


def get_fence_policy() -> dict[str, Any]:
    conn = connect()
    try:
        return {"policy": fence_repo.get_policy(conn), "options": list(fence_repo.POLICIES)}
    finally:
        conn.close()


def set_fence_policy(policy: str) -> dict[str, Any]:
    policy = (policy or "").strip()
    if policy not in fence_repo.POLICIES:
        raise HTTPException(400, f"未知策略，可选：{', '.join(fence_repo.POLICIES)}")
    conn = connect(immediate=True)
    try:
        fence_repo.set_policy(conn, policy)
        conn.commit()
        return get_fence_policy()
    finally:
        conn.close()


def recover_on_startup() -> int:
    """Recover any lock/batch a crashed process left behind. Called at startup."""
    conn = connect(immediate=True)
    try:
        n = fence_repo.recover_active_locks(conn, _now())
        conn.commit()
        return n
    finally:
        conn.close()


def apply_due() -> dict[str, Any]:
    """Serialize applies in-process, then run the fenced three-phase flow."""
    with _apply_guard:
        return _apply_due_locked()


def _apply_due_locked() -> dict[str, Any]:
    """Apply the pinned set of scheduled tickets whose effective_at <= clock.

    The due set is pinned (ticket list + effective time) in its own committed
    transaction. While the resulting lock is active, ticket creation follows
    the configured fence policy and rescheduling is always rejected, so no
    ticket created or changed after the pin can enter this batch. A second
    transaction processes exactly the pinned rows, then a third releases the
    lock.
    """
    pinned_at = _now()

    # Phase 1: pin the due set and publish the durable lock.
    conn = connect(immediate=True)
    try:
        # With the in-process guard held, an active lock here can only be a
        # leftover from a crashed run; recover it before starting a new batch.
        fence_repo.recover_active_locks(conn, pinned_at)
        clock = settings_repo.get(conn, "business_clock")
        if not clock:
            raise HTTPException(400, "业务钟未设置")
        policy = fence_repo.get_policy(conn)
        if fence_repo.active_lock(conn):
            raise HTTPException(409, "已有应用批次正在执行，请等待其结束")
        due = ticket_repo.due_tickets(conn, clock)
        if not due:
            conn.commit()
            return {
                "applied": False,
                "message": "当前业务钟下没有到期改价单",
                "business_clock": clock,
                "pinned_count": 0,
                "ticket_count": 0,
                "line_count": 0,
                "policy": policy,
                "tickets": [],
                "pinned_tickets": [],
            }

        lock_id = fence_repo.insert_lock(
            conn, clock_at=clock, pinned_at=pinned_at, policy=policy
        )
        batch_no = batch_repo.next_batch_no(conn)
        batch_id = batch_repo.create_batch(
            conn,
            batch_no=batch_no,
            clock_at=clock,
            ticket_count=0,
            line_count=0,
            status="running",
            created_at=pinned_at,
            pinned_count=len(due),
            fence_lock_id=lock_id,
        )
        fence_repo.fill_lock_batch(
            conn, lock_id, batch_id=batch_id, batch_no=batch_no, pinned_count=len(due)
        )
        for order, t in enumerate(due):
            line_count = len(ticket_repo.list_lines(conn, t["id"]))
            fence_repo.insert_batch_ticket(
                conn,
                batch_id=batch_id,
                lock_id=lock_id,
                ticket_id=t["id"],
                ticket_no=t["ticket_no"],
                effective_at=t["effective_at"],
                line_count=line_count,
                pinned_order=order,
            )
            fence_repo.record_event(
                conn,
                lock_id=lock_id,
                batch_no=batch_no,
                event_type=fence_repo.EVENT_PINNED,
                ticket_no=t["ticket_no"],
                policy=policy,
                detail=t["effective_at"],
                created_at=pinned_at,
            )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    pinned_snapshot = list(due)
    pinned_nos = [t["ticket_no"] for t in pinned_snapshot]

    try:
        # The lock is now committed and visible to every other connection.
        if _on_pinned is not None:
            _on_pinned(
                {"id": lock_id, "batch_no": batch_no, "clock_at": clock, "policy": policy},
                pinned_snapshot,
            )

        # Phase 2: process exactly the pinned tickets; nothing outside the pin
        # may touch the price book through this batch.
        conn = connect(immediate=True)
        try:
            details = fence_repo.list_batch_tickets(conn, batch_id)
            line_total = 0
            processed_nos: list[str] = []
            created = _now()
            for d in details:
                t = ticket_repo.get_ticket(conn, d["ticket_id"])
                if t is None or t["status"] != "scheduled":
                    # Pinned rows cannot be rescheduled while the lock is
                    # active; a missing/non-scheduled row means tampering.
                    continue
                lines = ticket_repo.list_lines(conn, t["id"])
                for line in lines:
                    price_repo.upsert_book(
                        conn,
                        sku_code=line["sku_code"],
                        price=float(line["new_price"]),
                        ticket_no=t["ticket_no"],
                        effective_at=t["effective_at"],
                        updated_at=created,
                    )
                    line_total += 1
                ticket_repo.mark_applied(conn, t["id"], applied_at=created, batch_id=batch_id)
                processed_nos.append(t["ticket_no"])

            batch_repo.update_totals(
                conn,
                batch_id,
                ticket_count=len(processed_nos),
                line_count=line_total,
                status="done",
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception:
        fail_conn = connect(immediate=True)
        try:
            batch_repo.mark_status(fail_conn, batch_id, "failed")
            fence_repo.mark_lock_failed(fail_conn, lock_id, _now())
            fail_conn.commit()
        finally:
            fail_conn.close()
        raise

    # Phase 3: release the lock; only now may new work join a later batch.
    conn = connect(immediate=True)
    try:
        fence_repo.release_lock(conn, lock_id, _now())
        conn.commit()
    finally:
        conn.close()

    return {
        "applied": True,
        "batch_no": batch_no,
        "business_clock": clock,
        "policy": policy,
        "lock_id": lock_id,
        "pinned_count": len(pinned_snapshot),
        "ticket_count": len(processed_nos),
        "line_count": line_total,
        "pinned_tickets": pinned_nos,
        "tickets": processed_nos,
    }
