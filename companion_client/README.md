# Companion clients

This directory contains reference clients for two different companion
interfaces:

| Module | Interface | Use |
|---|---|---|
| `rest.py` | Mobile Companion API v1 (`/api/v1`) | Authenticated, durable chat clients |
| `protocol.py` + `client.py` | MeshCore TCP frame protocol (port 5000 by default) | Standard frame clients and compatibility testing |
| `rest_simulator.py` | Real v1 HTTP tree with a fake bridge | REST integration tests |
| `simulator.py` | In-process frame server | Frame integration tests |
| `push_listener.py` | Local capture relay | Push-notifier tests/demo |
| `web/` | REST reference chat UI | Human-readable end-to-end example |

The interfaces are intentionally not equivalent. The frame protocol is a
trusted, unauthenticated device interface that can export identity/channel
secrets and permits one connected client per companion. REST uses scoped
tokens, durable non-destructive history, independent cursors, and never
returns those secrets.

Both ultimately call the same server-side bridge and radio path. Running a
REST chat client beside a frame client does not create another radio owner.
Every companion TCP listener and the HTTP listener needs a distinct port.
The server's WebSocket compatibility proxy occupies the same one-client frame
slot and therefore is not a parallel chat transport. A REST/SSE-only companion
can set `settings.frame_enabled: false`; its bridge and radio access remain
active without reserving an unauthenticated frame port.

## REST client

`CompanionRestClient` uses only the Python standard library.
Its default 30-second request timeout covers the API's bounded remote
login/status/telemetry waits; callers may choose a longer explicit timeout for
unusually slow networks.
It does not follow redirects or honor ambient HTTP proxy variables, so bearer
tokens stay on the explicitly configured repeater connection.

The public methods cover the complete v1 tree:

| Need | Methods | Auth / behavior |
|---|---|---|
| Discover and pair | `server_info`, `pair_start`, `pair` | Discovery and exchange are public; starting a code is admin-only. |
| Bootstrap and catch up | `companions`, `snapshot`, `sync`, `follow` | Device tokens see only their bound companion. |
| History and live tail | `messages`, `events` | History pages newest-first; SSE is resumable and does not reconnect itself. |
| Send chat | `send_message` | RF mutation; a caller-persisted `Idempotency-Key` is mandatory. |
| Contacts | `upsert_contact`, `set_favorite`, `delete_contact`, `reset_path` | Local state mutations; contact changes appear in the journal. |
| Channels | `set_channel`, `clear_channel` | PSKs are write-only and never returned. |
| Remote session | `login`, `has_connection`, `logout` | Query or change the login session with one remote contact. |
| Remote actions | `status_request`, `telemetry_request` | Synchronous RF requests; a timeout is an unknown outcome. |
| RF observations | `message_receptions`, `contact_paths`, `transmission_repeats` | Read-only, bounded-window evidence. |
| Devices and push | `devices`, `revoke_device`, `register_push`, `unregister_push` | Listing is admin-only; a device can manage only itself. |

Successful responses are checked against the v1 control fields before they
are returned. A malformed cursor, boolean, event id, pairing identity, or send
result fails as `RestError(status=502)` instead of being coerced into a value
that could lose or duplicate work.

### Pair and bootstrap

```python
from companion_client.rest import CompanionRestClient

client = CompanionRestClient("https://repeater.example")

started = client.pair_start("field-companion", admin_token)
paired = client.pair(
    started["code"],
    device_id="phone-stable-id",
    name="Field Phone",
    platform="ios",
    expected_fingerprint=started["fingerprint"],
)

snapshot, etag = client.snapshot("field-companion")
cursor = snapshot["cursor"]  # human-readable opaque form: epoch:seq
```

`pair_start` needs an operator token. Transfer its code and fingerprint through
the trusted pairing channel (typically a QR code). `pair` checks the returned
immutable identity before adopting the device token. Persist that token in
protected device storage. The fingerprint detects an unexpected identity; it
does not make plaintext HTTP resistant to an active attacker, so use TLS or a
trusted network.

