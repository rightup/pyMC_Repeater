# Companion frame protocol vs. Mobile Companion API v1

An inventory of what the TCP frame protocol (port 5000 by default,
`openhop_core.companion.frame_server`)
can do and whether `/api/v1` can do it, with a recommendation on each for a
first-party mobile chat client.

Compiled 2026-07-24 by comparing both implementations and driving each
surface through the independent clients and integration harnesses in
`companion_client/`.

**These are not two views of one API.** They differ in trust level as well as
coverage — the frame protocol hands out channel PSK secrets and can export the
node's private key; the REST surface deliberately exposes neither. Where REST
omits something, that is often the right answer rather than a gap.

Legend: **✅** supported · **⚠️** partial · **❌** absent

---

## 1. Session and handshake

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `APP_START` | pairing + `snapshot` | ✅ equivalent | None. REST bootstraps differently and better: a device token survives reconnects, where the frame protocol re-handshakes per socket. |
| `DEVICE_QUERY` | `GET /server_info` | ⚠️ partial | None. `server_info` gives version/auth modes; firmware/protocol version negotiation is frame-specific and meaningless over REST. |
| `HAS_CONNECTION` | `GET .../contacts/{pubkey}/connection` | ✅ | None. This is remote mesh-login state, not HTTP/Frame socket state. |
| `LOGOUT` | `POST .../contacts/{pubkey}/logout` | ✅ | None. The result separates local session closure from the best-effort RF send. |

The frame server serves **one client at a time and evicts the incumbent** on a
new connection. Its unauthenticated listener defaults to loopback; LAN binding
is an explicit trusted-network choice. The idle timeout defaults to 28,800
seconds (8 hours), with `tcp_timeout: 0` as the explicit opt-out. REST has no
such single-client limit — several devices can pair against the same companion.
For a REST/SSE-only companion, `frame_enabled: false` keeps the identity,
bridge, history, and shared radio path active without opening a frame port.
An enabled listener that cannot bind fails companion activation visibly.
The WebSocket compatibility proxy counts as that one frame client; it cannot
run beside another direct frame connection. Use REST/SSE for the parallel chat
client. Long-lived HTTP and WebSocket authorization is bounded: JWT sessions
close at expiration, and API-token sessions are revalidated at most every 15
seconds so revocation or a scope/device-binding change does not leave a stale
chat stream authorized.

Remote repeater login is separate from client authentication. A successful
`SEND_LOGIN` creates a bounded remote session; `connection` reads that local
session record without RF, and `logout` orders itself after any in-flight
Frame or REST login sharing the destination hash. It then closes the local
session and makes one best-effort RF send. Revoking `DELETE /devices/{id}`
still logs a phone/client out of this HTTP API; it does not log the companion
out of a remote repeater.

Listener ports are unique process-wide, including the HTTP port. This is a
small, observable configuration invariant: use `5000`, `5001`, and so on for
multiple companions rather than relying on bind-address-specific socket reuse.

A companion registration `name` is the stable 1–64 character ASCII slug used
in configuration and REST paths (`my-companion`). Its human-facing
`settings.node_name` is separate (`My Companion`) and follows MeshCore's
31-byte UTF-8 limit.

## 2. Messaging — the core chat loop

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `SEND_TXT_MSG` | `POST .../messages` `{to}` | ✅ | None. REST adds a mandatory `Idempotency-Key` (§6) the frame protocol has no equivalent of. |
| `SEND_CHANNEL_TXT_MSG` | `POST .../messages` `{channel_idx}` | ✅ | None. |
| `SYNC_NEXT_MESSAGE` | `snapshot` + `sync` + `GET .../messages` | ✅ better | None. Reset-safe cursors plus snapshot ETags beat drain-until-empty, and history paging has no frame equivalent. Frame pop now clears only the pending flag; durable API history remains. |
| `PUSH_CODE_MSG_WAITING` | `sync` poll / `GET .../events` (SSE) / push notifier | ✅ | None. |
| `PUSH_CODE_SEND_CONFIRMED` | `message_send_state` journal event | ✅ better | None. REST also reports `heard_repeated` and per-repeater correlation (architecture §12). |
| `SEND_CHANNEL_DATA` | — | ❌ | **Not needed for chat.** Binary channel payloads are an application-specific transport, not messaging. |

**Verdict: the core chat loop is complete over REST.** Everything a client needs
to send, receive, backfill and confirm messages is present, and in several
places the REST surface is the stronger one.

