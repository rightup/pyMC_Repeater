from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from companion_client.rest import RestError, SyncResult
from companion_client.web import app as web_app
from companion_client.web.app import ChatSession


class _FakeRestClient:
    def __init__(self, *, fail_once: bool = False):
        self.calls = []
        self.fail_once = fail_once
        self.token = "device-token"
        self.base_url = "https://repeater.example"
        self.timeout = 30.0
        self.revoked = []

    def send_message(
        self,
        companion_name,
        text,
        *,
        channel_idx,
        idempotency_key,
    ):
        self.calls.append(
            {
                "companion_name": companion_name,
                "text": text,
                "channel_idx": channel_idx,
                "idempotency_key": idempotency_key,
            }
        )
        if self.fail_once:
            self.fail_once = False
            raise urllib.error.URLError("response lost")
        return {
            "message_id": 41,
            "sent": True,
            "state": "transmitted",
            "packet_hash": "AABBCCDDEEFF0011",
        }

    def revoke_device(self, device_id):
        self.revoked.append(device_id)
        return {"revoked": True, "device_id": device_id}


def _session(client) -> ChatSession:
    session = ChatSession(
        live=True,
        base_url="https://repeater.example",
        companion="field-radio",
        admin_token="admin",
    )
    session.client = client
    session.channels = [{"index": 3, "name": "#field"}]
    return session


@pytest.mark.asyncio
async def test_web_send_replays_one_persisted_draft_without_duplicate_rendering():
    client = _FakeRestClient()
    session = _session(client)

    first = await session.send("hello", 3, "persisted-draft-key")
    second = await session.send("hello", 3, "persisted-draft-key")

    assert second is first
    assert len(client.calls) == 1
    assert client.calls[0]["idempotency_key"] == "persisted-draft-key"
    assert session.messages == [first]


@pytest.mark.asyncio
async def test_web_send_reuses_the_same_key_after_transport_loss():
    client = _FakeRestClient(fail_once=True)
    session = _session(client)

    with pytest.raises(urllib.error.URLError):
        await session.send("hello", 3, "persisted-draft-key")
    result = await session.send("hello", 3, "persisted-draft-key")

    assert result["id"] == 41
    assert [call["idempotency_key"] for call in client.calls] == [
        "persisted-draft-key",
        "persisted-draft-key",
    ]
    assert len(session.messages) == 1


@pytest.mark.asyncio
async def test_web_send_key_cannot_be_rebound_to_another_draft():
    session = _session(_FakeRestClient())
    await session.send("first", 3, "one-key")

    with pytest.raises(ValueError, match="another draft"):
        await session.send("different", 3, "one-key")


@pytest.mark.asyncio
async def test_web_demo_revokes_its_ephemeral_device_after_definite_sends():
    client = _FakeRestClient()
    session = _session(client)
    await session.send("hello", 3, "one-key")

    await session.stop()

    assert client.revoked == ["web-client"]


@pytest.mark.asyncio
async def test_web_demo_keeps_device_as_fail_closed_marker_after_uncertain_send():
    client = _FakeRestClient(fail_once=True)
    session = _session(client)
    with pytest.raises(urllib.error.URLError):
        await session.send("hello", 3, "one-key")

    await session.stop()

    assert client.revoked == []


@pytest.mark.asyncio
async def test_web_demo_does_not_block_editing_after_a_definite_presend_rejection():
    client = _FakeRestClient()
    session = _session(client)

    def reject(*_args, **_kwargs):
        raise RestError(400, {"error": "text too long"}, "send")

    client.send_message = reject
    with pytest.raises(RestError):
        await session.send("hello", 3, "one-key")
    await session.stop()

    assert client.revoked == ["web-client"]


@pytest.mark.asyncio
async def test_web_preflight_value_error_does_not_leave_unresolved_send_marker():
    client = _FakeRestClient()
    session = _session(client)

    def reject(*_args, **_kwargs):
        raise ValueError("invalid idempotency key")

    client.send_message = reject
    with pytest.raises(ValueError, match="invalid idempotency key"):
        await session.send("hello", 3, "bad-key")

    assert session._unresolved_send_keys == set()


@pytest.mark.asyncio
async def test_web_startup_failure_runs_session_cleanup(tmp_path):
    client = _FakeRestClient()
    session = ChatSession(
        live=True,
        base_url=client.base_url,
        companion="field-radio",
        admin_token="admin",
    )

    with (
        patch.object(web_app, "CompanionRestClient", return_value=client),
        patch.object(session, "_pair", side_effect=RuntimeError("pair failed")),
    ):
        with pytest.raises(RuntimeError, match="pair failed"):
            await session.start(tmp_path)

    assert client.revoked == ["web-client"]
    assert session._stopped is True
    assert session.admin_token is None


def test_web_pairing_preflights_an_orphaned_stable_device_before_minting_a_code():
    client = _FakeRestClient()
    session = _session(client)

    class _Operator:
        @staticmethod
        def devices():
            return [{"device_id": "web-client"}]

    with patch.object(web_app, "CompanionRestClient", return_value=_Operator()):
        with pytest.raises(RuntimeError, match="already paired"):
            session._pair()

    assert client.calls == []


