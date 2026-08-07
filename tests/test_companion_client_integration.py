"""End-to-end: companion_client driving a real CompanionFrameServer over TCP,
through the real journal and push notifier, to a captured relay POST.

This is the path the handoff records as untested -- "the notifier->relay POST
path itself is unit/integration-tested rather than live... triggering a live
`message` journal event needs a companion frame client (TCP 5000), out of
scope for a curl smoke". Everything here is real except the bridge (no radio)
and the relay (a local capture listener).

Tests are ``asyncio.run``-driven to match the convention in
tests/test_companion_event_journal.py rather than adding an asyncio plugin mode.
"""

from __future__ import annotations

import asyncio

import pytest

from companion_client.client import CommandError, CompanionClient
from companion_client.push_listener import PushListener
from repeater.companion.push_notifier import CompanionPushNotifier

from .companion_harness import start_harness, wait_for

TOKEN = "a" * 64


def run(coro):
    return asyncio.run(coro)


async def connected_client(harness) -> CompanionClient:
    client = CompanionClient("127.0.0.1", harness.port)
    await client.connect()
    return client


def register_push_device(harness, listener, *, detail="none", mention_push=None, keywords=None):
    """Register a device the way POST /api/v1/devices/{id}/push would."""
    token_id = harness.handler.create_api_token("t", "hash-1", scope="companion:x")
    harness.handler.companion_device_create(
        harness.companion_hash,
        "dev-1",
        "Phone",
        token_id,
        platform="ios",
        companion_identity=harness.bridge.get_public_key().hex(),
    )
    harness.handler.companion_device_set_push(
        "dev-1",
        TOKEN,
        push_detail=detail,
        mention_push=mention_push,
        mention_keywords=keywords,
    )


def start_notifier(harness, **kwargs) -> CompanionPushNotifier:
    notifier = CompanionPushNotifier(
        harness.handler,
        min_interval=kwargs.pop("min_interval", 0.2),
        allow_insecure_http=kwargs.pop("allow_insecure_http", True),
        **kwargs,
    )
    notifier.start()
    harness.journal.register_listener(
        notifier.make_listener(
            harness.companion_hash,
            harness.bridge.get_public_key().hex(),
        )
    )
    return notifier


# --- handshake ------------------------------------------------------------