## 3. Contacts

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `GET_CONTACTS` | `snapshot.contacts` | ✅ read | None. |
| `GET_CONTACT_BY_KEY` | — (filter the snapshot list) | ⚠️ | Low priority. Clients hold the list in memory anyway. |
| `ADD_UPDATE_CONTACT` | `POST .../contacts/{pubkey}` | ✅ *(added 2026-07-18)* | None. |
| `REMOVE_CONTACT` | `DELETE .../contacts/{pubkey}` | ✅ *(added 2026-07-18)* | None. |
| `IMPORT_CONTACT` / `EXPORT_CONTACT` / `SHARE_CONTACT` | — | ❌ | **Do not add to the device surface.** Keep opaque contact transfer on the trusted Frame/operator surface. |
| `RESET_PATH` | `POST .../contacts/{pubkey}/reset_path` | ✅ | None. |
| `PUSH_CODE_ADVERT` / `NEW_ADVERT` | `contact` journal event | ✅ | None. |
| `PUSH_CODE_CONTACT_DELETED` | `contact` event, `change: remove` | ✅ *(added 2026-07-18)* | None. |
| `PUSH_CODE_CONTACTS_FULL` | `507` from `POST .../contacts/{pubkey}` | ✅ *(added 2026-07-18)* | None — surfaced as a status rather than an event, which suits a request/response surface. |

### Resolved 2026-07-18

The contact list used to be read-only here, which was the most likely thing to
block a real client — deleting a contact is ordinary chat-app behaviour with no
workaround. `POST` and `DELETE` on `/api/v1/companions/{name}/contacts/{pubkey}`
now close it, emitting `contact` journal events (`new` / `update` / `remove`)
so other synced devices follow along.

Two implementation notes:

- **An update preserves learned routing state.** `out_path`, `out_path_len` and
  advert timestamps come from the mesh; a client renaming a contact must not
  erase them, so the handler merges onto the existing record rather than
  replacing it.
- **A full store returns `507`**, the request/response equivalent of the frame
  protocol's `PUSH_CODE_CONTACTS_FULL`.

Most contacts still arrive on their own via **auto-add**: `base_contacts` adds
them from received adverts, filtered by `autoadd_config` (per contact type) and
`autoadd_max_hops`. The new endpoints matter for the ones that filter excludes,
and for deletion.

Auto-add policy (`SET_AUTOADD_CONFIG`) and contact import/export blobs remain
high-trust frame/operator capabilities. The chat API exposes the resulting
contact state, not the policy or opaque transfer format.

## 4. Channels

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `GET_CHANNEL` | `snapshot.channels` | ⚠️ names only | Fine as-is — see the PSK note. |
| `SET_CHANNEL` | `PUT .../channels/{index}` | ✅ *(added 2026-07-18)* | None — resolved via option 1 below (write-only PSK). |
| channel change notification | `channel` journal event | ✅ *(implemented 2026-07-18)* | None. |

The `channel` journal event carries `{index, name, change}` and never the PSK.

### The PSK problem, and how it was resolved

`snapshot.channels` returns `{index, name}` and deliberately withholds the
16-byte secret; the frame protocol's `CHANNEL_INFO` includes it. That is the
correct privacy posture for a surface reachable over the network — but it meant
a REST-only client **could not join a channel at all**, because joining needs
the PSK and there was no way to supply one either.

Resolved with a **write-only secret**:
`PUT /api/v1/companions/{name}/channels/{index}`
accepts `{name, secret}`, and no v1 endpoint ever returns a secret — not the
response, not the snapshot, not the `channel` journal event. The client learns
the PSK out of band (QR code, invite link), exactly as MeshCore clients do.

This keeps the API's core property — *the server never hands out secrets* —
while making join possible.

## 5. Contact actions

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `SEND_LOGIN` | `POST .../contacts/{pubkey}/login` | ✅ | None. |
| `HAS_CONNECTION` | `GET .../contacts/{pubkey}/connection` | ✅ | Local read; no RF admission charge. |
| `LOGOUT` | `POST .../contacts/{pubkey}/logout` | ✅ | One best-effort RF send; no idempotency key. |
| `SEND_STATUS_REQ` | `POST .../status_request` | ✅ | None. |
| `SEND_TELEMETRY_REQ` | `POST .../telemetry_request` | ✅ | None. |
| `SEND_BINARY_REQ` / `SEND_ANON_REQ` | — | ❌ | Not needed for chat. |
| `SEND_PATH_DISCOVERY_REQ` | `GET .../contacts/{pubkey}/paths` (read) | ⚠️ | Low priority — triggering discovery is a diagnostic. |
| `SEND_TRACE_PATH` | — | ❌ | Not needed for chat; diagnostic. |
| `SEND_CONTROL_DATA` | — | ❌ | Not needed for chat. |

### Synchronous action contract

The REST actions are deliberately synchronous: the handler blocks on the
shared bridge and returns its result inline. Login has a 15-second outer
guard, status uses a 15-second bridge timeout with a 20-second outer guard,
and telemetry uses 20/25 seconds. The frame protocol instead delivers
`PUSH_CODE_LOGIN_SUCCESS` / `STATUS_RESPONSE` / `TELEMETRY_RESPONSE`
asynchronously.

