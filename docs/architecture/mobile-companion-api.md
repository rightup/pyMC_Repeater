# Mobile Companion API

**Status:** Draft for review
**Scope:** openhop_repeater HTTP API for first-party companion mobile clients (iOS first)
**Audience:** repeater contributors and mobile client implementers

---

## 1. Summary

openhop_repeater already exposes a companion identity through the MeshCore
companion frame protocol (binary frames over TCP, `repeater/companion/`), and a
browser-oriented REST/SSE proxy on top of it (`repeater/web/companion_endpoints.py`).
Neither surface is suitable as the primary protocol for a mobile app:

- The frame protocol supports **one TCP client per companion at a time** and
  uses **destructive pop** message sync (`CMD_SYNC_NEXT_MESSAGE` →
  `companion_pop_message`), so a phone, the web UI, and a desktop client cannot
  coexist against the same companion identity.
- The existing SSE stream is fire-and-forget: in-memory per-client queues, no
  event IDs, no replay. A mobile client that loses connectivity (which is the
  normal case, not the exception) silently misses events.
- There is no way for a backgrounded or terminated app to catch up cheaply, and
  no push-notification path at all.
- Per-message RF context (every reception of a packet, heard repeats of our own
  transmissions, incoming path diversity) exists in the `packets` table but is
  not correlated with companion messages anywhere in the API.

This document specifies a **versioned, cursor-based sync API** for mobile
clients, built around an **event journal** as the single synchronization
mechanism. One journal feeds three access patterns — snapshot bootstrap, delta
poll, and live SSE — with one event schema. The design deliberately reuses
existing infrastructure: CherryPy HTTP server, JWT + API-token auth
(`repeater/web/auth/`), the SQLite store and migration pattern
(`repeater/data_acquisition/sqlite_handler.py`), and the existing
`CompanionBridge` callbacks.

The TCP frame protocol remains fully supported for standard MeshCore clients;
this API is additive.

---

## 2. Background

### 2.1 What exists today

| Surface | File(s) | Characteristics |
|---|---|---|
| Companion frame protocol (TCP) | `repeater/companion/frame_server.py`, `openhop_core.companion` | MeshCore-standard binary frames; port 5000 by default; one client at a time; destructive message pop; SQLite persistence of contacts/channels/messages/prefs |
| WebSocket proxy | `repeater/web/companion_ws_proxy.py` | Raw byte pipe from browser WS to the TCP frame server; JWT-authenticated; inherits all frame-protocol limitations |
| Companion REST + SSE | `repeater/web/companion_endpoints.py` | `/api/companion/*`; proxies bridge methods (contacts, channels, send_text, telemetry, …); SSE broadcast of bridge push callbacks with no IDs or replay |
| General repeater API | `repeater/web/api_endpoints.py`, `repeater/web/openapi.yaml` | ~100 endpoints: packets, adverts, stats, noise floor, policy, identities, room server, … |
| Auth | `repeater/web/auth/` | Login → JWT (short expiry) + `/auth/refresh`; long-lived API tokens hashed at rest in the `api_tokens` table; `require_auth` accepts either |

Relevant storage (all SQLite, WAL mode, single `repeater.db`):

- `packets` — every reception and transmission with `timestamp`, `type`,
  `route`, `rssi`, `snr`, `packet_hash`, `original_path`, `forwarded_path`,
  `upstream_hash`, `is_duplicate`, `transmitted`, `drop_reason`. Indexed by
  timestamp, type, and `packet_hash`. This is already an RF observation log.
- `companion_messages` — persisted companion inbox per `companion_hash`, and
  each row already carries `packet_hash`, `snr`, `rssi`, `path_len`.
- `companion_contacts`, `companion_channels`, `companion_prefs` — companion
  state, keyed by `companion_hash`.
- `room_messages` / `room_client_sync` — the room-server watermark pattern
  (see §5.3 for lessons learned from it).

### 2.2 Why the frame protocol can't be the mobile protocol

The MeshCore companion protocol was designed for a phone talking to a
firmware node over BLE/serial: a single trusted client that owns the node's
state. Mapped onto a repeater daemon it inherits assumptions that break the
mobile use case:

1. **Single consumer.** `CMD_SYNC_NEXT_MESSAGE` deletes the message from the
   queue on delivery. Two clients means messages are split arbitrarily between
   them.
2. **Connection-oriented.** All state transfer requires a live socket. iOS
   terminates sockets aggressively in background; every foreground event would
   require a full reconnect + contact re-sync.
3. **No resumability.** There is no "give me everything since X". A client that
   was offline for a day must trust that the queue held everything (bounded by
   `offline_queue_size`) and drain it message-by-message.
4. **No RF context.** The frame protocol carries one SNR/RSSI per message (the
   copy that was delivered), with no access to other receptions or repeats.

These are protocol-shape problems, not implementation bugs, which is why the
answer is a new surface rather than more patches to the proxy.

---

## 3. Goals and non-goals

### Goals

1. **Multi-client, non-destructive sync.** Any number of clients (mobile, web
   UI, scripts) can independently sync the same companion identity to
   completeness.
