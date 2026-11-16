from __future__ import annotations

import sqlite3
from typing import Any


def next_batch_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"] + 1
    return f"AB-{n:04d}"


def create_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    clock_at: str,
    ticket_count: int,
    line_count: int,
    status: str,
    created_at: str,
    pinned_count: int = 0,
    fence_lock_id: int | None = None,
) -> int:
    cur = conn.execute(
        '''
        INSERT INTO apply_batches(
            batch_no, clock_at, ticket_count, line_count, status, created_at,
            pinned_count, fence_lock_id
        )
        VALUES (?,?,?,?,?,?,?,?)
        ''',
        (batch_no, clock_at, ticket_count, line_count, status, created_at,
         pinned_count, fence_lock_id),
    )
    return int(cur.lastrowid)


def update_totals(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    ticket_count: int,
    line_count: int,
    status: str,
) -> None:
    conn.execute(
        "UPDATE apply_batches SET ticket_count=?, line_count=?, status=? WHERE id=?",
        (ticket_count, line_count, status, batch_id),
    )


def mark_status(conn: sqlite3.Connection, batch_id: int, status: str) -> None:
    conn.execute(
        "UPDATE apply_batches SET status=? WHERE id=?",
        (status, batch_id),
    )


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM apply_batches WHERE id=?", (batch_id,)
    ).fetchone()
    return dict(row) if row else None


def get_batch_by_no(conn: sqlite3.Connection, batch_no: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM apply_batches WHERE batch_no=?", (batch_no,)
    ).fetchone()
    return dict(row) if row else None


def list_batches(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM apply_batches ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