The repeater treats `device_id` as globally unique, not per companion. If one
app installation pairs with multiple companion identities, derive and persist
a distinct stable ID for each identity (for example,
`<installation-id>:<companion-identity-prefix>`). Reuse returns `409`.

The pairing code is single-use. If the exchange response is lost, or identity
verification rejects it after the server created the device, do not guess at
the token: revoke that device as an operator and pair again.

Snapshot is the only conditional sync request:

```python
from companion_client.rest import NotModified

try:
    snapshot, etag = client.snapshot("field-companion", etag=etag)
except NotModified:
    pass
```

### Catch up

```python
events, cursor = client.follow("field-companion", cursor)
```

`follow` drains pages until `has_more` is false. Its explicit 100-page safety
bound prevents an ever-growing journal from consuming memory forever;
reaching it raises `RestError` rather than returning partial data. Pass
`max_pages=None` only when the application deliberately owns an unbounded
catch-up loop.

If the server returns `snapshot_required`, take a new snapshot. The
`SyncResult.reset_reason` field is one of `missing_epoch`, `epoch_mismatch`,
`future_cursor`, or `pruned_cursor`.

For the opt-in RF firehose, pass `include="rf_receptions"` to `sync`,
`follow`, or `events`. The selector is strict: blank or unknown tokens are
rejected locally and by the server instead of silently omitting data.

### Send

```python
key = client.new_idempotency_key()
save_key_with_draft(key)
result = client.send_message(
    "field-companion",
    "hello",
    channel_idx=0,
    idempotency_key=key,
)
```

Persist the key with the local draft before sending. If the HTTP result is
lost, retry with the same key. Never switch to a new key after an
`indeterminate` result; the original may already be over the air.
The reference client requires the key explicitly so transport loss cannot
hide the only safe retry handle. Keys use 1–128 visible ASCII characters with
no whitespace; `new_idempotency_key()` returns the recommended UUID form.
Message text is UTF-8 and cannot contain NUL because MeshCore peers treat it as
the end of the text.

Completed/failed keys replay for at least 48 hours from first reservation
(maintenance may retain them longer). Reconcile a lost result inside that
window. After it, inspect durable message history and require an explicit
decision instead of assuming the old key still prevents another RF send.
Indeterminate keys remain blocked.

The result includes a durable `message_id` and explicit `state`; a terminal
`failed` result also includes a human-readable `reason`.
`packet_hash` is present when the radio backend can provide it. Direct-message
ACK and heard-repeat changes arrive later as journal events with the same
identifiers.

Non-2xx responses raise `RestError`. Its `status`, full `body`, structured
`data`, and case-insensitive `headers` remain available, including
`Retry-After` and an indeterminate send record:

```python
from companion_client.rest import RestError

try:
    result = client.send_message(
        "field-companion",
        "hello",
        channel_idx=0,
        idempotency_key=key,
    )
except RestError as exc:
    retry_after = exc.headers.get("Retry-After")
    if isinstance(exc.data, dict) and exc.data.get("state") == "indeterminate":
        keep_draft_blocked_for_reconciliation(exc.data)
    raise
```

Connection failures and socket timeouts remain the standard-library transport
exceptions (`urllib.error.URLError`, `TimeoutError`). For `send_message`, that
is an uncertain HTTP result: retry only the same persisted key. The
login/logout/status/telemetry methods have no idempotency key and may already
have transmitted when they time out, so do not blindly retry them.
Login passwords are limited to 15 UTF-8 bytes and cannot contain NUL.

### Live events

```python
for event in client.events("field-companion", cursor=cursor):
    apply(event.data)
    if event.event_id is not None:
        cursor = event.event_id
        save_cursor(cursor)
```

The iterator does not reconnect automatically. Its default inactivity timeout
is at least 65 seconds, longer than the server's default 15-second keepalive.
Use `stream_timeout=` to override it. If a caller stops before EOF, close the
iterator (or wrap it in `contextlib.closing`) so the server's one-stream slot
is released promptly. EOF can also mean the JWT expired or the device token
was revoked/unpaired; the server rechecks long-lived authorization at most
every 15 seconds. Reauthenticate or pair again as appropriate, then resume
from the last cursor that was durably applied.

