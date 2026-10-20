# 03 · Data model

Database: **Tiger Data** (managed Postgres + TimescaleDB). pgvector does matching; the `events` hypertable and the `impact_hourly` continuous aggregate power the impact dashboard. `backend/app/core/db.py` stays an in-memory stub until Tiger is wired; keep its function signatures.

`writer` = which component sets the field.

## users *(optional for MVP; hardcode `demo_donor` and `demo_requester`)*

| Field | Type | Writer | Notes |
|---|---|---|---|
| id | string PK | app | |
| name | string | app | |
| role | string | app | `donor` or `requester` |
| zip | string | app | |

## drafts *(a Tiger table, in memory without `DATABASE_URL`; nothing goes to `items` until the donor chooses; expire after 24 h; donating deletes the draft)*

| Field | Type | Writer | Notes |
|---|---|---|---|
| id | string PK | orchestrator | `drf_xxxx` |
| donor_id | string | app | |
| zip | string | app | |
| note | string \| null | app | donor's free text |
| image_url | string \| null | orchestrator | first uploaded photo |
| item | ParsedItem (JSON) | parser | |
| created_at | timestamp | orchestrator | |

## items

| Field | Type | Writer | Notes |
|---|---|---|---|
| id | string PK | orchestrator | `itm_xxxx` |
| donor_id | string | app | |
| image_url | string \| null | orchestrator | absolute URL: `PUBLIC_BASE_URL` + `/static/uploads/<id>.<ext>` (demo items: `SEED_IMAGE_BASE_URL/<name>.png` if set, else `/static/seed/<name>.png`); `null` if no photo |
| note | string \| null | app | donor's free text |
| zip | string | app | pickup zip |
| category | string | parser | see `05-agents.md` |
| item | string | parser | e.g. `puffer jacket` |
| brand | string \| null | parser | read from tags; `null` if not visible |
| color | string | parser | |
| size | string \| null | parser | never guessed |
| condition | string | parser | `new`, `like new`, `good`, `fair` |
| season | string \| null | parser | `winter`, `summer`, `all` |
| attributes | string[] | parser | 2–6, e.g. `["insulated","hooded"]` |
| summary | string | parser | one sentence |
| embedding | vector(`EMBED_DIM`) | orchestrator | from `summary + attributes + item`; dimension = the Modal embedding model's |
| status | string | orchestrator | `available` · `listed` · `reserved` · `donated` |
| created_at | timestamptz | orchestrator | |

## requests

| Field | Type | Writer | Notes |
|---|---|---|---|
| id | string PK | orchestrator | `req_xxxx` |
| requester_id | string | app | |
| raw_text | string | app | |
| zip | string | app | |
| needs | string[] | requester agent | core items |
| inferred_context | string[] | requester agent | |
| must_have | string[] | requester agent | |
| nice_to_have | string[] | requester agent | |
| size | string \| null | requester agent | |
| search_text | string | requester agent | what gets embedded |
| embedding | vector | orchestrator | |
| status | string | orchestrator | `open` (no matches) · `matched` (suggestions shown) · `closed` (one accepted) |
| created_at | timestamptz | orchestrator | |

## matches

| Field | Type | Writer | Notes |
|---|---|---|---|
| id | string PK | orchestrator | `mat_xxxx` |
| request_id | string FK | orchestrator | |
| item_id | string FK | orchestrator | |
| score | float | orchestrator | raw similarity, 0–1 |
| rank | int | orchestrator | 1 = best after re-ranking |
| reason | string | orchestrator | ≤ 12 words, shown to requester |
| status | string | orchestrator | `suggested` · `accepted` |

## events *(Tiger hypertable)*

