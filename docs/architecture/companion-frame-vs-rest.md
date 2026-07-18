# Companion frame protocol vs. Mobile Companion API v1

An inventory of what the TCP frame protocol (port 15050, `openhop_core.companion.frame_server`)
can do and whether `/api/v1` can do it, with a recommendation on each for a
first-party mobile chat client.

Compiled 2026-07-18 by building a client for both surfaces
(`companion_client/`) and driving them against a live repeater.

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
| `HAS_CONNECTION`, `LOGOUT` | — | ❌ | **Do not add.** These manage a stateful socket. REST is stateless; `DELETE /devices/{id}` is the real "log out". |

The frame server serves **one client at a time and evicts the incumbent** on a
new connection. REST has no such limit — several devices can pair against the
same companion. This is a genuine advantage of the REST surface, not a gap.

## 2. Messaging — the core chat loop

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `SEND_TXT_MSG` | `POST .../messages` `{to}` | ✅ | None. REST adds a mandatory `Idempotency-Key` (§6) the frame protocol has no equivalent of. |
| `SEND_CHANNEL_TXT_MSG` | `POST .../messages` `{channel_idx}` | ✅ | None. |
| `SYNC_NEXT_MESSAGE` | `snapshot` + `sync` + `GET .../messages` | ✅ better | None. Cursor/ETag beats drain-until-empty, and history paging has no frame equivalent. |
| `PUSH_CODE_MSG_WAITING` | `sync` poll / `GET .../events` (SSE) / push notifier | ✅ | None. |
| `PUSH_CODE_SEND_CONFIRMED` | `message_send_state` journal event | ✅ better | None. REST also reports `heard_repeated` and per-repeater correlation (§10.3). |
| `SEND_CHANNEL_DATA` | — | ❌ | **Not needed for chat.** Binary channel payloads are an application-specific transport, not messaging. |

**Verdict: the core chat loop is complete over REST.** Everything a client needs
to send, receive, backfill and confirm messages is present, and in several
places the REST surface is the stronger one.

## 3. Contacts — the largest real gap

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `GET_CONTACTS` | `snapshot.contacts` | ✅ read | None. |
| `GET_CONTACT_BY_KEY` | — (filter the snapshot list) | ⚠️ | Low priority. Clients hold the list in memory anyway. |
| `ADD_UPDATE_CONTACT` | — | ❌ | **Recommended.** See below. |
| `REMOVE_CONTACT` | — | ❌ | **Recommended.** See below. |
| `IMPORT_CONTACT` / `EXPORT_CONTACT` / `SHARE_CONTACT` | — | ❌ | Optional. Contact sharing is a nice-to-have; add after add/remove. |
| `RESET_PATH` | `POST .../contacts/{pubkey}/reset_path` | ✅ | None. |
| `PUSH_CODE_ADVERT` / `NEW_ADVERT` | `contact` journal event | ✅ | None. |
| `PUSH_CODE_CONTACT_DELETED` | — | ❌ | Add alongside remove — the `contact` event already carries `change: removed` in the design doc, so the shape exists. |
| `PUSH_CODE_CONTACTS_FULL` | — | ❌ | Low priority; surfaces a full contact store. |

### Why this matters, and how far the workaround goes

A REST client's contact list is **read-only**. It cannot add a contact, delete
one, or import a shared one.

The gap is softened by **auto-add**: `base_contacts` adds contacts from received
adverts automatically, filtered by `autoadd_config` (per contact type) and
`autoadd_max_hops`. So on a normally-configured repeater, contacts do appear —
via the snapshot and `contact` journal events — without any client action.

But the client cannot:

- add a contact that auto-add filtered out (wrong type, too many hops);
- delete a contact the user no longer wants;
- change the auto-add policy (`SET_AUTOADD_CONFIG` is also absent);
- import a contact shared out-of-band.

**Recommendation: add `POST` and `DELETE` on
`/v1/companions/{name}/contacts/{pubkey}`.** Deleting a contact is a normal,
expected operation in any chat app, and its absence is the most likely thing to
block a real client. Both map directly onto existing bridge calls
(`add_update_contact`, `remove_contact`) and onto the `contact` journal event
that already exists.

## 4. Channels

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `GET_CHANNEL` | `snapshot.channels` | ⚠️ names only | Fine as-is — see the PSK note. |
| `SET_CHANNEL` | — | ❌ | **Recommended, with a design decision first.** |
| channel change notification | `channel` journal event | ✅ *(implemented 2026-07-18)* | None. |

The `channel` journal event was **documented in both the design doc §9 and
openapi.yaml but never emitted** — the implementation was marked "deferred to
phase 2" and not picked up. A client trusting the spec would have waited
forever for an event that never came. Now implemented, carrying
`{index, name, change}` and never the PSK.

### The PSK problem

`snapshot.channels` returns `{index, name}` and deliberately withholds the
16-byte secret; the frame protocol's `CHANNEL_INFO` includes it. That is the
correct privacy posture for a surface reachable over the network.

The consequence: **a REST-only client cannot join a channel it does not already
have.** Joining requires the PSK, which REST will not hand out and has no way to
accept. A REST client can only use channels already configured on the repeater.

Three options, in order of preference:

1. **Add `PUT /v1/companions/{name}/channels/{index}` accepting a client-supplied
   PSK** — write-only, never echoed back. The client learns the PSK
   out-of-band (QR code, invite link), exactly as MeshCore clients do today.
   Preserves "the server never hands out secrets" while making join possible.
2. **Leave it out** and treat channel membership as an operator/web-UI
   function. Defensible for a single-user repeater, limiting for a shared one.
3. Expose secrets on the snapshot — **not recommended**, it discards the
   privacy property §11.4 is built on.

