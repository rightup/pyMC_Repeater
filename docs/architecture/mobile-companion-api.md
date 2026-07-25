# Mobile Companion API v1

**Status:** Implemented
**Base path:** `/api/v1`
**Primary contract:** `repeater/web/openapi.yaml`
**Reference client:** `companion_client/rest.py`

This is the authenticated, durable chat API for clients that run alongside a
standard MeshCore frame client. It is additive: the TCP frame protocol remains
available, and both surfaces share the same companion identity, bridge, packet
injector, and radio.

## 1. Invariants

These rules are the shortest useful description of the design:

1. The daemon is the only radio owner. REST and frame clients never open
   competing radio paths.
2. Every chat message is durable history before it is exposed as a journal
   event.
3. Frame delivery is a bounded pending queue; consuming a frame message clears
   its pending flag but does not delete REST history.
4. Every sync cursor is `epoch:seq`. A cursor that cannot prove continuity
   requires a snapshot instead of risking a silent gap.
5. An HTTP send reserves its idempotency key before RF. A terminal result is
   replayed; an ambiguous result stays visibly `indeterminate`.
6. Companion device tokens can enter `/api/v1` only. They cannot use the
   operator API, auth-management endpoints, legacy companion API, or
   WebSockets.
7. A paired device chooses its push token and privacy preference. The operator
   chooses the one relay URL.
8. Channel secrets are write-only. Identity private keys are never exposed by
   v1.
9. Every persisted one-byte companion namespace is immutably bound to its
   full public identity before restore or activation. Deleting configuration
   does not delete that binding or history; a different key with the same
   first byte is always refused.

## 2. Two clients, one companion

```text
TCP frame client ─┐
                  ├─> CompanionBridge ─> shared injector/queue ─> radio
REST/SSE clients ─┘          │
                             └─> SQLite state + ordered event journal
```

The bridge is the compatibility boundary. Frame commands and REST actions call
the same bridge methods. Transport-neutral bridge observers report accepted
outbound messages and ACKs so history and lifecycle events do not depend on
which client sent them.

Source ownership prevents double persistence:

- REST creates its outbound row as `pending` before calling the bridge. The
  bridge observer updates that row.
- A frame send has no pre-created REST row. The observer creates it exactly
  once with `source: frame`.
- An unversioned operator API send is recorded the same way with
  `source: operator`, so a parallel chat client never mistakes it for its
  connected frame peer.
- Inbound radio messages are stored and journaled atomically before the frame
  pending queue is considered.

The TCP listener allows one connection per companion, matching the upstream
frame protocol. Its default port is `5000` and its safe default bind is
`127.0.0.1`. It is unauthenticated and can expose private identity/channel
material; bind it to a LAN only as an explicit trusted-network choice. Its
idle timeout defaults to 28,800 seconds (8 hours); `tcp_timeout: 0` disables
that timeout. Every enabled listener in this process uses a different port,
including the HTTP listener. Give additional companions `5001`, `5002`, and
so on; the deliberately simple process-wide rule avoids ambiguous wildcard
binds and platform-dependent socket reuse.

Set `settings.frame_enabled: false` when a companion is REST/SSE-only. This
does not disable its bridge, identity, durable history, or shared radio path;
it only avoids opening and reserving an unauthenticated frame port. The
default remains `true` for upstream compatibility. If an enabled frame
listener cannot bind, activation fails visibly instead of silently dropping
that companion's REST surface.

The `/ws/companion_frame` compatibility proxy is itself a frame client. Because
the upstream frame server permits only one client, opening that proxy evicts
the directly connected frame client (and vice versa). It is an operator
compatibility tool, not a parallel chat transport. A chat client that must run
beside a frame client uses `/api/v1` REST, sync, and SSE. Its URL `?token=`
escape hatch accepts an operator JWT only; an admin API token must use the
`X-API-Key` header and a device-scoped token cannot enter the proxy. Supply
exactly one credential and one `companion_name`; duplicate or unknown query
fields fail the handshake. The proxy closes at JWT expiry or within 15 seconds
of API-token revocation, scope change, or an unavailable authorization store.

Each companion has two deliberately different names:

- `name` is the stable registration slug used in configuration, URL paths,
  pairing, and authorization scopes. It is 1–64 ASCII characters, starts with
  a letter or digit, and otherwise permits only letters, digits, `.`, `_`, and
  `-` (`my-companion` is typical).
- `settings.node_name` is the human-facing advertised/display name. It may use
  human-readable text subject to MeshCore's 31-byte UTF-8 limit. Rename this
  field when only presentation should change.

### Adopting storage from an older build

An empty namespace binds automatically. If any older, unbound companion rows
already exist for the same one-byte hash—contacts, channels, messages,
preferences, events, devices, or journal floors—activation stops instead of
guessing ownership.

To adopt those rows:

1. Back up the database and verify that the rows belong to the configured
   companion's full public identity.
2. Set `settings.adopt_legacy_namespace: true`.
3. Start or activate the companion once, then remove the setting (or return it
   to `false`).

This switch can create a missing binding only. It cannot replace an existing
binding, even when left enabled. A genuine full-key collision therefore
requires restoring the original identity or choosing a key with a different
first byte; there is no in-place reassignment. Legacy paired devices whose
token rows lack an unambiguous full identity must be revoked and paired again.

REST has no single-client delivery state. Several phones, browsers, or agents
can independently consume the same journal without stealing messages from one
another or from the frame client.

The unversioned `/api/companion/*` routes remain operator-only Repeater API
compatibility endpoints. They intentionally keep their existing request and
response shapes, so their send routes do not gain v1 idempotency semantics.
New human or agent chat clients should use `/api/v1`; retries there are safe
when they reuse the same `Idempotency-Key`.

## 3. v1 resources

| Method and path | Purpose |
|---|---|
| `GET /server_info` | Public discovery and transport warning |
| `POST /pair/start` | Admin creates a five-minute, single-use pairing code |
| `POST /pair` | Device exchanges the code for one scoped token |
| `GET /companions` | Scope-filtered companion list |
| `GET /companions/{name}/snapshot` | Bootstrap current state and cursor |
| `GET /companions/{name}/sync` | Bounded ordered journal page |
| `GET /companions/{name}/events` | Resumable SSE over the same journal |
| `GET /companions/{name}/messages` | Newest-first durable history |
| `POST /companions/{name}/messages` | Idempotent DM or channel send |
| `POST /companions/{name}/anonymous_request` | Query public v13 node metadata by full key |
| `POST\|DELETE /companions/{name}/contacts/{pubkey}` | Contact mutation |
| `PUT\|DELETE /companions/{name}/channels/{index}` | Channel mutation |
| `POST .../login` | Synchronous remote login |
| `GET .../connection` | Current non-expired remote login-session state |
| `POST .../logout` | Close the local remote session and best-effort RF logout |
| `POST .../status_request` | Synchronous remote status request |
| `POST .../telemetry_request` | Synchronous remote telemetry request |
| `POST .../ping` | Direct TRACE from the selected companion to a repeater |
| `POST .../path_discovery` | Active outbound and return route discovery |
| `POST .../reset_path` | Local learned-path reset |
| `GET .../messages/{id}/receptions` | RF copies correlated to one message |
| `GET .../contacts/{pubkey}/paths` | Bounded incoming-path aggregation |
| `GET .../transmissions/{hash}/repeats` | Heard repeats of a companion-owned send |
| `GET /devices` | Admin-only redacted paired-device list |
| `DELETE /devices/{device_id}` | Device revokes itself; admin may revoke any device |
| `POST\|DELETE /devices/{device_id}/push` | Own-device push registration |

JSON success responses use:

```json
{"success": true, "data": {}}
```

JSON errors use:

```json
{"success": false, "error": "human-readable message"}
```

Some error paths also include `status` and structured `data`. Clients should
branch on HTTP status and stable structured fields, not parse error prose.

Every non-empty v1 request body uses `Content-Type: application/json`; other
media types return `415`. Actions with no fields may omit the body or send an
empty JSON object (`{}`).

Authenticated state/action responses use `Cache-Control: no-store`.
`snapshot` uses `private, no-store, no-cache, no-transform` plus an `ETag`, so
an explicit client may revalidate without storing authenticated chat state.
Its `304` carries the same cache policy.
Credentialed SSE uses
`no-store, no-cache, no-transform`.