2. **Cheap catch-up.** A client that has been offline for minutes or days can
   fetch exactly what it missed with one bounded request, resuming from a
   client-held cursor.
3. **One event schema everywhere.** Delta poll and live SSE deliver identical
   event objects; a client implements one decoder.
4. **RF observability.** Expose per-message reception detail, incoming path
   aggregation, and heard repeats of the repeater's own transmissions —
   the data a repeater operator's companion app should surface that a stock
   MeshCore client cannot.
5. **iOS-friendly.** Endpoints shaped for `BGAppRefreshTask` budgets
   (small, bounded, conditional), plus an optional push-notification relay so
   the app learns about messages without polling.
6. **Fit the deployment reality.** Pi-class hardware, SD-card storage where
   unbounded table scans have already caused production incidents. Every
   endpoint must be index-served and bounded.
7. **Coexistence.** Standard MeshCore clients on the TCP frame protocol keep
   working, unchanged, concurrently with API clients.

### Non-goals

- **Not a new mesh protocol.** Nothing here touches RF or MeshCore framing.
- **Not a message store redesign.** `companion_messages` remains the message
  table; the journal references state, it doesn't duplicate it (§5.2).
- **Not federation.** One repeater, its companions, its clients. Multi-node
  aggregation is future work (§15).
- **Not an app spec.** UI, local persistence, and offline authoring UX belong
  to the client project. The API contract is the boundary.
- **v1 does not implement the push relay** — it defines the interface and
  registration endpoints so the relay can ship later without API changes (§12).

---

## 4. Architecture overview

```
                             ┌──────────────────────────────────────────────┐
                             │                repeater daemon               │
                             │                                              │
 LoRa RF ──► packet_router ──►  CompanionBridge (per companion identity)    │
                             │        │ callbacks (message, advert, ack…)   │
                             │        ▼                                     │
                             │  CompanionEventJournal  ──►  SQLite          │
                             │        │                     companion_events│
                             │        │ notify                companion_*   │
                             │        ▼                      packets        │
                             │  ┌───────────────┐                           │
 TCP frame clients ◄─────────┼──┤ frame_server  │  (unchanged, legacy path) │
                             │  └───────────────┘                           │
                             │  ┌───────────────┐                           │
 Mobile / web API clients ◄──┼──┤ CherryPy HTTP │  /api/v1/companions/…     │
                             │  └───────────────┘  snapshot / sync / SSE    │
                             │        │                                     │
                             │        ▼ (optional, outbound only)           │
                             │   Push relay client ──► APNs relay ──► APNs  │
                             └──────────────────────────────────────────────┘
```

New components, all inside the existing daemon:

- **`CompanionEventJournal`** (`repeater/companion/journal.py`, new): an
  append-only table of companion-scoped events with a monotonically increasing
  sequence number. Written from the same bridge callbacks that today feed the
  SSE broadcast. This is the *only* new write path.
- **Mobile API endpoints** (`repeater/web/mobile_endpoints.py`, new, mounted at
  `/api/v1/companions/`): snapshot, sync, SSE, receptions, send, devices.
- **Push relay client** (phase 4): outbound HTTPS notifier to a relay service.

Everything else is reuse.

---

## 5. The event journal

### 5.1 Journal as the canonical sync mechanism

The core design decision: **clients synchronize by consuming a journal, not by
querying state tables**. Every change a client could care about (message
arrived, contact updated, ack confirmed, telemetry answered) appends one row
with a sequence number. A client's entire sync state is one integer — the last
sequence it has applied. Snapshot, delta, and live streaming are three ways of
reading the same journal:

- **Snapshot** = current state tables + the journal head sequence.
- **Delta** = `SELECT … WHERE seq > ? ORDER BY seq LIMIT ?`.
- **SSE** = the same rows, pushed as they are appended, with `id:` = seq.

This collapses what would otherwise be three synchronization mechanisms with
three consistency models into one, and it makes client recovery trivial: any
failure mode degrades to "re-fetch from cursor", and a pruned cursor degrades
to "re-snapshot". There is no server-side per-client delivery state to corrupt.

### 5.2 Journal scope: references, not payload duplication (and what stays out)

Two constraints shape what goes *into* the journal:

1. **SD-card write budget.** The `packets` table already records every RF event
   (~50k rows/day on a busy deployment). Journaling raw RF traffic would
   roughly double the write load for data that is already queryable by
   timestamp and `packet_hash`. Therefore: **the journal records
   companion-scoped events only** — things that pass the companion's decrypt/
   filter layer — not general RF activity. Packet-level data is fetched on
   demand through reception endpoints (§10) keyed by `packet_hash`.
2. **Single source of truth.** Message text lives in `companion_messages`;
   contacts live in `companion_contacts`. Journal rows carry a compact JSON
   payload sufficient for the client to apply the event *without a follow-up
   request* in the common case (message events embed the message), but the
   journal is never consulted to answer "what is the current state" — state
   endpoints read state tables. Pruning the journal (§5.4) must never lose
   information that isn't reconstructable from state tables + snapshot.

