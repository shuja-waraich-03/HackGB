"""Postgres backend (core/db_pg.py) against a real database.

Skipped unless TEST_DATABASE_URL is set. Runs in a throwaway schema
`rehome_test`, so pointing it at the team's Tiger service doesn't touch the
real tables:

    $env:TEST_DATABASE_URL = "postgres://..."; python -m pytest tests/test_tiger.py
"""
import os
from pathlib import Path

import pytest

URL = os.getenv("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

SCHEMA = "rehome_test"
DIM = 8


@pytest.fixture(scope="module")
def pg():
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    from app.core import db_pg
    from scripts.init_db import SCHEMA as SQL_FILE, statements

    with psycopg.connect(URL, autocommit=True) as conn:
        has_timescale = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'").fetchone()
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(f"SET search_path = {SCHEMA}, public")
        sql = Path(SQL_FILE).read_text(encoding="utf-8").replace("{EMBED_DIM}", str(DIM))
        for stmt in statements(sql):
            # Plain Postgres (no TimescaleDB) can still test everything else.
            if not has_timescale and ("timescale" in stmt or "hypertable" in stmt
                                      or "impact_hourly" in stmt):
                continue
            conn.execute(stmt)

    pool = ConnectionPool(URL, min_size=1, max_size=2, open=True,
                          kwargs={"autocommit": True, "row_factory": dict_row,
                                  "options": f"-c search_path={SCHEMA},public"})
    old = db_pg._pool
    db_pg._pool, db_pg._listing_cols = pool, None
    yield db_pg
    db_pg._pool, db_pg._listing_cols = old, None
    pool.close()
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {SCHEMA}.impact_hourly CASCADE")
        conn.execute(f"DROP SCHEMA {SCHEMA} CASCADE")


def _vec(*hot):
    v = [0.0] * DIM
    for i in hot:
        v[i] = 1.0
    return v


def _item(pg, vec, status="available", **kw):
    row = {"id": pg.new_item_id(), "donor_id": "d", "zip": "54311", "category": "outerwear",
           "item": "coat", "color": "black", "size": None, "condition": "good",
           "season": "winter", "attributes": ["insulated", "hooded"], "summary": "A coat.",
           "image_url": None, "note": None, "status": status, "embedding": vec,
           "created_at": pg.now(), **kw}
    pg.insert_item(row)
    return row["id"]


def test_items_roundtrip_and_status(pg):
    item_id = _item(pg, _vec(0))
    row = pg.get_item(item_id)
    assert row["attributes"] == ["insulated", "hooded"]
    assert "embedding" not in row
    pg.update_item_status(item_id, "reserved")
    assert pg.get_item(item_id)["status"] == "reserved"
    assert item_id in [r["id"] for r in pg.list_items("reserved")]
    assert pg.get_item("itm_nope") is None


def test_ids_are_unique_and_formatted(pg):
    a, b = pg.new_item_id(), pg.new_item_id()
    assert a != b and a.startswith("itm_")
    assert pg.new_request_id().startswith("req_")
    assert pg.new_listing_id().startswith("lst_")


def test_vector_search_orders_by_similarity_and_filters_status(pg):
    near = _item(pg, _vec(1, 2))
    far = _item(pg, _vec(5))
    hidden = _item(pg, _vec(1, 2), status="reserved")
    zero = _item(pg, [0.0] * DIM)
    got = pg.vector_search(_vec(1, 2), top_k=50)
    ids = [r["id"] for r, _ in got]
    assert hidden not in ids
    assert ids.index(near) < ids.index(far)
    scores = dict((r["id"], s) for r, s in got)
    assert scores[near] == pytest.approx(1.0)
    assert scores[zero] == 0.0            # NaN from a zero vector becomes 0


def test_requests_and_matches(pg):
    item_id = _item(pg, _vec(3))
    req_id = pg.new_request_id()
    pg.insert_request({"id": req_id, "requester_id": "r", "raw_text": "coat", "zip": "60601",
                       "needs": ["winter coat"], "inferred_context": ["snow"],
                       "must_have": ["insulated"], "nice_to_have": [], "size": None,
                       "search_text": "insulated coat", "embedding": _vec(3),
                       "status": "matched", "created_at": pg.now()})
    pg.insert_matches([{"request_id": req_id, "item_id": item_id, "score": 0.9,
                        "rank": 1, "reason": "Warm", "status": "suggested"}])
    with pg._db().connection() as conn:
        req = conn.execute("SELECT needs FROM requests WHERE id = %s", (req_id,)).fetchone()
        m = conn.execute("SELECT id, rank FROM matches WHERE request_id = %s",
                         (req_id,)).fetchone()
    assert req["needs"] == ["winter coat"]
    assert m["id"].startswith("mat_") and m["rank"] == 1

    assert pg.get_request(req_id)["status"] == "matched" and pg.get_request("req_x") is None
    match = pg.get_match(req_id, item_id)
    assert match["id"] == m["id"] and pg.get_match(req_id, "itm_x") is None
    assert pg.reserve_item(item_id) is True
    assert pg.reserve_item(item_id) is False          # already reserved
    pg.update_match_status(match["id"], "accepted")
    pg.update_request_status(req_id, "closed")
    assert pg.get_match(req_id, item_id)["status"] == "accepted"
    assert pg.get_request(req_id)["status"] == "closed"
    assert req_id not in [r["id"] for r, _ in pg.open_requests_near(_vec(3), top_k=50)]


def test_open_requests_near(pg):
    req_id = pg.new_request_id()
    pg.insert_request({"id": req_id, "requester_id": "r", "raw_text": "lamp", "zip": "54311",
                       "needs": ["lamp"], "inferred_context": [], "must_have": [],
                       "nice_to_have": [], "size": None, "search_text": "lamp",
                       "embedding": _vec(6), "status": "open", "created_at": pg.now()})
    got = pg.open_requests_near(_vec(6), top_k=50)
    assert got[0][0]["id"] == req_id and got[0][1] == pytest.approx(1.0)
    assert got[0][0]["needs"] == ["lamp"]


def test_listings(pg):
    item_id = _item(pg, _vec(4))
    lst_id = pg.new_listing_id()
    now = pg.now()
    pg.insert_listing({"id": lst_id, "item_id": item_id, "platform": "nostr",
                       "external_id": "ab", "price": 24.99, "listed_at": now,
                       "expires_at": now, "url": "https://x", "status": "active",
                       "dry_run": True, "relays_accepted": [],
                       "title": "Black Puffer, Size M", "description": "Warm."})
    active = {r["id"]: r for r in pg.list_listings("active")}
    assert active[lst_id]["price"] == 24.99
    assert active[lst_id]["title"] == "Black Puffer, Size M"
    pg.update_listing(lst_id, status="expired")
    assert lst_id in [r["id"] for r in pg.list_listings("expired")]
    with pytest.raises(ValueError):
        pg.update_listing(lst_id, bogus=1)


def test_drafts_roundtrip_and_pop_once(pg):
    draft_id = pg.new_draft_id()
    item = {"category": "outerwear", "item": "coat", "attributes": ["hooded"]}
    pg.insert_draft({"id": draft_id, "donor_id": "d", "zip": "54311", "note": None,
                     "image_url": None, "item": item, "created_at": pg.now()})
    assert draft_id.startswith("drf_") and pg.get_draft(draft_id)["item"] == item
    assert pg.pop_draft(draft_id)["zip"] == "54311"
    assert pg.pop_draft(draft_id) is None and pg.get_draft(draft_id) is None


def test_impact_aggregate_sees_fresh_events(pg):
    with pg._db().connection() as conn:
        has_view = conn.execute("SELECT to_regclass('impact_hourly') IS NOT NULL AS ok"
                                ).fetchone()["ok"]
    if not has_view:
        pytest.skip("TimescaleDB not available")
    before = sum(r["n"] for r in pg.impact_rows() if r["event_type"] == "match_accepted")
    for _ in range(2):
        pg.insert_event({"agent": "orchestrator", "message": "x", "event_type": "match_accepted",
                         "category": "home", "created_at": pg.now()})
    pg.insert_event({"agent": "orchestrator", "message": "untyped", "created_at": pg.now()})
    rows = [r for r in pg.impact_rows() if r["event_type"] == "match_accepted"]
    assert sum(r["n"] for r in rows) == before + 2     # real-time: no refresh needed
    assert all(r["event_type"] is not None for r in pg.impact_rows())


def test_events_feed_order_and_fields(pg):
    a = pg.insert_event({"agent": "parser", "message": "one", "event_type": "item_parsed",
                         "category": "outerwear", "created_at": pg.now()})
    b = pg.insert_event({"agent": "orchestrator", "message": "two", "created_at": pg.now()})
    got = pg.list_events(after=a["id"] - 1)
    assert [e["message"] for e in got][:2] == ["one", "two"]
    assert got[0]["event_type"] == "item_parsed" and got[0]["created_at"] is not None
    assert pg.list_events(after=b["id"]) == []


def test_agent_trace_columns(pg):
    pg.insert_event({"agent": "concierge", "message": "← $15–23", "run_id": "run_t1",
                     "tool": "get_price_quote", "duration_ms": 812,
                     "data": {"low": 15, "high": 23}, "created_at": pg.now()})
    pg.insert_event({"agent": "orchestrator", "message": "untraced", "created_at": pg.now()})
    [e] = pg.list_events(run_id="run_t1")
    assert (e["tool"], e["duration_ms"], e["data"]) == ("get_price_quote", 812, {"low": 15, "high": 23})


def test_listing_nostr_url_roundtrip(pg):
    item_id = _item(pg, _vec(6))
    lst_id = pg.new_listing_id()
    now = pg.now()
    pg.insert_listing({"id": lst_id, "item_id": item_id, "platform": "nostr",
                       "external_id": "cd", "price": 25.0, "listed_at": now,
                       "expires_at": now, "url": "https://shopstr.store/listing/naddr1x",
                       "nostr_url": "https://njump.me/naddr1x", "status": "active",
                       "dry_run": False, "relays_accepted": ["wss://relay.damus.io"]})
    row = {r["id"]: r for r in pg.list_listings()}[lst_id]
    assert row["nostr_url"] == "https://njump.me/naddr1x"
    assert row["relays_accepted"] == ["wss://relay.damus.io"]


def test_reset_clears_inventory_keeps_events(pg):
    """Keep this test last in the file: it empties the module's tables."""
    item_id = _item(pg, _vec(7))
    ev = pg.insert_event({"agent": "orchestrator", "message": "before reset",
                          "created_at": pg.now()})
    pg.reset()
    assert pg.get_item(item_id) is None and pg.list_items() == []
    assert pg.list_listings() == []
    assert pg.new_item_id() == "itm_0001" and pg.new_listing_id() == "lst_0001"
    assert [e["message"] for e in pg.list_events(after=ev["id"] - 1)][0] == "before reset"
