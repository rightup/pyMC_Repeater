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

## 3. Contacts

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `GET_CONTACTS` | `snapshot.contacts` | ✅ read | None. |
| `GET_CONTACT_BY_KEY` | — (filter the snapshot list) | ⚠️ | Low priority. Clients hold the list in memory anyway. |
| `ADD_UPDATE_CONTACT` | `POST .../contacts/{pubkey}` | ✅ *(added 2026-07-18)* | None. |
| `REMOVE_CONTACT` | `DELETE .../contacts/{pubkey}` | ✅ *(added 2026-07-18)* | None. |
| `IMPORT_CONTACT` / `EXPORT_CONTACT` / `SHARE_CONTACT` | — | ❌ | Optional. Contact sharing is a nice-to-have; add after add/remove. |
| `RESET_PATH` | `POST .../contacts/{pubkey}/reset_path` | ✅ | None. |
| `PUSH_CODE_ADVERT` / `NEW_ADVERT` | `contact` journal event | ✅ | None. |
| `PUSH_CODE_CONTACT_DELETED` | `contact` event, `change: removed` | ✅ *(added 2026-07-18)* | None. |
| `PUSH_CODE_CONTACTS_FULL` | `507` from `POST .../contacts/{pubkey}` | ✅ *(added 2026-07-18)* | None — surfaced as a status rather than an event, which suits a request/response surface. |

### Resolved 2026-07-18

The contact list used to be read-only here, which was the most likely thing to
block a real client — deleting a contact is ordinary chat-app behaviour with no
workaround. `POST` and `DELETE` on `/v1/companions/{name}/contacts/{pubkey}`
now close it, emitting `contact` journal events (`new` / `update` / `removed`)
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

Still absent: **auto-add policy is not settable here** (`SET_AUTOADD_CONFIG`),
and there is no contact import/export blob. Exposing the policy read-only in
the snapshot would let a client explain *why* an expected contact is missing.

## 4. Channels

| Frame command | REST | Status | Recommendation |
|---|---|---|---|
| `GET_CHANNEL` | `snapshot.channels` | ⚠️ names only | Fine as-is — see the PSK note. |
| `SET_CHANNEL` | `PUT .../channels/{index}` | ✅ *(added 2026-07-18)* | None — resolved via option 1 below (write-only PSK). |
| channel change notification | `channel` journal event | ✅ *(implemented 2026-07-18)* | None. |

The `channel` journal event was **documented in both the design doc §9 and
openapi.yaml but never emitted** — the implementation was marked "deferred to
phase 2" and not picked up. A client trusting the spec would have waited
forever for an event that never came. Now implemented, carrying
`{index, name, change}` and never the PSK.

### The PSK problem, and how it was resolved

`snapshot.channels` returns `{index, name}` and deliberately withholds the
16-byte secret; the frame protocol's `CHANNEL_INFO` includes it. That is the
correct privacy posture for a surface reachable over the network — but it meant
a REST-only client **could not join a channel at all**, because joining needs
the PSK and there was no way to supply one either.

Resolved with a **write-only secret**: `PUT /v1/companions/{name}/channels/{index}`
accepts `{name, secret}`, and no v1 endpoint ever returns a secret — not the
response, not the snapshot, not the `channel` journal event. The client learns
the PSK out of band (QR code, invite link), exactly as MeshCore clients do.

This keeps the property the design is built on — *the server never hands out
secrets* — while making join possible. The two rejected alternatives were
leaving channel membership to the operator/web UI (defensible on a single-user
repeater, limiting on a shared one) and exposing secrets on the snapshot, which
would discard §11.4 entirely.

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

**Implemented 2026-07-18** — the three that were blocking:

1. ~~Contact delete~~ — `DELETE .../contacts/{pubkey}`, emits
   `contact` / `change: removed`.
2. ~~Contact add~~ — `POST .../contacts/{pubkey}`, preserves learned routing
   state on update, `507` when the store is full.
3. ~~Channel join~~ — `PUT .../channels/{index}` with a write-only PSK.

**Still worth having:**

4. Self-advert control: set name/latlon, send advert.
5. Auto-add policy (`autoadd_config` / `autoadd_max_hops`), at least read-only
   in the snapshot, so a client can explain a missing contact.
6. Contact import/export blob, for out-of-band contact sharing.
7. `login_result` / `status_response` / `telemetry_response` journal events, if
   slow multi-hop round trips prove to outlast the synchronous 15s budget (§5).

**Deliberately excluded, and should stay that way:** radio/tuning
configuration, private key export/import, signing, raw packet transmission,
socket-session management.

**Everything else is already there.** The core chat loop — send, receive,
backfill, confirm, push — plus contact and channel management, is complete on
`/api/v1`, and in several respects better than the frame protocol.