### Push registration

```python
client.register_push(
    paired["device_id"],
    push_token=platform_token,
    push_detail="none",
    mention_push=True,
    mention_keywords=["fieldphone"],
)
```

A device cannot provide a relay URL. The operator configures one trusted
`companion.push.relay_url` for the repeater. With no relay configured,
registration is harmless and sync/SSE remain the delivery mechanisms.

## Frame client

The frame codec imports constants and framing from the same pinned
`openhop_core` package as the server.

```python
from companion_client.client import CompanionClient

async with CompanionClient("127.0.0.1", 5000) as client:
    print(client.self_info.node_name)
    await client.send_channel_message(0, "hello mesh")
    for message in await client.drain_messages():
        print(message.text)
```

The server's safe default bind is loopback. Set a companion
`settings.bind_address` explicitly only when a direct client needs a trusted
LAN listener. The frame protocol has no application authentication. Its idle
timeout defaults to eight hours; set `settings.tcp_timeout: 0` only when the
operator intentionally wants no idle timeout. Set `settings.frame_enabled:
false` only when this companion will use REST/SSE and does not need a frame
client.

Only one frame client can be active for a companion. Connecting through the
WebSocket compatibility proxy evicts this direct client, because the proxy is
also a frame client. Use `CompanionRestClient` for any concurrent chat UI or
agent.

The companion registration `name` is a stable ASCII slug such as
`my-companion`; it is used in configuration and REST paths. `node_name` is the
human-facing advertised name and may be `My Companion`.

Channel indices are companion-local. Resolve a channel by name for each
companion; never reuse another companion's numeric index.

`connect()` sends `DEVICE_QUERY` before `APP_START`. That query selects the
frame version; without it the server can return older message frames without
SNR.

## Web reference client

The web demo uses REST, not the frame protocol:

```bash
python -m pip install -e '.[companion-web]'
python -m companion_client.web.app

python -m companion_client.web.app \
  --live \
  --base-url http://127.0.0.1:8000 \
  --companion field-companion
```

Simulator mode mounts the real v1 handlers and storage with a fake bridge. It
also configures its local capture listener as the notifier's operator relay.
The browser stores each pending send and its idempotency key in local storage
before the request, bound to the API base URL, full companion identity, stable
device id, and an opaque browser-session generation. The server's idempotency
principal remains stable across re-pairing; the local generation only keeps a
later browser session from offering an older session's draft. A lost response
within the same session restores the draft and reuses that exact key; a
different draft, server, or device cannot silently replace an unresolved send. A
validation/rate-limit response known to occur before radio work releases the
draft for editing; ambiguous transport, server, and conflict results keep it
blocked.

Live mode sends over the real radio. Choose the companion and channel
deliberately. It prompts for the admin token without echoing it. Non-interactive
agents may provide `OPENHOP_ADMIN_TOKEN` through their protected process
environment. There is intentionally no token command-line flag, so a
long-lived credential cannot be exposed in the process list.
The demo accepts browser traffic only through a loopback Host and same-origin
requests.
The demo device is ephemeral and is revoked on a clean shutdown after all
sends have definite results. It deliberately stays paired if a send is still
unresolved. That fail-closed row prevents a silent re-pair under the same
device id while its result is unknown. The stable default device id is
intentional: switching to a random id after a crash would make a persisted
send impossible to reconcile under the same server principal. Startup checks
for an orphan before
minting a new pairing code and reports the recovery explicitly: inspect
durable history and explicitly resolve every pending send before revoking the
old device as an operator, then restart. A new web session creates a new local
generation and will never offer the older session's pending draft as a safe
retry.

## Tests

```bash
pytest tests/test_companion_rest_client.py
pytest tests/test_companion_rest_client_unit.py
pytest tests/test_companion_web_app.py
pytest tests/test_companion_client_protocol.py
pytest tests/test_companion_client_integration.py
python scripts/check_openapi_contract.py --strict-methods
```

The REST suite uses a real CherryPy mount, authentication, token manager,
SQLite journal, and reference client. The frame suite exercises the real TCP
framing and dispatch path. Radio hardware is not required.