## 4. Snapshot and cursor sync

### 4.1 Cursor form

A cursor is an opaque string with a readable representation:

```text
<journal_epoch>:<sequence>
```

Clients must store and echo the full string. A bare historical sequence is
recognized only so the server can return an explicit reset instruction; it
does not establish continuity.

The epoch changes when companion journal state is deliberately purged. A
small owner-only lineage sidecar is compared with the lineage stored in
SQLite at startup; replacing or restoring `repeater.db` rotates the epoch
before the API starts. Restoring the entire storage directory, including that
sidecar, must likewise rotate or remove the sidecar before startup. The
sequence is monotonic within an epoch. Gaps are allowed.

### 4.2 Snapshot

`GET .../snapshot` returns:

- the full companion public identity and public preferences;
- contacts;
- configured channel indices and names, never secrets;
- recent inbound and outbound messages, oldest first;
- `journal_epoch` and the matching `cursor`;
- server version. Current server time is available from `/server_info` and
  the standard HTTP `Date` header, so the cacheable snapshot contains no
  moving clock field.

The reset baseline under `self` contains `public_key` plus every public
preference later carried by `prefs` events: `node_name`, `adv_type`,
`latitude`, `longitude`, `autoadd_config`, `autoadd_max_hops`,
`path_hash_mode`, `rx_delay_base`, `airtime_factor`, `client_repeat`,
`manual_add_contacts`, all three `telemetry_mode_*` fields,
`advert_loc_policy`, `multi_acks`, and `default_scope_name`. Radio tuning and
secret scope material remain excluded.

The server reads journal head before state. A mutation racing the snapshot may
therefore appear in both the state and a later event. This is safe
at-least-once behavior: clients upsert by stable message/contact/channel
identity. The reverse ordering could lose an event and is not used.

Snapshot is the only conditional endpoint. Its `ETag` includes companion,
epoch, journal head, `messages_limit`, and server version; `If-None-Match` is
valid only for the same request shape.

### 4.3 Delta

`GET .../sync?cursor=<epoch:seq>&limit=100` validates the cursor and reads the
page in one SQLite transaction. A valid response contains:

```json
{
  "journal_epoch": "4b88bb302f988c17",
  "events": [],
  "next_cursor": "4b88bb302f988c17:42",
  "has_more": false,
  "snapshot_required": false
}
```

If continuity cannot be proven, the response is HTTP 200 with no events,
`snapshot_required: true`, a fresh snapshot cursor, and one stable
`reset_reason`:

- `missing_epoch`
- `epoch_mismatch`
- `future_cursor`
- `pruned_cursor`

A syntactically invalid or negative sequence is HTTP 400.

`sync` deliberately has no ETag. The cursor is already the condition, and
`has_more` is computed from the same bounded read. Continue until
`has_more` is false. A client may impose an explicit safety cap, but reaching
it must be an error—not a partial result presented as caught up. The reference
client's `follow()` follows that rule.

### 4.4 Optional RF events

`rf_reception` is a high-volume diagnostic event and is off by default at two
levels:

1. the companion must set `settings.rf_reception_events: true`;
2. the request must pass `include=rf_receptions`.

The `include` selector is strict so a typo cannot silently hide data: blank
or unknown comma-separated tokens return `400`. Repeating
`rf_receptions` is harmless.

Filtering happens after the bounded journal read. Filtered rows still advance
the cursor, preserving one unambiguous scan position.

## 5. SSE

`GET .../events` is a live view of the same rows and JSON event objects as
`sync`.

```text
id: 4b88bb302f988c17:43
event: message
data: {"seq":43,"type":"message","ts":...,"packet_hash":null,"data":{...}}
```

Resume priority is:

1. `Last-Event-ID`
2. `?cursor=`
3. current head (live tail, no backlog)

Invalid/reset cursors receive one `snapshot_required` control event, then the
stream closes. Idle streams emit `: ka` at
`http.sse_keepalive_sec` (15 seconds by default).