One class of RF event crosses the companion boundary and *is* journaled by
default: **receptions correlated to a companion message** — another copy of an
inbound message arriving over a different path, or a repeat of the companion's
own transmission heard from a neighboring repeater. These are bounded by the
companion's own message volume (dozens of events per day, not tens of
thousands) and they power two time-sensitive UI features: live incoming-path
accumulation and the "your message was heard repeated" cue after a send
(repeats typically arrive within seconds of transmission). See §9 and §10.4.

### 5.3 Sequence and cursor semantics

- `seq` is `INTEGER PRIMARY KEY AUTOINCREMENT` — strictly monotonic, never
  reused, gaps allowed (`AUTOINCREMENT` guarantees no rowid reuse after
  deletes; plain `INTEGER PRIMARY KEY` does not, and cursor semantics require
  the guarantee).
- **Cursors are client-held and opaque-ish**: the server returns `next_cursor`
  as a string; clients store and echo it. v1 encodes the seq directly; the
  string type leaves room to encode journal-generation info later without
  breaking clients.
- **The server keeps no per-client read position.** This is a deliberate
  lesson from the room-server sync code: server-tracked watermarks
  (`room_client_sync`) have produced a series of subtle bugs — the author-post
  watermark advance and the non-monotonic replay watermark were both fixed on
  this branch (`fix(room_server): stop advancing the author sync watermark on
  post`, `fix(acl): keep the session replay watermark monotonic`). The frame
  protocol *forces* server-side delivery state; the HTTP API does not, so we
  don't take that bug class on. The `devices` registry (§11.3) exists for
  token management and push routing, not for sync correctness.
- **Journal epoch.** The snapshot and every sync response include a
  `journal_epoch` (random ID generated when the journal table is created or
  reset). If a database is wiped or restored from backup, epochs won't match
  and the client must discard its cursor and re-snapshot. This prevents the
  nastiest cursor failure: a *smaller-but-valid-looking* seq after a DB reset
  silently replaying or skipping history.

### 5.4 Schema

Added via the existing migration mechanism in `sqlite_handler.py`:

```sql
CREATE TABLE companion_events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    companion_hash  TEXT NOT NULL,          -- '0x'-prefixed, matches companion_* tables
    event_type      TEXT NOT NULL,          -- see §9
    created_at      REAL NOT NULL,          -- unix seconds
    ref_table       TEXT,                   -- optional: 'companion_messages', 'companion_contacts', …
    ref_id          INTEGER,                -- optional: rowid in ref_table
    packet_hash     TEXT,                   -- optional: RF correlation key into packets
    payload         TEXT NOT NULL           -- compact JSON, event-type-specific
);
CREATE INDEX idx_companion_events_sync
    ON companion_events (companion_hash, seq);

CREATE TABLE companion_devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    companion_hash  TEXT NOT NULL,
    device_id       TEXT NOT NULL UNIQUE,   -- client-generated UUID
    name            TEXT NOT NULL,          -- "Adam's iPhone"
    token_id        INTEGER NOT NULL,       -- FK → api_tokens.id
    platform        TEXT,                   -- 'ios' | 'android' | 'other'
    push_token      TEXT,                   -- APNs/FCM token, NULL until registered
    push_relay_url  TEXT,                   -- relay chosen by the client build
    created_at      REAL NOT NULL,
    last_seen       REAL,
    last_synced_seq INTEGER                 -- informational only (UI display), never used for delivery
);
```

```sql
CREATE TABLE companion_idempotency (
    device_id       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,      -- client-generated per logical send
    request_hash    TEXT NOT NULL,      -- detect key reuse with different body
    response_json   TEXT NOT NULL,      -- original response, replayed on retry
    created_at      REAL NOT NULL,
    PRIMARY KEY (device_id, idempotency_key)
);
```

One existing table gains columns (additive migration): `companion_messages`
gets derived reception counters — `observation_count`, `unique_path_count`,
`heard_repeat_count`, `unique_repeater_count`, `send_state` — updated as
correlated receptions are journaled (§10.4). They make the headline counts
("received 3× via 2 paths", "heard by 2 repeaters") part of the message
object in snapshots and history pages, and they survive after the underlying
`packets` rows age out (§10.6).

Journal metadata (`journal_epoch`, prune floor) lives in a small
`companion_journal_meta` key-value table.

**Retention:** prune `companion_events` on the existing cleanup schedule
(`storage.retention`, default 31 days, configurable as
`storage.retention.companion_events_days`). Pruning moves the "floor" seq;
sync requests with a cursor below the floor get `snapshot_required: true`
rather than silently incomplete results.

**Tombstones:** deletions (e.g. a removed contact) are ordinary journal events
(`contact` with `change: removed`), so they replay like any other change. No
separate tombstone retention rule is needed: a tombstone only matters to a
client whose cursor predates it, and any cursor older than the journal floor
is forced through a fresh snapshot, which reflects the deletion implicitly.
The invariant to preserve is simply that state-table deletion and its journal
event commit together (same transaction), like every other state change.

Rows in `companion_idempotency` are pruned after 48 h — long past any
plausible mobile retry horizon.

**Write pattern:** journal appends happen on the daemon's asyncio loop in the
same transaction scope as the state write they describe where possible (e.g.
message persist + journal append), via `asyncio.to_thread` like the existing
persistence hooks in `repeater/companion/frame_server.py`. Expected volume is
companion-scoped (messages, adverts from contacts, acks) — orders of magnitude
below the packets table — so the SD-card impact is negligible.