def test_web_message_events_keep_wire_direction_and_dedupe_by_message_id():
    session = _session(_FakeRestClient())
    event = {
        "type": "message",
        "data": {
            "id": 7,
            "text": "outbound",
            "direction": "out",
            "channel_idx": 3,
            "timestamp": 1,
            "state": "pending",
        },
    }

    session._apply_event(event)
    session._apply_event(event)

    assert len(session.messages) == 1
    assert session.messages[0]["direction"] == "out"


def test_web_send_state_event_updates_the_existing_message():
    session = _session(_FakeRestClient())
    session._apply_event(
        {
            "type": "message",
            "data": {
                "id": 7,
                "text": "outbound",
                "direction": "out",
                "channel_idx": 3,
                "timestamp": 1,
                "state": "pending",
            },
        }
    )

    session._apply_event(
        {
            "type": "message_send_state",
            "data": {
                "message_id": 7,
                "state": "confirmed",
                "packet_hash": "AABBCCDDEEFF0011",
            },
        }
    )

    assert session.messages[0]["state"] == "confirmed"
    assert session.messages[0]["packet_hash"] == "AABBCCDDEEFF0011"


@pytest.mark.asyncio
async def test_web_sync_drains_all_pages_without_waiting_for_the_next_poll():
    client = _FakeRestClient()
    session = _session(client)
    client.sync = MagicMock()
    client.sync.side_effect = [
        SyncResult(
            events=[
                {
                    "type": "message",
                    "data": {"id": 1, "text": "first", "channel_idx": 3},
                }
            ],
            next_cursor="epoch:1",
            has_more=True,
        ),
        SyncResult(
            events=[
                {
                    "type": "message",
                    "data": {"id": 2, "text": "second", "channel_idx": 3},
                }
            ],
            next_cursor="epoch:2",
            has_more=False,
        ),
    ]
    session.cursor = "epoch:0"
    await session._sync_to_head()

    assert [message["id"] for message in session.messages] == [1, 2]
    assert session.cursor == "epoch:2"
    assert [call.args[1] for call in client.sync.call_args_list] == [
        "epoch:0",
        "epoch:1",
    ]


def test_web_contact_and_prefs_events_update_resync_state():
    session = _session(_FakeRestClient())
    session.self_info = {"public_key": "11" * 32, "node_name": "old"}
    session.contacts = [{"public_key": "aa" * 32, "name": "Alice"}]
    queue = session.subscribe()

    session._apply_event(
        {
            "type": "contact",
            "data": {
                "public_key": "aa" * 32,
                "name": "Alice Updated",
                "change": "path",
                "out_path_len": 2,
            },
        }
    )
    session._apply_event(
        {
            "type": "contact",
            "data": {
                "public_key": "bb" * 32,
                "name": "Bob",
                "change": "new",
            },
        }
    )
    session._apply_event(
        {
            "type": "contact",
            "data": {"public_key": "aa" * 32, "change": "remove"},
        }
    )
    session._apply_event(
        {
            "type": "prefs",
            "data": {"node_name": "new", "path_hash_mode": 2},
        }
    )

    assert session.contacts == [
        {"public_key": "bb" * 32, "name": "Bob"}
    ]
    assert session.self_info["node_name"] == "new"
    assert session.self_info["path_hash_mode"] == 2
    queued = []
    while not queue.empty():
        queued.append(queue.get_nowait())
    state_events = [event for event in queued if event["kind"] == "state"]
    assert state_events[-1]["data"]["node_name"] == "new"
    assert state_events[-1]["data"]["contacts"] == session.contacts


def test_web_channel_update_keeps_one_entry_per_index():
    session = _session(_FakeRestClient())
    session.channels = [
        {"index": 3, "name": "#old"},
        {"index": 3, "name": "#stale-duplicate"},
    ]

    session._apply_channel_change(
        {"index": 3, "name": "#new", "change": "update"}
    )

    assert session.channels == [{"index": 3, "name": "#new"}]


def test_web_slow_subscriber_is_bounded_and_told_to_resync():
    session = _session(_FakeRestClient())
    queue = session.subscribe()

    for index in range(257):
        session.emit("message", {"id": index})

    assert queue.qsize() == 1
    assert queue.get_nowait()["kind"] == "resync"


def test_web_rendered_messages_and_send_result_cache_are_bounded():
    session = _session(_FakeRestClient())

    for index in range(web_app.MAX_RENDERED_MESSAGES + 1):
        session.messages.append({"id": index})
    session._trim_messages()

    for index in range(web_app.MAX_LOCAL_SEND_RESULTS + 1):
        session._remember_send_result(str(index), "text", 0, {"id": index})

    assert len(session.messages) == web_app.MAX_RENDERED_MESSAGES
    assert session.messages[0]["id"] == 1
    assert len(session._sent_entries) == web_app.MAX_LOCAL_SEND_RESULTS
    assert "0" not in session._sent_entries