def test_connect_returns_self_info(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                info = client.self_info
                assert info.node_name == "TestNode"
                assert info.companion_hash == harness.companion_hash.removeprefix("0x")
                assert info.spreading_factor == 10
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_second_client_evicts_the_first(tmp_path):
    """The server serves one client at a time and evicts the incumbent."""

    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            first = await connected_client(harness)
            second = await connected_client(harness)
            try:
                assert second.self_info is not None
                assert await wait_for(lambda: first._reader.at_eof(), timeout=3)
            finally:
                await first.close()
                await second.close()
        finally:
            await harness.stop()

    run(scenario())


# --- sending --------------------------------------------------------------


def test_send_channel_message_reaches_the_bridge(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                await client.send_channel_message(0, "hello mesh", timestamp=4242)
                assert harness.bridge.sent_channel_messages == [(0, "hello mesh", 4242)]
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_send_to_unknown_channel_raises(tmp_path):
    """The server reports any channel-send failure as NOT_FOUND, matching
    firmware -- so an unknown channel surfaces as a CommandError."""

    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                with pytest.raises(CommandError):
                    await client.send_channel_message(99, "nowhere")
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_unicode_message_survives_the_round_trip(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                await client.send_channel_message(0, "héllo — 🛰 mesh")
                assert harness.bridge.sent_channel_messages[0][1] == "héllo — 🛰 mesh"
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


# --- receiving ------------------------------------------------------------


def test_inject_then_sync_delivers_the_message(tmp_path):
    """An inbound message persists to SQLite and is then pulled by the client
    via CMD_SYNC_NEXT_MESSAGE -- the real receive path."""

    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                await harness.inject_inbound_message("incoming!", "ph-1", timestamp=100)
                messages = await client.drain_messages()
                assert [m.text for m in messages] == ["incoming!"]
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_drain_stops_at_no_more_messages(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                for i in range(3):
                    await harness.inject_inbound_message(f"msg-{i}", f"ph-{i}", timestamp=i)
                messages = await client.drain_messages()
                assert sorted(m.text for m in messages) == ["msg-0", "msg-1", "msg-2"]
                assert await client.sync_next_message() is None
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_on_message_handler_fires(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            seen = []
            client.on_message(lambda m: seen.append(m.text))
            try:
                await harness.inject_inbound_message("callback me", "ph-cb")
                await client.drain_messages()
                assert seen == ["callback me"]
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


# --- the push chain -------------------------------------------------------


def test_inbound_message_produces_a_wake_push(tmp_path):
    """message -> journal -> notifier -> real HTTP POST, with nothing stubbed
    in between."""

    async def scenario():
        harness = await start_harness(tmp_path)
        listener = PushListener().start()
        notifier = None
        try:
            register_push_device(harness, listener, detail="none")
            notifier = start_notifier(harness, relay_url=listener.url)
            client = await connected_client(harness)
            try:
                await harness.inject_inbound_message("wake me", "ph-wake")
                assert listener.wait_for_push(1, timeout=6)
                push = listener.last()
                assert push.shape == "wake"
                assert push.body["push_token"] == TOKEN
                assert push.body["collapse_id"] == harness.companion_hash
                # Content-free by default: the text must not leave the repeater.
                assert "wake me" not in str(push.body)
            finally:
                await client.close()
        finally:
            if notifier:
                notifier.stop()
            listener.stop()
            await harness.stop()

    run(scenario())


def test_count_detail_carries_badge_hint(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        listener = PushListener().start()
        notifier = None
        try:
            register_push_device(harness, listener, detail="count")
            notifier = start_notifier(harness, relay_url=listener.url)
            await harness.inject_inbound_message("one", "ph-1")
            assert listener.wait_for_push(1, timeout=6)
            push = listener.last()
            assert push.shape == "count"
            assert push.body["badge_hint"] >= 1
        finally:
            if notifier:
                notifier.stop()
            listener.stop()
            await harness.stop()

    run(scenario())


def test_platform_field_is_forwarded(tmp_path):
    """Regression guard for the platform routing hint added 2026-07-18."""

    async def scenario():
        harness = await start_harness(tmp_path)
        listener = PushListener().start()
        notifier = None
        try:
            register_push_device(harness, listener)
            notifier = start_notifier(harness, relay_url=listener.url)
            await harness.inject_inbound_message("hi", "ph-p")
            assert listener.wait_for_push(1, timeout=6)
            assert listener.last().body["platform"] == "ios"
        finally:
            if notifier:
                notifier.stop()
            listener.stop()
            await harness.stop()

    run(scenario())


def test_mention_produces_a_content_free_alert(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        listener = PushListener().start()
        notifier = None
        try:
            register_push_device(harness, listener, mention_push=True, keywords=["adam"])
            notifier = start_notifier(harness, relay_url=listener.url)
            await harness.inject_inbound_message("hey adam are you there", "ph-m")
            assert listener.wait_for_push(1, timeout=6)
            push = listener.last()
            assert push.shape == "mention"
            assert push.body["alert"] == "You were mentioned"
            # The message text must never transit the relay for a mention.
            assert "are you there" not in str(push.body)
        finally:
            if notifier:
                notifier.stop()
            listener.stop()
            await harness.stop()

    run(scenario())


def test_non_matching_message_does_not_mention(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        listener = PushListener().start()
        notifier = None
        try:
            register_push_device(harness, listener, mention_push=True, keywords=["adam"])
            notifier = start_notifier(harness, relay_url=listener.url)
            await harness.inject_inbound_message("nothing relevant here", "ph-nm")
            assert listener.wait_for_push(1, timeout=6)
            assert listener.last().shape != "mention"
        finally:
            if notifier:
                notifier.stop()
            listener.stop()
            await harness.stop()

    run(scenario())


def test_burst_fires_immediately_then_collapses_the_rest(tmp_path):
    """The debounce is leading-edge AND trailing-edge, not purely trailing.

    Measured behaviour with min_interval=1.0 and five rapid messages: one push
    at t+0.02s with badge_hint=1, then a single collapsed push at t+1.00s with
    badge_hint=4. So the first message of a quiet period is prompt (latency ~0,
    not up to min_interval) and only the burst behind it is coalesced -- which
    is what keeps a chatty channel from becoming a push storm without making
    the first message wait.
    """

    async def scenario():
        harness = await start_harness(tmp_path)
        listener = PushListener().start()
        notifier = None
        try:
            register_push_device(harness, listener, detail="count")
            notifier = start_notifier(
                harness,
                min_interval=1.0,
                relay_url=listener.url,
            )
            for i in range(5):
                await harness.inject_inbound_message(f"burst-{i}", f"ph-b{i}")

            assert listener.wait_for_push(2, timeout=6)
            await asyncio.sleep(0.5)  # let any extra push show up

            counts = [p.body["badge_hint"] for p in listener.pushes]
            assert counts == [1, 4], f"expected leading 1 then collapsed 4, got {counts}"
            # Five messages, two pushes -- the collapse is doing real work.
            assert len(listener.pushes) == 2
        finally:
            if notifier:
                notifier.stop()
            listener.stop()
            await harness.stop()

    run(scenario())


def test_relay_410_clears_the_push_token(tmp_path):
    """410 is the only backpressure signal for a stale token; the notifier must
    clear push_token so it stops sending."""

    async def scenario():
        harness = await start_harness(tmp_path)
        listener = PushListener(status=410).start()
        notifier = None
        try:
            register_push_device(harness, listener)
            notifier = start_notifier(harness, relay_url=listener.url)
            await harness.inject_inbound_message("gone", "ph-410")
            assert listener.wait_for_push(1, timeout=6)

            def token_cleared():
                device = harness.handler.companion_device_get("dev-1")
                return device is not None and device.get("push_token") is None

            assert await wait_for(token_cleared, timeout=5)
        finally:
            if notifier:
                notifier.stop()
            listener.stop()
            await harness.stop()

    run(scenario())


# --- channels -------------------------------------------------------------


def test_list_channels_returns_configured_slots(tmp_path):
    """Enumerated per-index. The server's whole-table form replies with one
    frame per channel, which this client cannot match to a command."""

    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                channels = await client.list_channels()
                assert [(c.idx, c.name) for c in channels] == [
                    (0, "Public"),
                    (1, "#howltest"),
                    (2, "#seattle"),
                    (3, "#weather"),
                ]
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_unconfigured_slots_are_dropped(tmp_path):
    """The server zero-fills empty slots; an empty name means unused."""

    async def scenario():
        harness = await start_harness(tmp_path, channels=[(0, "Public"), (5, "#sparse")])
        try:
            client = await connected_client(harness)
            try:
                channels = await client.list_channels()
                assert [(c.idx, c.name) for c in channels] == [(0, "Public"), (5, "#sparse")]
                # Slot 1 exists in the table but is unconfigured.
                assert (await client.get_channel(1)).is_configured is False
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_send_targets_the_selected_channel(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                await client.send_channel_message(2, "to seattle", timestamp=7)
                assert harness.bridge.sent_channel_messages == [(2, "to seattle", 7)]
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_inbound_message_carries_its_channel(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                await harness.inject_inbound_message("hi", "ph-ch", channel_idx=3)
                messages = await client.drain_messages()
                assert messages[0].channel_idx == 3
                assert messages[0].is_channel is True
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


# --- channel journal events -----------------------------------------------
# Regression cover for a gap found 2026-07-18: channel changes were never
# journaled, so a client syncing from the journal could not learn about a
# channel being added, renamed or removed without re-snapshotting. Contacts
# already had this via record_contact; channels were "deferred to phase 2"
# and never picked up.


def channel_events(harness) -> list[dict]:
    return [
        event
        for event in harness.handler.companion_get_events(harness.companion_hash, 0)
        if event["event_type"] == "channel"
    ]


def test_set_channel_journals_an_update(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                await client.set_channel(7, "#newchan", bytes(32))
                events = channel_events(harness)
                assert len(events) == 1
                assert events[0]["payload"] == {
                    "index": 7,
                    "name": "#newchan",
                    "change": "update",
                }
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_channel_event_never_carries_the_psk_secret(tmp_path):
    """The journal feeds /sync to mobile clients, and the snapshot surface
    strips PSK secrets. Leaking one here would reach every synced device and,
    unlike a snapshot field, persist in the journal table."""

    async def scenario():
        harness = await start_harness(tmp_path)
        secret = bytes(range(32))
        try:
            client = await connected_client(harness)
            try:
                await client.set_channel(4, "#secret-chan", secret)
                payload = channel_events(harness)[0]["payload"]
                assert set(payload) == {"index", "name", "change"}
                assert secret.hex() not in str(payload)
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_rename_journals_an_update(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                await client.set_channel(1, "#renamed", bytes(32))
                events = channel_events(harness)
                assert events[-1]["payload"]["name"] == "#renamed"
                assert events[-1]["payload"]["change"] == "update"
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_clearing_a_slot_journals_a_removal(tmp_path):
    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                await client.set_channel(1, "", bytes(32))  # empty name clears
                events = channel_events(harness)
                assert events[-1]["payload"] == {
                    "index": 1,
                    "name": None,
                    "change": "remove",
                }
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_no_op_set_journals_nothing(tmp_path):
    """Before/after comparison, not blind trust that the command ran: setting
    a slot to what it already holds must not produce an event."""

    async def scenario():
        harness = await start_harness(tmp_path)
        try:
            client = await connected_client(harness)
            try:
                await client.set_channel(1, "#howltest", bytes([1]) * 16)
                assert channel_events(harness) == []
            finally:
                await client.close()
        finally:
            await harness.stop()

    run(scenario())


def test_bulk_save_at_stop_does_not_journal(tmp_path):
    """_save_channels also runs on shutdown; journaling there would replay the
    whole table into the journal on every restart."""

    async def scenario():
        harness = await start_harness(tmp_path)
        client = await connected_client(harness)
        await client.close()
        await harness.server.stop()
        assert channel_events(harness) == []

    run(scenario())