---

## 6. Consistency model

- **Per-companion total order.** Events for a given `companion_hash` are
  totally ordered by `seq`. Clients apply events in order; state converges.
- **At-least-once delivery.** A client may see an event twice (e.g. SSE
  delivered it, then a paranoid delta poll re-fetched it). Every event carries
  `seq`; clients ignore `seq <= last_applied`. Idempotency is the client's
  one obligation.
- **Snapshot consistency.** The snapshot is assembled in one read transaction:
  head seq is read first, then state tables. Events with `seq >` snapshot head
  may duplicate snapshot content (e.g. a message that is both in the snapshot's
  recent-messages list and in the first delta); message IDs make this
  deduplicable.
- **Outbound sends** are commands, not journal writes, from the client's view:
  `POST …/messages` returns an accepted message with its ID; delivery
  progress (heard repeats, `send_confirmed` ack, path used) arrives later as
  journal events referencing that ID. This gives mobile clients honest outbox
  UX (sending → sent → heard/confirmed/failed) without inventing a second
  channel.
- **Sends are idempotent.** Mobile requests get retried after timeouts, and a
  duplicated send doesn't just duplicate a UI row — it transmits a second RF
  packet and burns airtime. Every `POST …/messages` requires an
  `Idempotency-Key` header (client-generated UUID per logical send, scoped to
  the device). A retry with the same key returns the original response from
  `companion_idempotency` without touching the radio; the same key with a
  different body is a `409`.

---

## 7. REST API specification

All endpoints under `/api/v1/`, mounted alongside the existing `/api/` tree in
`repeater/web/http_server.py`, behind the same `require_auth` (§11). All
responses use the existing repo envelope: `{"success": true, "data": {…}}` or
`{"success": false, "error": "…"}` (the convention of
`repeater/web/companion_endpoints.py`; JSON examples below show the `data`
payload). All list endpoints require and clamp
`limit` (default 100, max 500). Multi-companion selection follows the existing
convention: companion name in the path, resolved via `identity_manager`
exactly as `companion_endpoints._get_bridge` does today.

### 7.1 Discovery and pairing

| Method & path | Purpose |
|---|---|
| `GET /api/v1/server_info` | Unauthenticated-safe minimum: server name, API version(s), auth modes, companion names list requires auth. Lets the app validate a scanned URL before pairing. |
| `POST /api/v1/pair` | Exchange a short-lived pairing code for a device API token (§11.2). |

### 7.2 Companion state

| Method & path | Purpose |
|---|---|
| `GET /api/v1/companions` | List companion identities (name, hash, node name, frame-protocol port). |
| `GET /api/v1/companions/{name}/snapshot` | Bootstrap document (§7.4). |
| `GET /api/v1/companions/{name}/sync?cursor=&limit=` | Delta events since cursor (§7.5). |
| `GET /api/v1/companions/{name}/events` | SSE live stream (§8). |
| `GET /api/v1/companions/{name}/contacts?limit=&offset=` | Paged contacts (state read). |
| `GET /api/v1/companions/{name}/channels` | Channel list (names + indexes; secrets only with `admin` scope). |
| `GET /api/v1/companions/{name}/messages?before_id=&limit=` | Paged message history, newest-first, keyed by message rowid — serves infinite scroll without touching the journal. |

### 7.3 Actions

Thin wrappers over the same `CompanionBridge` coroutines the existing
`/api/companion/*` endpoints call (send_text, send_channel_message, login,
request_status, request_telemetry, send_command, reset_path):

| Method & path | Purpose |
|---|---|
| `POST /api/v1/companions/{name}/messages` | Send DM or channel message. Requires `Idempotency-Key` header (§6). Body: `{to | channel_idx, text, txt_type?}`. Returns `{message_id, packet_hash}` immediately; heard-repeat and confirmation progress arrives as events. |
| `POST /api/v1/companions/{name}/contacts/{pubkey}/login` | Room/repeater login. |
| `POST /api/v1/companions/{name}/contacts/{pubkey}/status_request` | Remote status query. |
| `POST /api/v1/companions/{name}/contacts/{pubkey}/telemetry_request` | Remote telemetry query. |
| `POST /api/v1/companions/{name}/contacts/{pubkey}/reset_path` | Reset outbound path. |

Repeater-admin actions (policy, radio config, restarts) are **not** part of the
companion API; they stay on the existing `/api/` surface and require the
`admin` scope. The mobile app can gain them later by acquiring that scope; the
companion API stays messaging-shaped.

### 7.4 Snapshot

`GET /api/v1/companions/{name}/snapshot`

```jsonc
{
  "success": true,
  "journal_epoch": "b3f1…",
  "cursor": "184223",              // journal head at snapshot time
  "self": { "node_name": "…", "pubkey": "…", "adv_type": 1, "radio": {…} },
  "contacts": [ … ],               // full contact list (bounded by max_contacts, default 1000)
  "channels": [ … ],
  "messages": [ … ],               // most recent N (default 100) message objects, newest last
  "server": { "version": "…", "time": 1789… }
}
```

