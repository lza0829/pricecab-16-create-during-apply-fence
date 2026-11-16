from __future__ import annotations

import sqlite3
from typing import Any

# Strategies for ticket creation / rescheduling while an apply lock is held.
POLICY_REJECT = "reject"
POLICY_DEFER = "defer"
POLICIES = (POLICY_REJECT, POLICY_DEFER)
SETTING_KEY = "apply_fence_policy"
DEFAULT_POLICY = POLICY_DEFER

EVENT_PINNED = "pinned"
EVENT_REJECTED_CREATE = "rejected_create"
EVENT_DEFERRED_CREATE = "deferred_create"
EVENT_REJECTED_RESCHEDULE = "rejected_reschedule"


def get_policy(conn: sqlite3.Connection) -> str:
    from app.repositories import settings as settings_repo

    policy = settings_repo.get(conn, SETTING_KEY, DEFAULT_POLICY)
    return policy if policy in POLICIES else DEFAULT_POLICY


def set_policy(conn: sqlite3.Connection, policy: str) -> None:
    from app.repositories import settings as settings_repo

    settings_repo.set_value(conn, SETTING_KEY, policy)


def active_lock(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM apply_locks WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def recover_active_locks(conn: sqlite3.Connection, now: str) -> int:
    """Recover locks left by a crashed process.

    The caller serializes applies within the process (apply_service guard),
    so any still-active lock at recovery time belongs to a dead prior run.
    Its in-flight batch is marked failed and the lock is marked stale.
    Returns the number of recovered locks.
    """
    rows = conn.execute(
        "SELECT id, batch_id FROM apply_locks WHERE status='active'"
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE apply_locks SET status='stale', released_at=? WHERE id=?",
            (now, r["id"]),
        )
        if r["batch_id"] is not None:
            conn.execute(
                "UPDATE apply_batches SET status='failed' WHERE id=? AND status='running'",
                (r["batch_id"],),
            )
    return len(rows)


def insert_lock(
    conn: sqlite3.Connection,
    *,
    clock_at: str,
    pinned_at: str,
    policy: str,
) -> int:
    cur = conn.execute(
        '''
        INSERT INTO apply_locks(clock_at, pinned_at, status, policy, pinned_count)
        VALUES (?,?,'active',?,0)
        ''',
        (clock_at, pinned_at, policy),
    )
    return int(cur.lastrowid)


def fill_lock_batch(
    conn: sqlite3.Connection,
    lock_id: int,
    *,
    batch_id: int,
    batch_no: str,
    pinned_count: int,
) -> None:
    conn.execute(
        "UPDATE apply_locks SET batch_id=?, batch_no=?, pinned_count=? WHERE id=?",
        (batch_id, batch_no, pinned_count, lock_id),
    )


def release_lock(conn: sqlite3.Connection, lock_id: int, released_at: str) -> None:
    conn.execute(
        "UPDATE apply_locks SET status='released', released_at=? WHERE id=?",
        (released_at, lock_id),
    )


def mark_lock_failed(conn: sqlite3.Connection, lock_id: int, released_at: str) -> None:
    conn.execute(
        "UPDATE apply_locks SET status='failed', released_at=? WHERE id=?",
        (released_at, lock_id),
    )


def increment_counter(conn: sqlite3.Connection, lock_id: int, column: str) -> None:
    assert column in {
        "rejected_creates",
        "deferred_creates",
        "rejected_reschedules",
    }
    conn.execute(
        f"UPDATE apply_locks SET {column}={column}+1 WHERE id=?",
        (lock_id,),
    )


def record_event(
    conn: sqlite3.Connection,
    *,
    lock_id: int | None,
    batch_no: str | None,
    event_type: str,
    ticket_no: str | None = None,
    policy: str | None = None,
    detail: str | None = None,
    created_at: str,
) -> None:
    conn.execute(
        '''
        INSERT INTO apply_fence_events(lock_id, batch_no, event_type, ticket_no, policy, detail, created_at)
        VALUES (?,?,?,?,?,?,?)
        ''',
        (lock_id, batch_no, event_type, ticket_no, policy, detail, created_at),
    )


def list_events(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM apply_fence_events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_locks(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM apply_locks ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_batch_ticket(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    lock_id: int,
    ticket_id: int,
    ticket_no: str,
    effective_at: str,
    line_count: int,
    pinned_order: int,
) -> None:
    conn.execute(
        '''
        INSERT INTO apply_batch_tickets(
            batch_id, lock_id, ticket_id, ticket_no, effective_at, line_count, pinned_order
        ) VALUES (?,?,?,?,?,?,?)
        ''',
        (batch_id, lock_id, ticket_id, ticket_no, effective_at, line_count, pinned_order),
    )


def list_batch_ticket_nos(conn: sqlite3.Connection, batch_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT ticket_no FROM apply_batch_tickets WHERE batch_id=? ORDER BY pinned_order",
        (batch_id,),
    ).fetchall()
    return [r["ticket_no"] for r in rows]


def list_batch_tickets(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM apply_batch_tickets WHERE batch_id=? ORDER BY pinned_order",
        (batch_id,),
    ).fetchall()
    return [dict(r) for r in rows]