def test_web_event_subscribers_are_bounded():
    session = _session(_FakeRestClient())

    for _ in range(web_app.MAX_EVENT_SUBSCRIBERS):
        session.subscribe()

    with pytest.raises(RuntimeError, match="too many local event streams"):
        session.subscribe()


@pytest.mark.asyncio
async def test_web_http_routes_bind_a_send_to_server_and_identity():
    if web_app.web is None:
        pytest.skip("aiohttp companion-web optional dependency is not installed")
    from aiohttp.test_utils import TestClient, TestServer

    session = _session(_FakeRestClient())
    session.self_info = {"public_key": "aa" * 32}
    browser = TestClient(TestServer(web_app.build_app(session)))
    await browser.start_server()
    try:
        page = await browser.get("/")
        assert page.status == 200
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
        assert page.headers["X-Frame-Options"] == "DENY"

        state = await browser.get("/api/state")
        assert state.status == 200
        assert state.headers["Cache-Control"] == "no-store"
        assert state.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in state.headers["Content-Security-Policy"]
        state_body = await state.json()
        assert state_body["companion_identity"] == "aa" * 32
        assert state_body["pairing_generation"] == session.pairing_generation

        stream = await browser.get("/api/events")
        initial = json.loads((await stream.content.readline()).decode().removeprefix("data: "))
        assert initial["kind"] == "state"
        assert initial["data"]["companion_identity"] == "aa" * 32
        stream.close()

        blocked = await browser.get(
            "/api/state",
            headers={"Origin": "https://evil.example"},
        )
        assert blocked.status == 403
        malformed_origin = await browser.get(
            "/api/state",
            headers={"Origin": f"{browser.make_url('/')!s}not-an-origin"},
        )
        assert malformed_origin.status == 403
        deeply_nested = await browser.post(
            "/api/send",
            data="[" * 2_000 + "0" + "]" * 2_000,
            headers={"Content-Type": "application/json"},
        )
        assert deeply_nested.status == 400
        wrong_scheme_origin = str(browser.make_url("/")).replace(
            "http://",
            "https://",
            1,
        ).rstrip("/")
        wrong_scheme = await browser.get(
            "/api/state",
            headers={"Origin": wrong_scheme_origin},
        )
        assert wrong_scheme.status == 403

        payload = {
            "companion": session.companion,
            "companion_identity": "aa" * 32,
            "api_base_url": session.client.base_url,
            "device_id": session.device_id,
            "pairing_generation": session.pairing_generation,
            "text": "hello",
            "channel": 3,
            "idempotency_key": "persisted-draft-key",
        }
        sent = await browser.post("/api/send", json=payload)
        assert sent.status == 200
        assert (await sent.json())["id"] == 41

        wrong_media_type = await browser.post(
            "/api/send",
            data=json.dumps(payload),
            headers={"Content-Type": "text/plain"},
        )
        assert wrong_media_type.status == 415
        duplicate_field = await browser.post(
            "/api/send",
            data='{"text":"first","text":"second"}',
            headers={"Content-Type": "application/json"},
        )
        assert duplicate_field.status == 400
        nonfinite_number = await browser.post(
            "/api/send",
            data='{"channel":NaN}',
            headers={"Content-Type": "application/json"},
        )
        assert nonfinite_number.status == 400
        overflowing_decimal = await browser.post(
            "/api/send",
            data='{"channel":1e999}',
            headers={"Content-Type": "application/json"},
        )
        assert overflowing_decimal.status == 400

        padded = await browser.post(
            "/api/send",
            json={**payload, "idempotency_key": " persisted-draft-key"},
        )
        assert padded.status == 400
        controlled = await browser.post(
            "/api/send",
            json={**payload, "idempotency_key": "persisted\tdraft-key"},
        )
        assert controlled.status == 400
        unicode_controlled = await browser.post(
            "/api/send",
            json={**payload, "idempotency_key": "persisted\u202edraft-key"},
        )
        assert unicode_controlled.status == 400
        printable_unicode = await browser.post(
            "/api/send",
            json={**payload, "idempotency_key": "persisted🚀draft-key"},
        )
        assert printable_unicode.status == 400
        invalid_unicode = await browser.post(
            "/api/send",
            json={**payload, "idempotency_key": "persisted\ud800draft-key"},
        )
        assert invalid_unicode.status == 400
        assert len(session.client.calls) == 1

        session.pairing_generation = "new-pairing-generation"
        stale_pairing = await browser.post("/api/send", json=payload)
        assert stale_pairing.status == 400
        assert len(session.client.calls) == 1
    finally:
        await browser.close()


def test_web_index_separates_safety_and_transient_status_and_bounds_push_dom():
    source = web_app.INDEX.read_text(encoding="utf-8")

    assert 'id="pendingStatus"' in source
    assert 'id="connectionStatus"' in source
    assert "stream.onopen = () => showConnectionStatus(\"\")" in source
    assert "try {\n      evt = JSON.parse(e.data);" in source
    assert "const MAX_RENDERED_PUSHES = 256;" in source
    assert "pushesEl.children.length > MAX_RENDERED_PUSHES" in source
    assert "code >= 0x21 && code <= 0x7e" in source
