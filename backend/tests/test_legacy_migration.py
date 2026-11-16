"""Migration: databases created before the apply fence must upgrade in place."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

OLD_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE skus (
  id INTEGER PRIMARY KEY, sku_code TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
  category TEXT NOT NULL, list_price REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE price_tickets (
  id INTEGER PRIMARY KEY, ticket_no TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
  status TEXT NOT NULL, effective_at TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
  applied_at TEXT, apply_batch_id INTEGER
);
CREATE TABLE price_ticket_lines (
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL,
  sku_code TEXT NOT NULL, old_price REAL NOT NULL, new_price REAL NOT NULL
);
CREATE TABLE apply_batches (
  id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE, clock_at TEXT NOT NULL,
  ticket_count INTEGER NOT NULL, line_count INTEGER NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE price_book (
  sku_code TEXT PRIMARY KEY, current_price REAL NOT NULL,
  source_ticket_no TEXT, effective_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE price_history (
  id INTEGER PRIMARY KEY, sku_code TEXT NOT NULL, price REAL NOT NULL,
  source_ticket_no TEXT, effective_at TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE sales_fixture (
  id INTEGER PRIMARY KEY, sale_no TEXT NOT NULL UNIQUE, sku_code TEXT NOT NULL,
  sold_at TEXT NOT NULL, unit_price REAL NOT NULL, qty INTEGER NOT NULL
);
CREATE TABLE hold_applies (
  id INTEGER PRIMARY KEY, ticket_id INTEGER, status TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _build_legacy_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO skus(sku_code, title, category, list_price, enabled) "
        "VALUES ('SKU-OLD-1','旧商品','tea',10.0,1)"
    )
    conn.execute(
        "INSERT INTO price_book(sku_code, current_price, source_ticket_no, effective_at, updated_at) "
        "VALUES ('SKU-OLD-1',10.0,NULL,'2026-11-16T09:00:00Z','2026-11-16T09:00:00Z')"
    )
    conn.execute(
        "INSERT INTO price_tickets(ticket_no, title, status, effective_at, created_at) "
        "VALUES ('PT-0001','旧单','scheduled','2026-11-16T10:00:00Z','2026-11-16T09:00:00Z')"
    )
    conn.execute(
        "INSERT INTO price_ticket_lines(ticket_id, sku_code, old_price, new_price) "
        "VALUES (1,'SKU-OLD-1',10.0,12.5)"
    )
    conn.execute(
        "INSERT INTO settings(key,value) VALUES ('business_clock','2026-11-16T12:00:00Z')"
    )
    conn.commit()
    conn.close()


def test_legacy_schema_migrates_in_place():
    """Schema-level migration: new tables/columns appear, old data survives."""
    from app.seed import init_schema

    tmp = tempfile.mkdtemp(prefix="pricecab-legacy-")
    db_path = os.path.join(tmp, "pricecab.db")
    _build_legacy_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)  # must be idempotent and add the missing pieces
    init_schema(conn)  # running twice must not error
    conn.commit()

    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"apply_locks", "apply_batch_tickets", "apply_fence_events"} <= tables

    ticket_cols = {r[1] for r in conn.execute("PRAGMA table_info(price_tickets)")}
    assert "fence_lock_id" in ticket_cols
    batch_cols = {r[1] for r in conn.execute("PRAGMA table_info(apply_batches)")}
    assert {"pinned_count", "fence_lock_id"} <= batch_cols

    # Legacy rows survive and new columns take safe defaults.
    row = conn.execute("SELECT * FROM price_tickets WHERE ticket_no='PT-0001'").fetchone()
    assert row["status"] == "scheduled"
    assert dict(row)["fence_lock_id"] is None
    conn.close()


def test_migrated_legacy_db_runs_full_fenced_apply():
    """End-to-end in a clean subprocess so module imports pick DATA_DIR fresh."""
    tmp = tempfile.mkdtemp(prefix="pricecab-legacy-e2e-")
    db_path = os.path.join(tmp, "pricecab.db")
    _build_legacy_db(db_path)

    script = """
import os
from fastapi.testclient import TestClient
from app.seed import seed_if_empty
from app.main import app

seed_if_empty()  # migrates the legacy schema in place; skips seeding (not empty)
with TestClient(app) as client:
    assert client.put('/api/apply/fence/policy', json={'policy': 'defer'}).status_code == 200
    body = client.post('/api/apply/due').json()
    assert body['applied'] is True
    assert body['tickets'] == ['PT-0001'], body
    detail = client.get(f"/api/apply/batches/{body['batch_no']}").json()
    assert detail['ticket_nos'] == ['PT-0001']
    book = client.get('/api/prices/book').json()
    row = next(b for b in book if b['sku_code'] == 'SKU-OLD-1')
    assert row['current_price'] == 12.5
    assert row['source_ticket_no'] == 'PT-0001'
    print('LEGACY_E2E_OK')
"""
    env = {**os.environ, "DATA_DIR": tmp, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "LEGACY_E2E_OK" in proc.stdout