## 5. Contact actions

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `SEND_LOGIN` | `POST .../contacts/{pubkey}/login` | ✅ | None. |
| `SEND_STATUS_REQ` | `POST .../status_request` | ✅ | None. |
| `SEND_TELEMETRY_REQ` | `POST .../telemetry_request` | ✅ | None. |
| `SEND_BINARY_REQ` / `SEND_ANON_REQ` | — | ❌ | Not needed for chat. |
| `SEND_PATH_DISCOVERY_REQ` | `GET .../contacts/{pubkey}/paths` (read) | ⚠️ | Low priority — triggering discovery is a diagnostic. |
| `SEND_TRACE_PATH` | — | ❌ | Not needed for chat; diagnostic. |
| `SEND_CONTROL_DATA` | — | ❌ | Not needed for chat. |

### One caveat on the three that exist

The REST actions are **synchronous with a 15s timeout**: the handler blocks on
the bridge call and returns the result inline. The frame protocol instead
delivers `PUSH_CODE_LOGIN_SUCCESS` / `STATUS_RESPONSE` / `TELEMETRY_RESPONSE`
asynchronously, whenever they arrive.

The synchronous shape is simpler and fine for a nearby contact. But a multi-hop
mesh round trip can exceed 15s — and when it does, the REST caller gets a
timeout and **the response is dropped**, where a frame client would still have
received it.

The design doc §9 lists `login_result`, `status_response` and
`telemetry_response` as journal event types, which would fix this. **None are
implemented**, and with synchronous actions they are not currently needed. If
slow round trips prove to be a problem in practice, implementing those three
events (and making the actions return `202 Accepted`) is the principled fix.

## 6. Node identity and adverts

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `SET_ADVERT_NAME` | — | ❌ | Optional. Renaming your own node is plausible in a chat app; the `prefs` journal event already reports changes, so only the setter is missing. |
| `SET_ADVERT_LATLON` | — | ❌ | Optional, same reasoning. Privacy-sensitive. |
| `SEND_SELF_ADVERT` | — | ❌ | Optional — "announce me now" is a useful button. |
| `GET_ADVERT_PATH` | `GET .../contacts/{pubkey}/paths` | ⚠️ related | None. |
| `GET_DEVICE_TIME` / `SET_DEVICE_TIME` | `server.time` in snapshot | ⚠️ read-only | Do not add the setter to the device surface. |

`prefs` changes are **observable but not settable** over REST: the journal
reports them from any surface, but nothing on `/api/v1` can cause one.

## 7. Radio and device configuration

`SET_RADIO_PARAMS`, `SET_RADIO_TX_POWER`, `SET_TUNING_PARAMS`,
`GET_TUNING_PARAMS`, `SET_OTHER_PARAMS`, `SET_FLOOD_SCOPE`,
`SET`/`GET_DEFAULT_FLOOD_SCOPE`, `SET_PATH_HASH_MODE`,
`GET_ALLOWED_REPEAT_FREQ`, `GET_BATT_AND_STORAGE`, `GET_STATS`,
`SET`/`GET_AUTOADD_CONFIG`, `GET_CUSTOM_VARS` / `SET_CUSTOM_VAR`

**All ❌ on `/api/v1`. Recommendation: keep it that way** — with one exception.

These are device-administration concerns. They already exist on the admin `/api`
tree used by the web UI, behind operator auth. A device-scoped token should not
retune the radio for every other user of that repeater.

The exception is **`autoadd_config`**, which shapes the contact list a chat
client depends on. If contact add/remove lands (§3), exposing the auto-add
policy read-only in the snapshot would help a client explain *why* a contact it
expected is missing.

## 8. Keys, signing, raw packets

`EXPORT_PRIVATE_KEY`, `IMPORT_PRIVATE_KEY`, `SIGN_START` / `SIGN_DATA` /
`SIGN_FINISH`, `SEND_RAW_DATA`, `SEND_RAW_PACKET`

**All ❌. Recommendation: never add these to `/api/v1`.**

The frame protocol is reached over a LAN socket with no authentication, which
is already a generous trust assumption. The REST surface is authenticated but
network-reachable and token-bearing — private key export there would turn a
leaked device token into a full identity compromise. Raw packet injection would
turn it into an arbitrary-transmit primitive.

## 9. Where REST is ahead

The frame protocol has no equivalent of:

- `GET .../messages/{id}/receptions` — per-message RF reception detail (§10.4)
- `GET .../contacts/{pubkey}/paths` — path history with resolution (§10.5)
- `GET .../transmissions/{packet_hash}/repeats` — who repeated our send (§10.3)
- `GET .../messages` — history paging with `before_id`
- ETag/`304` conditional fetch, and cursor-based resumable sync
- Idempotency keys on send
- Multiple concurrent devices per companion
- Push notification registration

---

## Summary: what a complete REST chat client still needs

**Blocking-ish (recommended):**

1. **Contact delete** — `DELETE /v1/companions/{name}/contacts/{pubkey}`. Normal
   chat-app behaviour, no workaround.
2. **Contact add** — `POST` the same path. Auto-add covers the common case, so
   this is about the contacts auto-add filters out.
3. **Channel join** — `PUT /v1/companions/{name}/channels/{index}` taking a
   write-only PSK. Requires the design decision in §4 first.

**Worth having:**

4. `PUSH_CODE_CONTACT_DELETED` → a `contact` event with `change: removed`
   (the design doc already specifies this shape).
5. Self-advert control: set name/latlon, send advert.
6. Auto-add policy, at least read-only in the snapshot.

**Deliberately excluded, and should stay that way:** radio/tuning
configuration, private key export/import, signing, raw packet transmission,
socket-session management.

**Everything else is already there.** The core chat loop — send, receive,
backfill, confirm, push — is complete on `/api/v1`, and in several respects
better than the frame protocol.