| Field | Type | Writer | Notes |
|---|---|---|---|
| time | timestamptz | events.py | hypertable time column; API returns it as `created_at` |
| id | bigserial | events.py | feed polls `?after=<id>` |
| agent | string | events.py | `orchestrator` · `parser` · `pricing` · `requester` |
| event_type | string \| null | events.py | `item_parsed` · `item_donated` · `price_quoted` · `request_made` · `match_made` · `match_accepted`; `null` for progress-only steps |
| item_id | string \| null | events.py | |
| request_id | string \| null | events.py | |
| category | string \| null | events.py | item category, for impact stats |
| message | string | events.py | human-readable, shown in the feed |
| run_id | string \| null | events.py | one agent run (`run_…`); set on every event emitted during a concierge run, including the specialists it calls |
| tool | string \| null | events.py | tool the agent called (`get_price_quote`, `check_local_demand`, `list_for_sale`, `donate_item`) |
| duration_ms | int \| null | events.py | how long the tool (or, on the run's last event, the whole run) took |
| data | JSONB \| null | events.py | tool result, the decision (`brain`, `decision`, `price`, quote), or a tool error |

## impact_hourly *(continuous aggregate over `events`)*

`bucket` (1 hour), `event_type`, `category`, `n` (count). `/api/impact` reads from it.

## listings *(stretch, Nostr/Shopstr)*

| Field | Type | Writer | Notes |
|---|---|---|---|
| id | string PK | orchestrator | `lst_xxxx` |
| item_id | string FK | orchestrator | |
| platform | string | orchestrator | `nostr` (NIP-99 classified, shown on Shopstr) |
| external_id | string | orchestrator | Nostr event id (hex) |
| price | float | orchestrator | USD |
| listed_at | timestamptz | orchestrator | = the event's `published_at` |
| expires_at | timestamptz | orchestrator | `listed_at + 30 days` |
| url | string | orchestrator | `https://shopstr.store/listing/<naddr>` |
| nostr_url | string | orchestrator | `https://njump.me/<naddr>`: plain Nostr viewer, works even when Shopstr's site is down |
| status | string | orchestrator | `active` · `expired` |
| dry_run | bool | orchestrator | `true` = signed locally, never sent to relays |
| relays_accepted | string[] | orchestrator | relays that stored the event (`[]` in dry run) |
| title | string? | orchestrator | title as posted (the pricing agent's `listing_draft`, or the template) |
| description | string? | orchestrator | description as posted |

The Nostr event never contains `donor_id`, `note`, or other personal data: only catalog fields, price, and `location` (`LISTING_LOCATION`, else the item's zip).

## Tiger schema

The real schema is `backend/db/schema.sql`, applied by `backend/scripts/init_db.py` (adds sequences for ids, CHECK constraints on statuses, a refresh policy, and `timescaledb.materialized_only = false` so the dashboard sees fresh events). Summary:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE items (
  id TEXT PRIMARY KEY,
  donor_id TEXT NOT NULL,
  image_url TEXT, note TEXT,
  category TEXT, item TEXT, brand TEXT, color TEXT, size TEXT,
  condition TEXT, season TEXT,
  attributes TEXT[],
  summary TEXT,
  zip TEXT,
  status TEXT NOT NULL DEFAULT 'available',  -- available | listed | reserved | donated
  embedding VECTOR(768),                      -- = EMBED_DIM of the Modal embedding model
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON items USING hnsw (embedding vector_cosine_ops);

CREATE TABLE requests (
  id TEXT PRIMARY KEY,
  requester_id TEXT NOT NULL,
  raw_text TEXT NOT NULL, zip TEXT,
  needs TEXT[], inferred_context TEXT[], must_have TEXT[], nice_to_have TEXT[],
  size TEXT, search_text TEXT,
  embedding VECTOR(768),
  status TEXT NOT NULL DEFAULT 'open',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE matches (
  id TEXT PRIMARY KEY,
  request_id TEXT REFERENCES requests(id),
  item_id TEXT REFERENCES items(id),
  score REAL, rank INT, reason TEXT,
  status TEXT NOT NULL DEFAULT 'suggested'
);

CREATE TABLE events (
  time TIMESTAMPTZ NOT NULL DEFAULT now(),
  id BIGSERIAL,
  agent TEXT NOT NULL,
  event_type TEXT,
  item_id TEXT,
  request_id TEXT,
  category TEXT,
  message TEXT NOT NULL,
  PRIMARY KEY (time, id)
);
SELECT create_hypertable('events', 'time');

CREATE MATERIALIZED VIEW impact_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS bucket,
       event_type, category, count(*) AS n
FROM events
WHERE event_type IS NOT NULL
GROUP BY bucket, event_type, category;
```

Match query:

```sql
SELECT id, item, size, attributes, zip,
       1 - (embedding <=> $1) AS score
FROM items
WHERE status = 'available'
ORDER BY embedding <=> $1
LIMIT 5;
```

## Status lifecycle

```
drafts:    parsed ──sell──▶ (quote only; stays a draft)
              └──donate──▶ items.available
items:     available ──(stretch) list──▶ listed ──30d unsold──▶ available
               │                            │
               └──accept──▶ reserved ───────┴─▶ donated
requests:  open / matched ──accept──▶ closed
matches:   suggested ──accept──▶ accepted
listings:  active ──30d (simulate-expiry)──▶ expired
```