Journal listeners carry only a coalescing wake signal. The stream reads every
page back from SQLite in sequence order, so concurrent writers and event
bursts cannot reorder or overflow the event data.

There is one stream per principal and companion. The v1 stream and deprecated
legacy operator stream share one process-wide cap
(`mobile_api.sse_max_connections`, default 8); opening one surface consumes
capacity from the other. The configured value is limited to 256 and must also
be no greater than the effective `http.thread_pool_max - 2`, reserving two
workers for ordinary API requests.
Capacity rejection is HTTP 429 with `Retry-After`; a slow legacy consumer is
disconnected if its bounded callback queue fills rather than being left on a
silently incomplete stream.

`http.sse_queue_maxsize` defaults to 64, is clamped to at least 32, and may
not exceed 4096. `http.sse_keepalive_sec` defaults to 15, is clamped to at
least 5, and may not exceed 60 seconds. Both must be positive JSON/YAML
integers (booleans and numeric strings are not accepted); oversized settings
fail at startup instead of allocating or waiting unexpectedly.

Use `Authorization: Bearer` from native clients or streaming `fetch`.
`?token=` exists only for a short-lived operator JWT used by browser
`EventSource`. Device tokens are deliberately rejected in query strings.
Query URLs may be recorded by reverse proxies and browser or developer tooling.
Keep those JWTs short-lived, scrub query strings from upstream logs, and use
the header form whenever the client permits it. The embedded server disables
its access log, but that does not protect logs maintained by an upstream proxy.
An open stream is not an authorization snapshot: it closes at JWT expiry or
within 15 seconds of API-token revocation, scope change, device unpairing, or
loss of the immutable companion binding. Authorization-storage uncertainty
also closes the stream. Reconnect with a current credential and the last
durably applied cursor; the stream itself never refreshes credentials.

## 6. Events and durable messages

Every event has:

```json
{
  "seq": 43,
  "type": "message",
  "ts": 1789012345.0,
  "packet_hash": "AABBCCDDEEFF0011",
  "data": {}
}
```

Known event types:

| Type | Meaning |
|---|---|
| `message` | Durable inbound or outbound message row |
| `message_send_state` | Outbound lifecycle or heard-repeat transition |
| `message_reception` | Another RF copy of a known inbound message |
| `contact` | `new`, `update`, `remove`, or learned-path change |
| `channel` | Channel `update` or `remove`; never includes the secret |
| `prefs` | Preferences changed through another surface |
| `rf_reception` | Opt-in uncorrelated RF diagnostic |

Known event payloads are exact public shapes: all documented fields are
present and storage-only fields are projected out. A safe, syntactically valid
future event type keeps its envelope so clients can advance the cursor, but
this server replaces its unreviewed payload with `{}`. Clients should ignore
that unknown type. `snapshot_required` is reserved for the non-journal SSE
reset control event and is rejected as a durable journal type.
For `message`, `message_reception`, `message_send_state`, and `rf_reception`,
the envelope and payload `packet_hash` values are identical; a conflicting
durable row fails closed instead of exposing two correlation identities.

Message rows explicitly carry:

- `direction`: `in` or `out`;
- `state`: `received`, `pending`, `transmitted`, `confirmed`, `failed`, or
  `indeterminate` (and `heard_repeated` when applied as a lifecycle state);
- `source`: `radio`, `rest`, `frame`, or `operator`;
- `id` (the message ID), `packet_hash`, recipient/channel fields, and ACK
  correlation; lifecycle event payloads refer to that row as `message_id`;
- no frame-delivery `pending_for_frame` or `consumed_at` markers. Those are
  private to the parallel frame queue and are not REST chat state.

Clients should render lifecycle from these fields, not infer it from timing.

Durable does not mean unbounded. `storage.retention.companion_events_days`
controls journal and soft-consumed message-history retention (1–36,500 days;
default 31). Pruning advances the journal floor, so an older cursor receives
`snapshot_required` instead of a silent gap. Unconsumed Frame queue entries
are never deleted by age alone.

## 7. Sending and idempotency

`POST .../messages` requires exactly one target:

```json
{"channel_idx": 0, "text": "hello"}
```

or:

```json
{"to": "<64 hex characters>", "text": "hello", "txt_type": 0}
```

It also requires `Idempotency-Key`: 1–128 visible ASCII characters (`!`
through `~`) with no whitespace. UUIDs are the recommended readable form.
Message text must be valid UTF-8 and cannot contain NUL; MeshCore text payloads
are NUL-terminated, so accepting it would make stored and received text differ.

For a paired device, the principal is the full companion identity plus the
stable `device_id`—not a database row or token ID. Rotating the token or
re-pairing that same device therefore cannot make an unresolved key transmit
again. A client must not switch device IDs to bypass an unknown result.

The server:

1. checks for an existing principal/key record;
2. replays a matching completed/failed result without charging RF admission;
3. rejects conflicting, in-progress, or indeterminate reuse;
4. charges RF admission only for an absent key;
5. atomically reserves the key;
6. creates a durable outbound `pending` message;
7. calls the shared bridge/radio path;
8. records the resulting lifecycle, then closes the replay record. If any
   partial finalization leaves the RF outcome uncertain, compensation marks
   both surfaces `indeterminate` instead of pretending the send failed. If
   that compensation write is temporarily unavailable, the running process
   remembers the key as indeterminate, blocks redispatch, and repairs both
   durable rows on a later same-key request. Startup recovery performs the
   same fail-closed repair after a process restart.

Two simultaneous first attempts cannot both transmit: the atomic reservation
selects one winner. A terminal radio rejection is stored and replayed just
like a success. If the server cannot even hand the operation to its event
loop, it does not call the bridge or radio; it closes the reservation as the
same terminal `sent: false`, `state: failed` result. Every terminal failed
response includes a short human-readable `reason` and is replayed unchanged.

Completed and failed keys replay for at least 48 hours from their first
reservation; maintenance timing may retain them longer. A client with a lost
result must reconcile within that window. Afterward, inspect durable history
and require an explicit decision rather than assuming the old key still
suppresses RF. Indeterminate outcomes are retained and never made reusable by
that terminal-record cleanup.

If RF may have started but the request times out or final persistence fails,
the result is `indeterminate`. Retry only with the same key. Creating a new
key could duplicate the RF send.

Direct sends return after radio acceptance and do not block for an ACK. A
later ACK produces `message_send_state: confirmed` with the same message ID.
Channel sends have no ACK.

All REST RF actions share a small per-principal and process-wide token bucket.
Defaults are documented in `config.yaml.example`. The radio queue and duty
cycle remain authoritative after API admission.

Login/status/telemetry/ping, anonymous metadata queries, and path discovery are
synchronous RF actions without idempotency keys.
Login passwords are at most 15 UTF-8 bytes and cannot contain NUL, matching
MeshCore's NUL-terminated credential wire format.
Treat their timeout as unknown and do not blindly retry. Ping uses a direct
TRACE owned by the selected companion identity and accepts only repeater
contacts. `logout` also has no idempotency key: it orders itself after an
in-flight Frame or REST login for the same destination, clears the local
remote-session record, and attempts one best-effort RF send. Its response
keeps those outcomes explicit as `{logged_out, sent}`. `connection` and
`reset_path` are local state operations and do not consume the RF budget.

`anonymous_request` accepts a full 32-byte public key and one public request
kind (`owner`, `regions`, or `basic`). On protocol v13-capable Core versions,
the target is transient and is not added to the companion's contact list.
`path_discovery` requires an existing contact and returns the correlated
outbound and return paths as encoded lengths plus individual hash elements.

## 8. Contacts and channels

Mutations run on the shared companion event loop. In-memory state, SQLite
state, and the journal are reconciled as one operation; a storage failure
rolls back the in-memory mutation instead of leaving REST and frame clients
with different views.

Writable contact fields are intentionally narrow:

- `name`
- `adv_type`
- `favorite`
- `gps_lat`
- `gps_lon`

`name` is required when inserting a new contact; `adv_type` accepts `1..255`
and defaults to the normal chat-contact type (`1`). Core reserves `0` for its
transient non-contact value, so the API rejects it instead of creating a row
that would be absent from the next snapshot. On update, every omitted field
keeps its current value.