Supports `ETag`/`If-None-Match` (ETag = `journal_epoch:head_seq`) so a client
re-validating an unchanged snapshot pays ~zero.

### 7.5 Sync (delta)

`GET /api/v1/companions/{name}/sync?cursor=184223&limit=200`

```jsonc
{
  "success": true,
  "journal_epoch": "b3f1…",
  "events": [ { "seq": 184224, "type": "message", "ts": 1789…, "data": {…} }, … ],
  "next_cursor": "184301",
  "has_more": false,               // true → immediately request again with next_cursor
  "snapshot_required": false       // true → cursor below prune floor or epoch mismatch
}
```

One indexed range scan (`idx_companion_events_sync`), bounded by `limit`.
Returns `200` with empty `events` when up to date — cheap enough for
`BGAppRefreshTask` (§12.1). `ETag` = head seq supports `If-None-Match` polling
at effectively zero read cost.

---

## 8. SSE stream

`GET /api/v1/companions/{name}/events` — `text/event-stream`.

- Each event: `id:` = seq, `event:` = event type, `data:` = the same JSON
  object as `sync` returns. One schema, two transports.
- **Resume:** standard `Last-Event-ID` header (or `?cursor=`). On connect the
  server first drains the journal from that seq (reusing the sync query), then
  switches to live tail. This upgrades the existing broadcast plumbing in
  `companion_endpoints.py` (per-client `queue.Queue`, keepalive comments every
  `sse_keepalive_sec`) from fire-and-forget to resumable.
- Keepalive comments (`: ka`) every 15 s (existing config
  `http.sse_keepalive_sec`).
- Queue overflow (slow client) → close the stream; the client reconnects with
  `Last-Event-ID` and misses nothing. Today's code silently drops the client's
  queue; with a journal behind it, disconnection becomes safe instead of lossy.

**Client guidance:** SSE is for foregrounded apps and the web UI. On cellular,
a persistent stream costs battery; the intended mobile pattern is
push-notification wake (or `BGAppRefreshTask`) → one `sync` call. The API
supports both; the app chooses per lifecycle state.

---

## 9. Event schema

`type` discriminates; `data` is type-specific. v1 types, mapped from existing
`CompanionBridge` callbacks (the same list `companion_endpoints._ensure_callbacks`
registers today, plus message persistence):

| `type` | Emitted when | `data` (summary) |
|---|---|---|
| `message` | DM or channel message persisted for this companion | full message object: `id`, `packet_hash`, sender key/prefix, `txt_type`, text, `is_channel`, `channel_idx`, `path_len`, `snr`, `rssi`, timestamps |
| `message_reception` | another RF copy of a known inbound companion message is heard (different or repeated path) | `message_id`, `packet_hash`, `path` (raw hashes + resolution, §10.5), `rssi`, `snr`, `observed_at`, running `observation_count` / `unique_path_count` |
| `message_send_state` | outbound send progresses: transmitted, heard repeated by a neighbor, ack confirmed, or failed | `message_id`, `state: sent|heard_repeated|confirmed|failed`, and for `heard_repeated`: the repeat's `path`, terminal repeater hash (+resolution), `rssi`, `snr`, running `heard_repeat_count` / `unique_repeater_count` |
| `contact` | contact added/updated (advert received, path update, import) | full contact object + `change: new|advert|path|removed` |
| `channel` | channel added/renamed/removed | channel object |
| `login_result` | room/repeater login completes | contact pubkey, success, permissions |
| `status_response` | remote status arrives | contact pubkey + parsed status |
| `telemetry_response` | remote telemetry arrives (Cayenne LPP, decoded) | contact pubkey + decoded sensor values |
| `prefs` | node prefs changed (any surface: API, frame client, web UI) | changed fields |
| `rf_reception` | *(flagged, default off)* any packet heard again, regardless of companion relevance | `packet_hash`, `rssi`, `snr`, `path` |

Unknown-type tolerance is mandatory: clients must skip event types they don't
recognize (log + advance cursor), so the server can add types without a
version bump.

**Correlated vs. uncorrelated receptions.** `message_reception` and the
`heard_repeated` send state are **on by default**: they are bounded by the
companion's own message volume and they carry the two live cues clients need —
incoming paths accumulating on a message, and the near-immediate "your
transmission was heard repeated" signal after a send. The uncorrelated
firehose (`rf_reception`, every duplicate of every flood the repeater hears)
remains **opt-in** (`?include=rf_receptions` on sync/SSE) for "signal view"
style screens; at ~50k packets/day it would otherwise dominate both the
journal and every delta. The pull endpoints in §10 remain the exhaustive RF
surface either way.

---

## 10. RF observation surface

This is the part a repeater-attached companion can do that no stock client
can: the repeater sees every reception, including duplicates that arrive over
different paths. All of it is already in the `packets` table
(`is_duplicate`, `original_path`, `forwarded_path`, `rssi`, `snr`,
`packet_hash`, indexed by `packet_hash` and timestamp) — this surface is
read-only queries, no new write path.

