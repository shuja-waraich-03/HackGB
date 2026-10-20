"""Tiger Data (Postgres + pgvector + TimescaleDB) implementation of core/db.py.

Same function signatures as the in-memory stub; core/db.py swaps these in
when DATABASE_URL is set. Create the tables first: python scripts/init_db.py
"""
import math
from datetime import datetime, timezone
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from app.core import config

ITEM_COLS = ["id", "donor_id", "image_url", "note", "zip", "category", "item", "brand",
             "color", "size", "condition", "season", "attributes", "summary", "status",
             "created_at"]
REQUEST_COLS = ["id", "requester_id", "raw_text", "zip", "needs", "inferred_context",
                "must_have", "nice_to_have", "size", "search_text", "status", "created_at"]
LISTING_COLS = ["id", "item_id", "platform", "external_id", "price", "listed_at",
                "expires_at", "url", "nostr_url", "status", "dry_run", "relays_accepted",
                "title", "description"]
_ITEM_SELECT = ", ".join(ITEM_COLS)

_pool: ConnectionPool | None = None
_listing_cols: list[str] | None = None


def _db() -> ConnectionPool:
    """Opened on first use so importing the app never needs the network."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=5, open=True,
                               kwargs={"autocommit": True, "row_factory": dict_row})
    return _pool


def _one(sql: str, params=()) -> dict | None:
    with _db().connection() as conn:
        return conn.execute(sql, params).fetchone()


def _all(sql: str, params=()) -> list[dict]:
    with _db().connection() as conn:
        return conn.execute(sql, params).fetchall()


def _run(sql: str, params=()) -> None:
    with _db().connection() as conn:
        conn.execute(sql, params)


def _vec(v: list[float]) -> str:
    """pgvector text form, cast with ::vector in SQL."""
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def _insert(table: str, cols: list[str], row: dict, extra: dict | None = None) -> None:
    """INSERT the given columns from row; `extra` maps column -> SQL expression
    (e.g. embedding -> %s::vector) with its value."""
    names, marks, values = [], [], []
    for c in cols:
        if c in row:
            names.append(c)
            marks.append("%s")
            values.append(row[c])
    for c, (expr, value) in (extra or {}).items():
        names.append(c)
        marks.append(expr)
        values.append(value)
    _run(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(marks)})", values)


def reset() -> None:
    """Empty the demo inventory and restart its ids at 0001 (POST /api/admin/reset).
    Events are kept: nothing references them, and the feed polls by id."""
    with _db().connection() as conn:
        conn.execute("TRUNCATE matches, listings, requests, items")
        for seq in ("item_seq", "request_seq", "match_seq", "listing_seq"):
            conn.execute(f"ALTER SEQUENCE {seq} RESTART WITH 1")


def now() -> datetime:
    return datetime.now(timezone.utc)


def _next(seq: str) -> int:
    return _one(f"SELECT nextval('{seq}') AS n")["n"]


def new_item_id() -> str:
    return f"itm_{_next('item_seq'):04d}"


def new_request_id() -> str:
    return f"req_{_next('request_seq'):04d}"


def new_listing_id() -> str:
    return f"lst_{_next('listing_seq'):04d}"


def new_draft_id() -> str:
    return f"drf_{_next('draft_seq'):04d}"


# ---- drafts ----------------------------------------------------------------
_DRAFT_SELECT = "id, donor_id, zip, note, image_url, item, created_at"


def insert_draft(row: dict) -> dict:
    _run("""INSERT INTO drafts (id, donor_id, zip, note, image_url, item, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
         (row["id"], row["donor_id"], row["zip"], row.get("note"), row.get("image_url"),
          Jsonb(row["item"]), row["created_at"]))
    return row


def get_draft(draft_id: str) -> dict | None:
    return _one(f"SELECT {_DRAFT_SELECT} FROM drafts WHERE id = %s", (draft_id,))


def pop_draft(draft_id: str) -> dict | None:
    """Atomic: two concurrent donates of one draft can't both get it."""
    return _one(f"DELETE FROM drafts WHERE id = %s RETURNING {_DRAFT_SELECT}", (draft_id,))


# ---- items -----------------------------------------------------------------
def insert_item(row: dict) -> dict:
    extra = {"embedding": ("%s::vector", _vec(row["embedding"]))} if row.get("embedding") else {}
    _insert("items", ITEM_COLS, row, extra)
    return row


def get_item(item_id: str) -> dict | None:
    return _one(f"SELECT {_ITEM_SELECT} FROM items WHERE id = %s", (item_id,))


def list_items(status: str | None = None) -> list[dict]:
    if status is None:
        return _all(f"SELECT {_ITEM_SELECT} FROM items ORDER BY created_at, id")
    return _all(f"SELECT {_ITEM_SELECT} FROM items WHERE status = %s ORDER BY created_at, id",
                (status,))


def update_item_status(item_id: str, status: str) -> None:
    _run("UPDATE items SET status = %s WHERE id = %s", (status, item_id))


def vector_search(query_vec: list[float], top_k: int,
                  status: str = "available") -> list[tuple[dict, float]]:
    """pgvector cosine search (HNSW index). Score = 1 - cosine distance."""
    rows = _all(f"""SELECT {_ITEM_SELECT}, 1 - (embedding <=> %(q)s::vector) AS score
                    FROM items WHERE status = %(status)s AND embedding IS NOT NULL
                    ORDER BY embedding <=> %(q)s::vector LIMIT %(k)s""",
                {"q": _vec(query_vec), "status": status, "k": top_k})
    out = []
    for r in rows:
        score = r.pop("score")
        # A zero vector has no direction; pgvector returns NaN for it.
        out.append((r, 0.0 if score is None or math.isnan(score) else float(score)))
    return out


