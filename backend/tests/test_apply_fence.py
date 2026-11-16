"""Create-during-apply fence tests.

Covers:
- pin at apply start (ticket list + effective time)
- defer policy: create during apply lands as scheduled and stays out of the batch
- reject policy: create and reschedule fail while the lock is held
- reschedule is always rejected while a lock is active
- batch detail set == processed subset of the pinned set
- price book / history only carry pinned ticket numbers (batch & tags same source)
- counters and fence events are auditable

Service-layer calls inside the pin hooks run on real, independent DB
connections (the lock is already committed), while HTTP calls outside the
hooks exercise the full API.
"""
from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException

from app.services import apply_service
from app.services import ticket_service


CLOCK = "2026-11-16T12:00:00Z"


def _set_clock(client, clock: str = CLOCK):
    r = client.put("/api/apply/clock", json={"business_clock": clock})
    assert r.status_code == 200, r.text


def _set_policy(client, policy: str):
    r = client.put("/api/apply/fence/policy", json={"policy": policy})
    assert r.status_code == 200, r.text


def _first_sku(client) -> str:
    r = client.get("/api/skus")
    assert r.status_code == 200
    return r.json()[0]["sku_code"]


def _make_ticket(client, title: str, effective_at: str, price: float, sku: str | None = None):
    sku = sku or _first_sku(client)
    r = client.post(
        "/api/tickets",
        json={
            "title": title,
            "effective_at": effective_at,
            "lines": [{"sku_code": sku, "new_price": price}],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _service_create(title: str, effective_at: str, price: float, sku: str) -> dict:
    return ticket_service.create_ticket(
        {
            "title": title,
            "effective_at": effective_at,
            "lines": [{"sku_code": sku, "new_price": price}],
        }
    )


def _pin_hook(callable_):
    """Install a single-fire hook so a later apply in the same test cannot
    re-trigger it (the seam is a module-level global)."""
    def wrapper(lock, pinned):
        apply_service._on_pinned = None
        return callable_(lock, pinned)

    apply_service._on_pinned = wrapper


def test_pin_excludes_ticket_created_during_apply_under_defer(client):
    _set_policy(client, "defer")
    sku = _first_sku(client)
    t1 = _make_ticket(client, "先于应用建单", "2026-11-16T10:00:00Z", 11.5, sku)
    _set_clock(client)

    captured = {}

    def during_apply(lock, pinned):
        captured["lock"] = lock
        captured["pinned"] = [t["ticket_no"] for t in pinned]
        # The lock is committed: a separate connection must observe it.
        fence = apply_service.fence_status()
        assert fence["active"] is True
        assert fence["policy"] == "defer"
        # New ticket, effective time already due — it must still be fenced out.
        t2 = _service_create("钉扎后建单", "2026-11-16T09:00:00Z", 99.9, sku)
        assert t2["status"] == "scheduled"
        assert t2["deferred_to_later_batch"] is True
        assert t2["lock_batch_no"] == lock["batch_no"]
        captured["t2"] = t2

    _pin_hook(during_apply)
    r = client.post("/api/apply/due")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    assert body["pinned_tickets"] == [t1["ticket_no"]]
    # The new ticket must not appear in the success detail.
    assert captured["t2"]["ticket_no"] not in body["tickets"]
    assert body["tickets"] == [t1["ticket_no"]]

    # Lock released afterwards.
    assert client.get("/api/apply/fence").json()["active"] is False

    # Batch detail set == processed subset of the pinned set.
    detail = client.get(f"/api/apply/batches/{body['batch_no']}").json()
    assert detail["ticket_nos"] == [t1["ticket_no"]]
    assert detail["pinned_count"] == 1
    assert detail["ticket_count"] == 1

    # The deferred ticket is still scheduled and points at no applied batch.
    t2_after = client.get(f"/api/tickets/{captured['t2']['id']}").json()
    assert t2_after["status"] == "scheduled"
    assert t2_after["apply_batch_id"] is None

    # Price book / history must only carry the pinned ticket's number.
    book = client.get("/api/prices/book").json()
    row = next(b for b in book if b["sku_code"] == sku)
    assert row["current_price"] == 11.5
    assert row["source_ticket_no"] == t1["ticket_no"]

    history = client.get(f"/api/prices/history?sku_code={sku}").json()
    applied_sources = {h["source_ticket_no"] for h in history if h["source_ticket_no"]}
    assert t1["ticket_no"] in applied_sources
    assert captured["t2"]["ticket_no"] not in applied_sources

    # Audit: deferred create is counted on the lock row and recorded as an event.
    events = client.get("/api/apply/fence/events").json()["events"]
    deferred = [e for e in events if e["event_type"] == "deferred_create"]
    assert len(deferred) == 1
    assert deferred[0]["ticket_no"] == captured["t2"]["ticket_no"]
    pinned_events = [e for e in events if e["event_type"] == "pinned"]
    assert [e["ticket_no"] for e in pinned_events] == [t1["ticket_no"]]
    lock_row = client.get("/api/apply/fence/locks").json()["locks"][0]
    assert lock_row["deferred_creates"] == 1
    assert lock_row["rejected_creates"] == 0
    assert lock_row["status"] == "released"

    # The deferred ticket applies in a later batch.
    r2 = client.post("/api/apply/due")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["applied"] is True
    assert body2["tickets"] == [captured["t2"]["ticket_no"]]
    row2 = next(b for b in client.get("/api/prices/book").json() if b["sku_code"] == sku)
    assert row2["current_price"] == 99.9
    assert row2["source_ticket_no"] == captured["t2"]["ticket_no"]


def test_reject_policy_fails_create_while_lock_held(client):
    _set_policy(client, "reject")
    sku = _first_sku(client)
    t1 = _make_ticket(client, "先于应用建单", "2026-11-16T10:00:00Z", 12.0, sku)
    _set_clock(client)

    rejected = {}

    def during_apply(lock, pinned):
        with pytest.raises(HTTPException) as exc:
            _service_create("持锁期间硬建", "2026-11-16T09:00:00Z", 88.8, sku)
        assert exc.value.status_code == 409
        rejected["detail"] = exc.value.detail

    _pin_hook(during_apply)
    body = client.post("/api/apply/due").json()
    assert body["tickets"] == [t1["ticket_no"]]
    assert "reject" in rejected["detail"]

    # No ticket was created by the rejected request.
    tickets = client.get("/api/tickets").json()
    assert all(t["title"] != "持锁期间硬建" for t in tickets)

    # Rejected create is auditable.
    events = client.get("/api/apply/fence/events").json()["events"]
    rejects = [e for e in events if e["event_type"] == "rejected_create"]
    assert len(rejects) == 1
    assert rejects[0]["batch_no"] == body["batch_no"]
    lock_row = client.get("/api/apply/fence/locks").json()["locks"][0]
    assert lock_row["rejected_creates"] == 1
    assert lock_row["deferred_creates"] == 0

    # Price book never carries a rejected ticket.
    row = next(b for b in client.get("/api/prices/book").json() if b["sku_code"] == sku)
    assert row["source_ticket_no"] == t1["ticket_no"]
    assert row["current_price"] == 12.0


def test_reschedule_always_rejected_while_lock_held(client):
    _set_policy(client, "defer")
    sku = _first_sku(client)
    t1 = _make_ticket(client, "将被钉扎", "2026-11-16T10:00:00Z", 13.0, sku)
    t2 = _make_ticket(client, "未到期不进批", "2026-12-31T00:00:00Z", 14.0, sku)
    _set_clock(client)

    def during_apply(lock, pinned):
        # Even under defer policy, rescheduling is rejected.
        with pytest.raises(HTTPException) as e1:
            ticket_service.reschedule_ticket(t1["id"], CLOCK)
        assert e1.value.status_code == 409
        with pytest.raises(HTTPException) as e2:
            ticket_service.reschedule_ticket(t2["id"], CLOCK)
        assert e2.value.status_code == 409

    _pin_hook(during_apply)
    body = client.post("/api/apply/due").json()
    assert body["pinned_tickets"] == [t1["ticket_no"]]

    # Effective times unchanged after the failed reschedules.
    assert client.get(f"/api/tickets/{t1['id']}").json()["effective_at"] == "2026-11-16T10:00:00Z"
    assert client.get(f"/api/tickets/{t2['id']}").json()["effective_at"] == "2026-12-31T00:00:00Z"

    # Once the lock is released, rescheduling works again (HTTP path).
    r = client.patch(f"/api/tickets/{t2['id']}/reschedule", json={"effective_at": CLOCK})
    assert r.status_code == 200
    assert r.json()["effective_at"] == CLOCK

    events = client.get("/api/apply/fence/events").json()["events"]
    assert len([e for e in events if e["event_type"] == "rejected_reschedule"]) == 2
    lock_row = client.get("/api/apply/fence/locks").json()["locks"][0]
    assert lock_row["rejected_reschedules"] == 2


def test_batch_detail_equals_processed_subset_and_tags_same_source(client):
    _set_policy(client, "defer")
    skus = [s["sku_code"] for s in client.get("/api/skus").json()][:2]
    t1 = _make_ticket(client, "批内单A", "2026-11-16T08:00:00Z", 1.0, skus[0])
    t2 = _make_ticket(client, "批内单B", "2026-11-16T09:00:00Z", 2.0, skus[1])
    _set_clock(client)

    created_during = []

    def during_apply(lock, pinned):
        # Two tickets born after the pin, both already due.
        created_during.append(_service_create("批外单X", "2026-11-16T09:30:00Z", 3.0, skus[0]))
        created_during.append(_service_create("批外单Y", "2026-11-16T09:45:00Z", 4.0, skus[1]))

    _pin_hook(during_apply)
    body = client.post("/api/apply/due").json()
    pinned_nos = {t1["ticket_no"], t2["ticket_no"]}
    outside_nos = {t["ticket_no"] for t in created_during}

    assert set(body["pinned_tickets"]) == pinned_nos
    assert set(body["tickets"]) == pinned_nos
    assert outside_nos.isdisjoint(set(body["tickets"]))

    detail = client.get(f"/api/apply/batches/{body['batch_no']}").json()
    assert set(detail["ticket_nos"]) == pinned_nos
    assert detail["pinned_count"] == 2
    assert detail["ticket_count"] == 2
    assert detail["line_count"] == 2

    # Every price tag written by the batch traces to a pinned ticket only.
    book = client.get("/api/prices/book").json()
    for sku_code, expected_no, expected_price in [
        (skus[0], t1["ticket_no"], 1.0),
        (skus[1], t2["ticket_no"], 2.0),
    ]:
        row = next(b for b in book if b["sku_code"] == sku_code)
        assert row["source_ticket_no"] == expected_no
        assert row["current_price"] == expected_price

    history = client.get("/api/prices/history").json()
    batch_sources = {h["source_ticket_no"] for h in history if h["source_ticket_no"]}
    assert outside_nos.isdisjoint(batch_sources)

    # Ticket-level linkage: outside tickets have no apply batch.
    for t in created_during:
        fresh = client.get(f"/api/tickets/{t['id']}").json()
        assert fresh["apply_batch_id"] is None
        assert fresh["status"] == "scheduled"

    # Fence is idle again.
    assert client.get("/api/apply/fence").json()["active"] is False


def test_concurrent_create_during_real_apply_is_deferred(client):
    """Without the test seam: a real thread racing the running apply.

    A barrier parks the worker until the pin transaction has committed, so the
    create runs against the durable lock through a separate connection.
    """
    import sqlite3
    import time

    from app.config import DB_PATH

    _set_policy(client, "defer")
    sku = _first_sku(client)
    t1 = _make_ticket(client, "先于应用建单", "2026-11-16T10:00:00Z", 21.0, sku)
    _set_clock(client)

    barrier = threading.Barrier(2)
    result_box = {}

    def worker():
        # Wait until the apply thread has pinned and published the lock.
        barrier.wait()
        result_box["ticket"] = _service_create("并发建单", "2026-11-16T09:00:00Z", 77.7, sku)

    def during_apply(lock, pinned):
        # The lock row must be visible from an independent connection.
        probe = sqlite3.connect(DB_PATH)
        try:
            active = probe.execute(
                "SELECT id FROM apply_locks WHERE status='active'"
            ).fetchall()
            assert len(active) == 1
        finally:
            probe.close()
        barrier.wait()
        # Hold the fence open until the worker's create has committed.
        while "ticket" not in result_box:
            time.sleep(0.005)

    thread = threading.Thread(target=worker)
    thread.start()
    _pin_hook(during_apply)
    body = client.post("/api/apply/due").json()
    thread.join()

    raced = result_box["ticket"]
    assert body["tickets"] == [t1["ticket_no"]]
    assert raced["ticket_no"] not in body["tickets"]
    assert raced["deferred_to_later_batch"] is True
    # It must be pending for a later batch.
    assert client.get(f"/api/tickets/{raced['id']}").json()["status"] == "scheduled"

    # The deferred ticket is picked up by the next apply.
    next_body = client.post("/api/apply/due").json()
    assert next_body["tickets"] == [raced["ticket_no"]]


def test_apply_without_due_tickets_keeps_fence_idle(client):
    _set_clock(client, "2026-11-16T00:00:00Z")
    r = client.post("/api/apply/due")
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is False
    assert body["pinned_count"] == 0
    assert client.get("/api/apply/fence").json()["active"] is False


def test_policy_validation(client):
    r = client.put("/api/apply/fence/policy", json={"policy": "nope"})
    assert r.status_code == 400
    assert client.get("/api/apply/fence/policy").json()["options"] == ["reject", "defer"]


def test_crashed_active_lock_is_recovered_on_next_apply(client):
    """A lock left active by a crashed process must not block later applies."""
    import sqlite3

    from app.config import DB_PATH

    _set_policy(client, "defer")
    sku = _first_sku(client)
    t1 = _make_ticket(client, "第一批单", "2026-11-16T10:00:00Z", 11.0, sku)
    t2 = _make_ticket(client, "第二批单", "2026-11-16T13:00:00Z", 22.0, sku)
    _set_clock(client, "2026-11-16T12:00:00Z")

    first = client.post("/api/apply/due").json()
    assert first["tickets"] == [t1["ticket_no"]]

    # Simulate a crash during the processing phase: lock stays active and its
    # batch stays running, exactly as a dead process would leave them.
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE apply_locks SET status='active', released_at=NULL")
    conn.execute("UPDATE apply_batches SET status='running' WHERE status='done'")
    conn.commit()
    conn.close()
    assert client.get("/api/apply/fence").json()["active"] is True

    # Startup recovery and the next apply must reclaim, not 409 forever.
    assert apply_service.recover_on_startup() >= 1
    # Recovery is idempotent: a second pass finds nothing active.
    assert apply_service.recover_on_startup() == 0

    # Advance the clock so the second ticket is now due.
    _set_clock(client, "2026-11-16T14:00:00Z")
    second = client.post("/api/apply/due")
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["tickets"] == [t2["ticket_no"]]

    locks = client.get("/api/apply/fence/locks").json()["locks"]
    by_batch = {l["batch_no"]: l["status"] for l in locks}
    assert by_batch[first["batch_no"]] == "stale"
    assert by_batch[body["batch_no"]] == "released"

    first_batch = next(
        b for b in client.get("/api/apply/batches").json()
        if b["batch_no"] == first["batch_no"]
    )
    assert first_batch["status"] == "failed"
    assert client.get("/api/apply/fence").json()["active"] is False


def test_reject_policy_create_succeeds_again_after_lock_release(client):
    """The reject fence must not leak: creates work normally once released."""
    _set_policy(client, "reject")
    sku = _first_sku(client)
    _make_ticket(client, "先于应用建单", "2026-11-16T10:00:00Z", 31.0, sku)
    _set_clock(client)

    def during_apply(lock, pinned):
        with pytest.raises(HTTPException) as exc:
            _service_create("持锁期间硬建", "2026-11-16T09:00:00Z", 88.8, sku)
        assert exc.value.status_code == 409

    _pin_hook(during_apply)
    client.post("/api/apply/due")

    after = _make_ticket(client, "锁释放后建单", "2026-11-16T13:00:00Z", 32.0, sku)
    assert after["status"] == "scheduled"
    assert "deferred_to_later_batch" not in after