| Method & path | Purpose |
|---|---|
| `GET /api/v1/companions/{name}/messages/{id}/receptions` | Every reception of that message's `packet_hash`: per-copy RSSI/SNR, incoming path, arrival time, whether we retransmitted it. Answers "how did this message reach me, and how well". |
| `GET /api/v1/companions/{name}/contacts/{pubkey}/paths?window=24h` | Incoming path aggregation for a contact: distinct first-hop/last-hop paths observed on their traffic in the window, with counts and RSSI/SNR stats. Serves a "route diversity" view. |
| `GET /api/v1/companions/{name}/transmissions/{packet_hash}/repeats` | Heard repeats of our own transmission: receptions of the same `packet_hash` after we transmitted it, i.e. neighbors repeating us. The strongest available signal that a send actually propagated. |

### 10.1 Query-shape rules

- All three resolve through `idx_packets_hash` or the timestamp index with a
  **mandatory bounded window** (default 24 h, max 7 d) — the July 2026
  airtime-chart incident (unbounded scan of a 760 MB packets table saturating
  SD I/O) is the anti-pattern these limits exist to prevent.
- Path aggregation for a contact requires resolving that contact's recent
  `packet_hash`es first (via `companion_messages.packet_hash` and adverts),
  then a hash-keyed lookup per packet — never a scan of `packets` filtered by
  a non-indexed column.

### 10.2 How the data gets there

No new write path is needed: the engine's duplicate handler
(`repeater/engine.py`, `record_duplicate`) already writes a full
`packets` row for **every** repeated copy — per-copy `original_path`, `rssi`,
`snr`, timestamp, `is_duplicate=1` — keyed by a `packet_hash` that excludes
the mutable path bytes, so all OTA copies of one logical packet share the
hash. Transmissions are recorded with `transmitted=1`. Receptions and repeats
are therefore reconstructable entirely from existing rows.

One nuance to carry into the API contract: the stored `packet_hash` is the
full hash truncated to 16 hex chars (64 bits). Within a correlation window
(minutes to hours) collisions are negligible, but the API documents the
identifier as an opaque correlation key, not a cryptographic commitment, and
correlation queries always pair the hash with a bounded time window.

### 10.3 Heard-repeat semantics

A heard repeat is a derived relationship, not a separate radio phenomenon:
a reception row with the same `packet_hash` as one of our `transmitted=1`
rows, arriving after it. The retransmitting repeater is identified by the
**terminal path hash** of the repeat's `original_path` — the hash each
repeater appends as it forwards. Two counts are exposed and neither is
collapsed: `heard_repeat_count` (every matching OTA copy — one repeater heard
twice counts twice) and `unique_repeater_count` (distinct terminal hashes).

**Local echo exclusion:** a locally injected outbound frame, or the
transmission record itself, must never count as a heard repeat — only a
genuine OTA reception arriving *after* our transmit row qualifies. The
correlation predicate is `is_duplicate=1 AND transmitted=0 AND timestamp >
tx.timestamp`, and any future radio backend that surfaces self-receptions
must mark them so they are excluded here.

Hash-changing rebroadcast variants (packet types whose hash semantics
incorporate path data) are out of scope for v1; the endpoints document
repeat counts as a lower bound.

### 10.4 Live correlation (default-on journal events)

The pull endpoints above serve history; the live cues come from the journal
(§9). On each duplicate reception the engine consults a small in-memory TTL
map of recent companion-message hashes — inbound message `packet_hash`es and
our own recent outbound sends, with a lifetime matching the `seen_packets`
dedup cache — and on a hit appends a `message_reception` or
`message_send_state(heard_repeated)` event. Cost per duplicate is one dict
lookup; volume is bounded by the companion's message rate, not the mesh's
packet rate. This is what makes "sent → heard by 2 repeaters" appear in the
app seconds after transmission (SSE) and survive into the next background
sync (journal).

### 10.5 Path-hash identity resolution

Path entries are abbreviated hashes (commonly one byte) and genuinely
collide. Wherever the API renders a path element it returns both the raw
value and its interpretation:

```jsonc
{ "raw_hash": "71", "resolution": "unique|ambiguous|unknown",
  "candidates": [ { "pubkey": "…", "name": "Everett North" }, … ] }
```

Clients display the raw hash unless resolution is `unique`. Resolution is
computed at read time against current contacts/adverts — it can improve as
contacts are learned, which is another reason observations store raw bytes
and never a resolved identity.

### 10.6 Pruning honesty

`packets` retention (default 31 d) is shorter than the lifetime of messages
in `companion_messages`. Reception queries whose window reaches past the
packets retention floor set `"observations_pruned": true` in the response so
clients can distinguish "heard once" from "older copies aged out". The
running counters journaled on events (§9) are preserved in the message
row's derived fields at prune time, so headline counts survive raw-row
pruning.

---

## 11. Authentication and security

### 11.1 Model

Reuse the existing two-tier scheme, extended with scopes:

- **Web UI:** username/password → JWT (short-lived, `/auth/refresh`) —
  unchanged.
- **Mobile devices:** long-lived **device API token**, stored in the existing
  `api_tokens` table (hashed at rest, `Authorization: Bearer`), linked 1:1 to
  a `companion_devices` row. Tokens get a `scope` column (migration):
  - `companion:{name}` — full companion API for one companion identity
  - `companion:*` — all companions
  - `admin` — existing behavior; implied for all pre-migration tokens
    (backward compatible)
