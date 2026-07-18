# Companion client

A reference client for the repeater's companion interface, and the test
instrument the handoff kept asking for.

The handoff notes twice that the notifier→relay path could only be unit-tested
because *"triggering a live `message` journal event needs a companion frame
client (TCP 15050), out of scope for a curl smoke"*. This is that client.

## Layout

| Module | Depends on | Purpose |
|---|---|---|
| `protocol.py` | `openhop_core` only | frame codec, command builders, response parsers |
| `client.py` | `protocol` | async client: connect, send, sync, push handling |
| `push_listener.py` | stdlib | captures notifier POSTs so pushes are assertable |
| `simulator.py` | `repeater.*` | in-process frame server + journal for tests/demo |
| `web/` | `aiohttp` | browser chat UI over the same client |

`protocol.py`, `client.py`, and `push_listener.py` deliberately contain **no
`repeater.*` import** — they talk to the server over the wire exactly as a
phone app would. `simulator.py` is the one repeater-coupled module.

The frame codec imports its constants and framing from `openhop_core`, the same
package the server uses, so the wire format cannot drift between the two.

## Web chat UI

```bash
python -m companion_client.web.app                 # simulator on :8800
python -m companion_client.web.app --live --host 192.168.1.50 --port 15050
```

Simulator mode stands up a real frame server, journal, push notifier and
capture listener in-process. Send messages, hit **Receive** to simulate inbound
mesh traffic, and watch the resulting push appear — including the difference
between a `count` push (silent, badge only) and a `mention` push (visible alert
reading "You were mentioned", never the message text).

Live mode connects to a real repeater. Sending is real; receiving depends on
actual RF traffic, and pushes go wherever that device's registered relay points.

## Channels

Channels are enumerated per index rather than with the server's whole-table
form. `CMD_GET_CHANNEL` with an *empty* body replies with one `CHANNEL_INFO`
frame **per channel slot**, which this client cannot match to a command — the
protocol has no request IDs, so ordering is the only thing tying a response to
its request. Per-index costs a few more round trips and stays in lockstep.

Unconfigured slots come back zero-filled; an empty name means unused, and
`list_channels()` drops them.

```python
for channel in await client.list_channels():
    print(channel.idx, channel.name)
await client.send_channel_message(channel.idx, "hi")
```

Verified against a live dev repeater: one companion reported 23 configured
channels (`Public`, `#seattle`, `#weather`, …), another reported 2. The
simulator ships a small representative table rather than a single hardcoded
channel, so indexing is exercised the same way.

## Library use

```python
from companion_client.client import CompanionClient

async with CompanionClient("192.168.1.50", 15050) as client:
    print(client.self_info.node_name)
    await client.send_channel_message(0, "hello mesh")
    for message in await client.drain_messages():
        print(message.text)
```

Note `connect()` sends `CMD_DEVICE_QUERY` *before* `CMD_APP_START`: DEVICE_QUERY
is what sets the server's app target version, and without it the server replies
with the pre-V3 message frames (no SNR). APP_START's trailing bytes are
reserved, not a version — an easy thing to get backwards.

## Tests

```bash
pytest tests/test_companion_client_protocol.py     # codec, no server
pytest tests/test_companion_client_integration.py  # real server over TCP
```

The integration suite covers what was previously untestable: the handshake,
client eviction, channel sends reaching the bridge, the sync/receive path, and
the full push chain — wake / count / mention shapes, the `platform` routing
hint, debounce collapse, and relay `410` clearing the stored push token.

### What the debounce actually does

Measured, not assumed. With `min_interval=1.0` and five rapid inbound messages
the notifier emits **two** pushes: one at t+0.02s with `badge_hint=1`, then one
at t+1.00s with `badge_hint=4`.

So it is leading-edge *and* trailing-edge — the first message of a quiet period
goes out immediately and only the burst behind it is coalesced. The handoff
describes it as a "trailing-edge debounce (30s collapse)", which undersells it:
first-message push latency is ~0, not up to 30 seconds.

## Limits

- The bridge is a double; there is no radio. Outbound sends are recorded, not
  transmitted, and inbound messages are injected via
  `Harness.inject_inbound_message` rather than received over RF. Everything
  above the bridge — framing, dispatch, SQLite, journal, notifier, HTTP — is
  real.
- DM (`send_direct_message`) is implemented against the wire format but is not
  covered end-to-end, because the double has no contact store.
- `aiohttp` is needed only for the web UI, not for the library or the tests.

## Live mode and RF

`--live` talks to a real repeater. Reading is harmless, but **sending
transmits over the air** — on the dev radio that is 22 dBm at 910.525 MHz into
a public mesh with real people on channels like `#seattle` and `#emergency`.
The UI shows a banner in live mode and labels every message with its channel.
Pick the channel deliberately; `#howltest` is the safe one on this deployment.