A multi-hop round trip can exceed those bounds. A timeout means the remote
outcome is unknown, not failed; clients must not blindly retry. The API does
not promise a later result event. That small, readable contract is complete
for v1 and avoids a second command/result lifecycle beside message delivery.

## 6. Node identity and adverts

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `SET_ADVERT_NAME` | — | ❌ | Keep on the frame/operator surface; it changes the shared companion identity. |
| `SET_ADVERT_LATLON` | — | ❌ | Keep on the frame/operator surface; it changes shared, privacy-sensitive state. |
| `SEND_SELF_ADVERT` | — | ❌ | Keep on the frame/operator surface; it transmits RF outside the chat send contract. |
| `GET_ADVERT_PATH` | `GET .../contacts/{pubkey}/paths` | ⚠️ related | None. |
| `GET_DEVICE_TIME` / `SET_DEVICE_TIME` | `/server_info` time + HTTP `Date` | ⚠️ read-only | Do not add the setter to the device surface. |

`prefs` changes are **observable but not settable** over REST. This lets a chat
client update its display without giving a device token control of shared
identity state.

## 7. Radio and device configuration

`SET_RADIO_PARAMS`, `SET_RADIO_TX_POWER`, `SET_TUNING_PARAMS`,
`GET_TUNING_PARAMS`, `SET_OTHER_PARAMS`, `SET_FLOOD_SCOPE`,
`SET`/`GET_DEFAULT_FLOOD_SCOPE`, `SET_PATH_HASH_MODE`,
`GET_ALLOWED_REPEAT_FREQ`, `GET_BATT_AND_STORAGE`, `GET_STATS`,
`SET`/`GET_AUTOADD_CONFIG`, `GET_CUSTOM_VARS` / `SET_CUSTOM_VAR`

**All ❌ on `/api/v1`. Keep it that way.**

These are device-administration concerns. They already exist on the admin `/api`
tree used by the web UI, behind operator auth. A device-scoped token should not
retune the radio or change contact policy for every other client sharing that
repeater.

The unversioned `/api/companion/*` tree is also an operator compatibility
surface, not a chat-client alternative. Its shapes remain stable for existing
Repeater API users and it does not acquire v1 idempotency keys. New clients use
`/api/v1`; legacy direct sends preserve upstream's wait-for-ACK behavior and
response schema. The shared bridge still records a packet accepted by the
radio when the later ACK wait times out, so operator and chat history do not
silently diverge.

## 8. Keys, signing, raw packets

`EXPORT_PRIVATE_KEY`, `IMPORT_PRIVATE_KEY`, `SIGN_START` / `SIGN_DATA` /
`SIGN_FINISH`, `SEND_RAW_DATA`, `SEND_RAW_PACKET`

**All ❌. Recommendation: never add these to `/api/v1`.**

The frame protocol is reached over an unauthenticated socket (loopback by
default; an explicit LAN bind is a generous trust assumption). REST is
authenticated but network-reachable and token-bearing — private key export
there would turn a leaked device token into a full identity compromise. Raw
packet injection would turn it into an arbitrary-transmit primitive.

## 9. Where REST is ahead

The frame protocol has no equivalent of:

- `GET .../messages/{id}/receptions` — per-message RF reception detail
- `GET .../contacts/{pubkey}/paths` — path history with collision-safe resolution
- `GET .../transmissions/{packet_hash}/repeats` — who repeated our send
- `GET .../messages` — history paging with `before_id`
- snapshot ETag/`304` conditional fetch, and `epoch:seq` resumable sync
- Idempotency keys on send
- Multiple concurrent devices per companion
- Push notification registration through one operator-owned relay

---

## Summary: v1 chat completion boundary

The formerly blocking chat operations are implemented:

1. ~~Contact delete~~ — `DELETE .../contacts/{pubkey}`, emits
   `contact` / `change: remove`.
2. ~~Contact add~~ — `POST .../contacts/{pubkey}`, preserves learned routing
   state on update, `507` when the store is full.
3. ~~Channel join~~ — `PUT .../channels/{index}` with a write-only PSK.
4. ~~Remote login lifecycle~~ — `GET .../connection` and
   `POST .../logout`, distinct from API-device revocation.

Self-advert control, radio/auto-add policy, private-key operations, raw packet
injection, and contact transfer blobs remain deliberately outside the chat
surface. Login/status/telemetry keep their synchronous, timeout-is-unknown
contract. None of these exclusions prevents a complete REST chat client, and
adding them would expand device-token authority or create a second lifecycle
without a demonstrated chat need.

**Deliberately excluded, and should stay that way:** radio/tuning
configuration, private key export/import, signing, raw packet transmission,
and direct Frame/HTTP socket-session management.

**Everything else is already there.** The core chat loop — send, receive,
backfill, confirm, push — plus contact and channel management, is complete on
`/api/v1`, and in several respects better than the frame protocol.