- `require_auth` grows scope enforcement; the `/api/v1/companions/{name}/…`
  router checks the token's scope against the path.

### 11.2 Pairing flow

Typing a URL + token on a phone is the adoption killer; pairing is QR-based:

1. Operator (authenticated in the web UI) opens *Settings → Mobile devices →
   Pair*, which calls `POST /api/v1/pair/start` (admin scope) → server
   generates a **pairing code**: single-use, 5-minute TTL, displayed as QR
   encoding `{url, fingerprint, code}`.
2. App scans, validates reachability via `GET /api/v1/server_info`, then calls
   `POST /api/v1/pair` with `{code, device_id, name, platform}`.
3. Server atomically consumes the code, creates the device row + scoped token,
   returns the token **once**. App stores it in the iOS Keychain.
4. Web UI lists devices (name, platform, created, last seen) with revoke —
   revocation deletes the token; the next request 401s and the app returns to
   pairing.

### 11.3 Transport security

Honest position: most deployments are plain HTTP on a LAN or a WireGuard/
Tailscale overlay. The design accommodates rather than pretends:

- `server_info` reports the transport situation; the app warns when pairing a
  bearer token over plain HTTP on a non-private address.
- Documented recommended remote-access path: Tailscale/WireGuard to the LAN
  (already common in this community), keeping the repeater unexposed. Direct
  internet exposure requires TLS (reverse proxy) and is documented as
  such.
- Pairing QR includes a server key fingerprint so the app can pin the identity
  it paired with (TOFU) and detect later substitution even without TLS.
- Rate limiting on `/api/v1/pair` and auth endpoints (small fixed-window
  counter, in-memory) to blunt code guessing; pairing codes are 128-bit.

### 11.4 Privacy note

Channel secrets and identity private keys never leave the server through this
API except: channel secrets with `admin` scope (existing behavior for the web
UI). The mobile client does not need channel secrets — decryption happens
server-side in the bridge, which is the companion model's trust boundary
already (the repeater holds the companion identity key).

---

## 12. iOS client integration

### 12.1 Background refresh

Designed-for pattern with `BGAppRefreshTask` (~30 s budget, opportunistic
scheduling):

1. Wake → `GET …/sync?cursor=<stored>&limit=200` with `If-None-Match`.
2. `304` (common case) → done in one round trip, minimal radio time.
3. Events → apply, store `next_cursor`, optionally post local notifications
   for `message` events, loop while `has_more` (bounded iterations).
4. `snapshot_required` → schedule a `BGProcessingTask` for the full snapshot
   rather than burning the refresh budget.

This is why sync is a single bounded indexed read with ETag support: the
endpoint's worst case must fit a background-task budget on a Pi serving it
from an SD card.

### 12.2 Push notifications (APNs relay)

The constraint: APNs requires the developer's key; a self-hosted repeater
can't sign APNs requests for the app's bundle ID. Standard solution — a thin
**push relay** operated by the app maintainer (the pattern Home Assistant
uses):

```
bridge callback → journal append → push notifier (debounced)
    → POST {relay_url}/notify {push_token, badge_hint, collapse_id}
    → relay signs & forwards to APNs (content-available + optional alert)
    → app wakes → sync from stored cursor
```

Design properties:

- **Payload-free by default.** The push is a wake signal ("new events");
  message content stays on the repeater unless the operator opts into
  alert-bearing pushes (`push_detail: none | count | preview`, per device).
  This keeps the relay low-trust: it learns *that* a device got traffic, not
  *what*.
- The repeater side is only: `push_token`/`push_relay_url` registration
  (`POST /api/v1/devices/{device_id}/push`, already in the schema §5.4), a
  debounced outbound POST on journal append (collapse per device, min
  interval ~30 s), and failure backoff with token invalidation on relay 410.
- Relay URL is client-supplied at registration (the app build knows its
  relay), so third-party app builds or self-hosted relays need no repeater
  changes.
- **v1 ships the registration endpoints and the notifier interface; the relay
  service itself is a separate deliverable** (phase 4). Until then the app
  runs on background refresh alone — functional, just slower.

---

## 13. Performance and capacity

Binding constraints (from a production Pi 4 + SD-card deployment): ~25 MB/s
random-read I/O ceiling, packets table ~1.5M rows/760 MB at
31-day retention, SQLite WAL with a single writer, and a demonstrated failure
mode where one unbounded scan times out the dashboard and backs up the TX
queue.

Budget rules for every endpoint in this document:

1. **Index-only access paths.** Sync uses `idx_companion_events_sync`;
   receptions use `idx_packets_hash`; message history uses the
   `companion_messages` rowid. No endpoint may filter `packets` on a
   non-indexed column.
2. **Mandatory limits.** Every list is clamped (≤500 events, ≤200 messages,
   ≤7 d RF windows). `has_more` pagination instead of large responses.