Learned route fields, advert timestamps, and unrelated flag bits are
server-owned and preserved.

Channel `secret` accepts 16 or 32 bytes as hex and is write-only. Snapshot,
responses, device listings, and events expose only channel index/name.

## 9. Authentication and pairing

Credential classes:

| Credential | Effective access |
|---|---|
| Operator JWT | Admin |
| Explicit `admin` API token | Admin |
| Legacy stored token with NULL scope | Admin migration compatibility |
| `companion:*` API token | All v1 companions |
| Paired `companion:{name}` token | Its immutable companion identity in v1 |
| Unknown explicit scope | Rejected |

Device tokens may use `Authorization: Bearer` or `X-API-Key`, but authorization
still restricts them to `/api/v1`. A v1 request presents exactly one credential
transport: `Authorization`, `X-API-Key`, or the SSE-only `?token=` operator
JWT. Multiple transports return `400` instead of silently choosing a broader
or stale principal. Legacy Repeater API credential precedence is unchanged.
The paired device row binds the token to the full public identity, not only
the mutable name or eight-bit companion hash.
Push fan-out and the final pre-delivery check both require that same full
identity, so a hash collision cannot select, wake, or preview another
identity's paired device.

`repeater.security.jwt_secret` is either generated and durably saved before
the HTTP server starts, or is an explicit value of at least 32 UTF-8 bytes.
Changing an established value invalidates both operator JWTs and the keyed
hashes of stored API/device tokens, so rotate it only with a plan to reissue
tokens and pair devices again. If token storage is temporarily unavailable,
HTTP authentication returns `503` and packet WebSockets close with `1011`;
neither condition is presented as a bad credential.

Long-lived transports recheck the same decision. Companion and legacy SSE
streams and `/ws/companion_frame` close at JWT expiry; an idle packet
WebSocket closes no later than its next 15-second authorization check.
API-token metadata is likewise revalidated at most every 15 seconds. The
packet WebSocket keeps its existing 30-second wire ping cadence; the
authorization check is a separate internal wake. WebSocket handshakes accept
exactly one credential. Their documented query keys are strict
(`token`/`client_id` for
packets; `token`/`companion_name` for the frame proxy), so duplicates, unknown
keys, and a simultaneous query credential plus `X-API-Key` fail visibly.
Packet WebSocket clients should put API tokens in `X-API-Key`; its legacy
`?token=<api-token>` form remains accepted for compatibility, is deprecated,
and receives the same admin-scope and periodic-revocation checks. The
companion Frame proxy accepts only JWTs in its query and API tokens in
`X-API-Key`.

Pairing:

1. an admin calls `/pair/start`;
2. the server returns a random 128-bit code, five-minute TTL, companion name,
   immutable public identity, and its SHA-256 fingerprint;
3. the device calls `/pair` with code, stable `device_id`, display name, and
   optional platform;
4. device row and hashed token are created in one transaction;
5. plaintext token, immutable identity, and fingerprint are returned once;
6. before storing the token, the device constant-time compares that
   fingerprint with the value transferred through the trusted pairing channel
   (for example, a QR code).

Codes are in memory, single-use, and rate-limited per remote address. A
transient storage failure restores a still-live code. Device revocation
deletes the device/token binding atomically.

`device_id` is globally unique on one repeater, not scoped to a companion.
An app pairing the same installation with multiple companion identities must
derive and persist a distinct stable ID for each one (for example,
`<installation-id>:<companion-identity-prefix>`). Reusing an ID returns `409`;
the server does not silently move the existing device or retain the newly
minted token.

The fingerprint is an identity-change/TOFU check, not proof that a plaintext
HTTP endpoint possesses the companion key: a malicious endpoint could copy a
public value. TLS, an encrypted overlay, or a trusted LAN remains necessary.

`GET /devices` is admin-only and redacts token IDs, push tokens, relay values,
and mention keywords.

## 10. Network and input safety

- The setup wizard and first-run config restore share one serialized,
  persisted bootstrap gate. A malformed request does not consume it, and once
  one valid request sets `setup_complete`, another public mutation cannot win
  a race; later imports require an administrator.