# ---- requests & matches ----------------------------------------------------
def insert_request(row: dict) -> dict:
    extra = {"embedding": ("%s::vector", _vec(row["embedding"]))} if row.get("embedding") else {}
    _insert("requests", REQUEST_COLS, row, extra)
    return row


def open_requests_near(query_vec: list[float], top_k: int = 5) -> list[tuple[dict, float]]:
    """pgvector search over requests that aren't closed (the matchmaker's view)."""
    rows = _all(f"""SELECT {', '.join(REQUEST_COLS)}, 1 - (embedding <=> %(q)s::vector) AS score
                    FROM requests WHERE status <> 'closed' AND embedding IS NOT NULL
                    ORDER BY embedding <=> %(q)s::vector LIMIT %(k)s""",
                {"q": _vec(query_vec), "k": top_k})
    out = []
    for r in rows:
        score = r.pop("score")
        out.append((r, 0.0 if score is None or math.isnan(score) else float(score)))
    return out


def get_request(request_id: str) -> dict | None:
    return _one(f"SELECT {', '.join(REQUEST_COLS)} FROM requests WHERE id = %s", (request_id,))


def update_request_status(request_id: str, status: str) -> None:
    _run("UPDATE requests SET status = %s WHERE id = %s", (status, request_id))


def get_match(request_id: str, item_id: str) -> dict | None:
    return _one("""SELECT id, request_id, item_id, score, rank, reason, status FROM matches
                   WHERE request_id = %s AND item_id = %s""", (request_id, item_id))


def update_match_status(match_id: str, status: str) -> None:
    _run("UPDATE matches SET status = %s WHERE id = %s", (status, match_id))


def reserve_item(item_id: str) -> bool:
    """available -> reserved atomically; False if someone else got it first."""
    return _one("""UPDATE items SET status = 'reserved'
                   WHERE id = %s AND status = 'available' RETURNING id""", (item_id,)) is not None


def insert_matches(rows: list[dict]) -> None:
    if not rows:
        return
    with _db().connection() as conn:
        with conn.transaction():
            for r in rows:
                n = conn.execute("SELECT nextval('match_seq') AS n").fetchone()["n"]
                conn.execute("""INSERT INTO matches (id, request_id, item_id, score, rank, reason, status)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                             (r.get("id") or f"mat_{n:04d}", r["request_id"], r["item_id"],
                              r["score"], r["rank"], r["reason"], r["status"]))


# ---- listings (marketplace) ------------------------------------------------
def _listing_columns() -> list[str]:
    """LISTING_COLS this database has. title/description came later; a DB that
    hasn't re-run init_db.py keeps working without them (they read back null)."""
    global _listing_cols
    if _listing_cols is None:
        have = {r["column_name"] for r in _all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'listings'")}
        _listing_cols = [c for c in LISTING_COLS if c in have]
    return _listing_cols


def insert_listing(row: dict) -> dict:
    _insert("listings", _listing_columns(), row)
    return row


def list_listings(status: str | None = None) -> list[dict]:
    cols = ", ".join(_listing_columns())
    if status is None:
        return _all(f"SELECT {cols} FROM listings ORDER BY listed_at, id")
    return _all(f"SELECT {cols} FROM listings WHERE status = %s ORDER BY listed_at, id", (status,))


def update_listing(listing_id: str, **fields) -> None:
    bad = set(fields) - set(_listing_columns())
    if bad:
        raise ValueError(f"Unknown listing fields: {bad}")
    sets = ", ".join(f"{k} = %s" for k in fields)
    _run(f"UPDATE listings SET {sets} WHERE id = %s", [*fields.values(), listing_id])


# ---- events (hypertable) ---------------------------------------------------
_EVENT_SELECT = """id, agent, event_type, item_id, request_id, category, message,
                   run_id, tool, duration_ms, data, time AS created_at"""


def insert_event(row: dict) -> dict:
    data = row.get("data")
    out = _one("""INSERT INTO events (time, agent, event_type, item_id, request_id, category,
                                      message, run_id, tool, duration_ms, data)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
               (row.get("created_at") or now(), row["agent"], row.get("event_type"),
                row.get("item_id"), row.get("request_id"), row.get("category"), row["message"],
                row.get("run_id"), row.get("tool"), row.get("duration_ms"),
                Jsonb(data) if data is not None else None))
    row["id"] = out["id"]
    return row


def impact_rows() -> list[dict]:
    """The continuous aggregate (real-time, so the newest events are included)."""
    return _all("SELECT bucket, event_type, category, n FROM impact_hourly")


def list_events(after: int = 0, limit: int = 50, run_id: str | None = None) -> list[dict]:
    if run_id is None:
        return _all(f"SELECT {_EVENT_SELECT} FROM events WHERE id > %s ORDER BY id LIMIT %s",
                    (after, limit))
    return _all(f"""SELECT {_EVENT_SELECT} FROM events WHERE run_id = %s AND id > %s
                    ORDER BY id LIMIT %s""", (run_id, after, limit))