3. **Write amplification.** Journal appends are companion-scoped (hundreds/
   day, not tens of thousands) and ride the existing WAL. Correlated
   reception events (§10.4) preserve this bound — they scale with the
   companion's message volume, and the mesh-wide `rf_reception` firehose
   stays opt-in for exactly this reason.
4. **SSE fan-out** stays in-memory per client (existing pattern); the journal
   is the durability layer, so overflow handling is "drop connection", never
   "buffer unboundedly".
5. **Expected mobile load** (one operator household, a handful of devices,
   background sync every ~15 min + push wakes) is noise compared to the web
   dashboard; the risk is not QPS but a single bad query shape, hence rules
   1–2.

---

## 14. Migration and compatibility

- **TCP frame protocol: preserved via soft-consume.** Standard MeshCore
  clients keep working, but this required one storage-semantics change:
  `companion_pop_message` historically **deleted** the row (the frame
  protocol destructively drained history), which would have erased message
  history out from under the API. Pop now *soft-consumes* — it sets a
  `consumed_at` timestamp instead of deleting. Frame-protocol reads filter
  `consumed_at IS NULL`, and offline-queue capacity/eviction counts only
  unconsumed rows, so MeshCore clients observe identical queue behavior;
  API history reads see all rows. Consumed rows age out on the normal
  retention schedule. Journal appends hook the same persistence path
  (`_persist_companion_message`), so both consumers see every message.
- **Existing `/api/companion/*`: kept, frozen.** The web UI migrates to
  `/api/v1/companions/*` opportunistically (the SSE upgrade in §8 is the main
  win — reconnect without missing events). Once migrated, the old endpoints
  and the WS proxy can be deprecated on their own schedule.
- **Database:** all changes are additive migrations (journal + devices tables,
  `scope` column on `api_tokens` defaulting existing tokens to `admin`).
  Rollback = ignore new tables.
- **Versioning policy:** `/api/v1` is the contract. Additive changes (new
  event types, new optional fields) don't bump the version; clients must
  tolerate unknown fields and event types (§9). Breaking changes require
  `/api/v2` served alongside v1.

---

## 15. Implementation plan

Phases are independently shippable; each ends in a working state.

**Phase 1 — Journal + sync core**
- `companion_events` + meta tables, migrations, retention pruning.
- `CompanionEventJournal` writer hooked into bridge callbacks and message
  persistence.
- `GET snapshot`, `GET sync`, `GET messages` endpoints; epoch/cursor
  semantics; ETag support.
- Tests: ordering, prune-floor → `snapshot_required`, epoch mismatch, restart
  continuity, concurrent frame-client + API-client message delivery.

**Phase 2 — Live + actions + auth**
- SSE endpoint with `Last-Event-ID` replay (upgrade existing SSE plumbing).
- Action endpoints (send, login, status/telemetry requests) + send-state
  events, with `Idempotency-Key` enforcement and the
  `companion_idempotency` table.
- Token scopes, pairing flow, device registry, web UI device management page.
- Tests: SSE resume equivalence with sync, scope enforcement, pairing
  single-use/TTL, send retry with same/different body under one key.

**Phase 3 — RF observation surface**
- Receptions, contact paths, heard-repeats endpoints with window clamps,
  path-hash resolution (§10.5), and `observations_pruned` signaling (§10.6).
- Live correlation hook in `record_duplicate` (§10.4): default-on
  `message_reception` / `heard_repeated` events + derived counters on
  `companion_messages`; local echo exclusion tests (§10.3).
- Opt-in `rf_reception` event type behind `?include=`.
- Load-test query shapes against a production-scale packets table (the
  31-day Pi 4 dataset from §13 is the reference workload).

**Phase 4 — Push**
- Push registration endpoints + debounced notifier with backoff.
- Relay service (separate repo/deliverable) + documentation.

Each phase updates `repeater/web/openapi.yaml` — the spec ships with the
endpoints, not after them.

---

## 16. Future work

- **Android / FCM** through the same relay interface (`platform` field already
  present).
- **Room-server browsing** from mobile: the room tables and sync machinery
  exist; a read-only room surface could reuse the journal pattern
  per-room.
- **Multi-repeater aggregation**: a client paired with several repeaters
  merging journals client-side; `journal_epoch` + per-server cursors already
  make this safe. Cross-repeater dedup by `packet_hash` is the interesting
  problem.
- **Web dashboard on the journal**: converge the dashboard's various pollers
  onto sync/SSE.
- **Offline outbox with scheduled TX**: client queues messages while
  unreachable and drains them on reconnect — the v1 `Idempotency-Key`
  contract already makes the drain safe against retries.

## 17. Open questions

1. **Snapshot size vs `max_contacts`=1000.** Is a full contact list in one
   snapshot response acceptable on Pi + cellular (~150–300 KB), or should
   snapshot page contacts from the start?
2. **`message_send_state` fidelity.** How reliably can `send_confirmed` be
   correlated to a specific outbound message in `openhop_core` today? If ack
   correlation is weak, v1 may only expose `sent` (transmitted) without
   `confirmed`.
3. **Pairing without the web UI** (headless installs): is a CLI generator
   (`manage.sh pair`) enough for v1?
4. **Relay hosting/funding** for phase 4 — maintainer-operated, and under what
   availability expectations?