- `/server_info` reports HTTP/HTTPS and whether a trusted network is required.
- Plain HTTP is suitable only on a trusted LAN or encrypted overlay.
- CORS is off by default. When enabled, `web.cors_origins` is an exact
  allow-list; wildcard origins and credentialed wildcard behavior are not
  supported.
- JSON request bodies are bounded to 16 KiB.
- Endpoints reject unknown writable fields and bound text, identifiers,
  arrays, channel indices, public keys, GPS values, and non-finite numbers.
- Client and push HTTP implementations do not follow redirects with
  credentials or request bodies.
- The SQLite database contains decrypted companion message history. Protect
  the storage directory and backups as sensitive data.

## 11. Push wakes

Push delivery is optional. Configure one operator-controlled endpoint:

```yaml
companion:
  push:
    relay_url: "https://push.example.net/notify"
```

HTTPS is required by default. `allow_insecure_http: true` exists only for an
explicitly trusted local test relay. Redirects are not followed.

A device registers:

- `push_token`
- `push_detail`: `none`, `count`, or `preview`
- `mention_push`
- optional `mention_keywords`

It cannot register a relay URL. `none` is a content-free wake; `count` adds a
badge hint; `preview` deliberately sends truncated message content. Mention
alerts contain only “You were mentioned,” never the message text.

Exact mention matching retains at most 64 texts per coalesced burst. If a
larger burst arrives, mention-enabled devices conservatively receive the same
content-free alert; this keeps memory bounded without silently missing a
mention, at the cost of a possible generic false-positive alert during an
exceptional burst.

Only inbound durable message events produce push wakes. Local sends from the
REST or parallel frame client are already known locally and do not wake paired
devices.

The notifier uses one coordinator and a bounded worker pool (1–4). It
coalesces bursts, retries transient failures with capped backoff, drops after
the attempt cap, and clears a device push token on HTTP 410. With no relay URL,
the worker does not start and sync/SSE remain fully functional.

## 12. RF observations

Packet storage is process-wide, so every RF endpoint first establishes
companion ownership before exposing global packet rows.

- Message receptions require a message ID scoped to the companion.
- Heard repeats require a packet hash belonging to a durable outbound message
  for that companion, then query only post-transmit OTA duplicate rows.
- Contact paths begin with companion-scoped messages from that sender.

An RF duplicate can race the blocking message insert. The correlation tracker
holds one bounded cumulative aggregate until the row has a durable message ID,
then advances the row and appends the detailed observation event atomically.
It never publishes a correlation event with `message_id: null`; failed or
deduplicated provisional inbound inserts are discarded.

All observation queries have a bounded window (default 24 hours, clamped from
60 seconds to 7 days), use indexed packet hashes, and return at most 500
packet observations. `truncated: true` means the returned counts cover only
that bounded result. Responses say `observations_pruned: true` when the
requested window extends beyond retained packet history. Path hash resolution
can be `unique`, `ambiguous`, or `unknown`; clients should display the raw
hash unless it is unique.

## 13. Client loop

A complete chat client needs only this loop:

1. Pair once and store the device token in protected storage.
2. Fetch snapshot; atomically persist state and cursor.
3. Apply `sync` pages in order until `has_more` is false.
4. Optionally hold SSE and persist each event `id` after applying its data.
5. On `snapshot_required`, discard the old cursor and take a new snapshot.
6. For every send, generate one key and persist it with the local draft before
   calling the API.
7. Reuse that key after transport uncertainty; never invent a new key for an
   indeterminate attempt.

Events are forward-compatible at the type boundary: ignore unknown event
types. Validate the exact documented shape for known types, and never advance
the stored cursor until the containing event/page has been durably applied.

## 14. Deliberate exclusions

The v1 chat surface does not expose:

- private-key export/import or signing;
- raw packet injection;
- radio, tuning, repeat, or daemon configuration;
- frame socket/session control;
- self-advert setters/sending;
- contact import/export/share blobs;
- auto-add policy mutation;
- arbitrary binary/anonymous/control requests.

These are operator, diagnostic, or high-trust frame capabilities. Adding them
to a device-token surface would collide with shared-radio ownership or expand
the effect of a leaked chat credential.
