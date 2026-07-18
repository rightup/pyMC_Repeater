"""End-to-end tests for the Mobile Companion API v1 over real HTTP.

The existing endpoint tests (tests/test_mobile_endpoints.py and friends) call
handlers through ``__wrapped__``, bypassing ``require_auth`` and CherryPy. That
covers handler logic well but leaves the surface a phone app actually meets
untested: HTTP routing, the auth gate, the pairing-code exchange, bearer
tokens, scope enforcement, ETag/304 round trips, and the JSON envelope.

These drive ``companion_client.rest`` against a real CherryPy mount with a real
SQLiteHandler, real token manager and real JWT handler. Only the bridge is a
double.

CherryPy is process-global, so the harness is module-scoped and each test gets
its own companion/device names rather than a fresh server.
"""

from __future__ import annotations

import pytest

from companion_client.rest import CompanionRestClient, NotModified, RestError
from companion_client.rest_simulator import start_rest_harness, stop_rest_harness


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    served = start_rest_harness(tmp_path_factory.mktemp("rest"))
    try:
        yield served
    finally:
        stop_rest_harness(served)


@pytest.fixture
def anon(harness) -> CompanionRestClient:
    return CompanionRestClient(harness.base_url)


@pytest.fixture(scope="module")
def paired(harness) -> CompanionRestClient:
    """One paired client shared by the suite.

    Module-scoped on purpose: POST /pair is rate-limited to
    ``_RATE_LIMIT_MAX`` (10) attempts per 60s window with a single global
    counter (design doc 11.3), so pairing per-test trips 429 partway through
    the run. That limit is a deliberate anti-guessing control -- the tests
    work within it rather than around it.
    """
    client = CompanionRestClient(harness.base_url)
    code = client.pair_start(harness.companion_name, harness.admin_token())["code"]
    device_id = "dev-shared"
    client.pair(code, device_id, "Test Phone", platform="ios")
    client.device_id = device_id
    return client


# --- unauthenticated surface ----------------------------------------------


def test_server_info_is_public(anon):
    info = anon.server_info()
    assert "v1" in info["api_versions"]


def test_server_info_does_not_leak_companion_names(anon, harness):
    # Deliberate: companion names require auth (openapi note on /v1/server_info).
    assert harness.companion_name not in str(anon.server_info())


def test_snapshot_requires_auth(anon, harness):
    with pytest.raises(RestError) as excinfo:
        anon.snapshot(harness.companion_name)
    assert excinfo.value.status == 401


def test_sync_requires_auth(anon, harness):
    with pytest.raises(RestError) as excinfo:
        anon.sync(harness.companion_name, "0")
    assert excinfo.value.status == 401


def test_bad_token_is_rejected(harness):
    client = CompanionRestClient(harness.base_url, token="not-a-real-token")
    with pytest.raises(RestError) as excinfo:
        client.snapshot(harness.companion_name)
    assert excinfo.value.status == 401


# --- pairing ---------------------------------------------------------------


def test_pair_start_requires_admin(anon, harness):
    """A device cannot bootstrap itself: minting a code needs an operator."""
    with pytest.raises(RestError) as excinfo:
        anon._data("POST", "/pair/start", body={"companion_name": harness.companion_name})
    assert excinfo.value.status == 401


def test_pair_start_returns_a_short_lived_code(anon, harness):
    started = anon.pair_start(harness.companion_name, harness.admin_token())
    assert started["companion_name"] == harness.companion_name
    assert started["expires_in"] > 0
    assert started["code"]


def test_pair_exchanges_code_for_a_device_token(anon, harness):
    code = anon.pair_start(harness.companion_name, harness.admin_token())["code"]
    result = anon.pair(code, "dev-exchange", "Phone", platform="ios")
    assert result["token"]
    assert result["companion_name"] == harness.companion_name
    assert anon.token == result["token"]


def test_pairing_code_is_single_use(anon, harness):
    code = anon.pair_start(harness.companion_name, harness.admin_token())["code"]
    anon.pair(code, "dev-first", "First")
    with pytest.raises(RestError) as excinfo:
        CompanionRestClient(harness.base_url).pair(code, "dev-second", "Second")
    assert excinfo.value.status in (400, 404)


def test_bad_pairing_code_rejected(anon):
    with pytest.raises(RestError) as excinfo:
        anon.pair("0" * 32, "dev-bad", "Phone")
    assert excinfo.value.status in (400, 404)


def test_post_without_trailing_slash_still_works(anon, harness):
    """CherryPy 301-redirects /pair to /pair/, and stock HTTP clients downgrade
    POST to GET when following it -- yielding a misleading
    '405 Method not allowed. Use POST.' The client preserves the method."""
    code = anon.pair_start(harness.companion_name, harness.admin_token())["code"]
    assert anon.pair(code, "dev-noslash", "Phone")["token"]


# --- snapshot --------------------------------------------------------------


def test_snapshot_carries_contacts_and_channels(paired, harness):
    """There is no dedicated list endpoint for either -- snapshot is how a
    client learns them."""
    data, _etag = paired.snapshot(harness.companion_name)
    assert [c["name"] for c in data["contacts"]] == ["Alice"]
    assert {c["index"]: c["name"] for c in data["channels"]} == {0: "Public", 1: "#howltest"}


def test_snapshot_channels_never_expose_psk_secrets(paired, harness):
    data, _etag = paired.snapshot(harness.companion_name)
    for channel in data["channels"]:
        assert set(channel) == {"index", "name"}
    assert "secret" not in str(data["channels"])


def test_snapshot_has_cursor_and_epoch(paired, harness):
    data, _etag = paired.snapshot(harness.companion_name)
    assert data["journal_epoch"]
    assert data["cursor"] is not None


def test_snapshot_etag_replay_gives_304(paired, harness):
    _data, etag = paired.snapshot(harness.companion_name)
    assert etag, "server must send an ETag for conditional requests"
    with pytest.raises(NotModified):
        paired.snapshot(harness.companion_name, etag=etag)


def test_unknown_companion_is_404(paired):
    with pytest.raises(RestError) as excinfo:
        paired.snapshot("no-such-companion")
    assert excinfo.value.status == 404


# --- sync ------------------------------------------------------------------


def test_sync_from_snapshot_cursor_is_empty_when_idle(paired, harness):
    data, _etag = paired.snapshot(harness.companion_name)
    result = paired.sync(harness.companion_name, data["cursor"])
    assert result.events == []
    assert result.has_more is False


def test_sync_delivers_new_events(paired, harness):
    data, _etag = paired.snapshot(harness.companion_name)
    harness.handler.companion_push_message(
        harness.companion_hash, {"text": "hi", "timestamp": 1, "packet_hash": "rest-1"}
    )
    from repeater.companion.journal import CompanionEventJournal

    journal = CompanionEventJournal(harness.handler, harness.companion_hash)
    journal.record_message({"text": "hi", "timestamp": 1, "packet_hash": "rest-1"})

    result = paired.sync(harness.companion_name, data["cursor"])
    assert [e["type"] for e in result.events if e.get("type")] or result.events
    assert int(result.next_cursor) > int(data["cursor"])


def test_channel_event_reaches_a_syncing_client(paired, harness):
    """Regression cover for the gap fixed 2026-07-18: channel changes were
    never journaled, so a synced client could not learn about a channel being
    added or renamed without re-snapshotting."""
    from repeater.companion.journal import CompanionEventJournal

    data, _etag = paired.snapshot(harness.companion_name)
    journal = CompanionEventJournal(harness.handler, harness.companion_hash)
    journal.record_channel(3, "#brand-new", "update")

    result = paired.sync(harness.companion_name, data["cursor"])
    channel_events = [e for e in result.events if e.get("type") == "channel"]
    assert channel_events, f"no channel event in {result.events}"
    payload = channel_events[-1].get("data", channel_events[-1])
    assert payload["index"] == 3
    assert payload["name"] == "#brand-new"
    assert payload["change"] == "update"


def test_synced_channel_event_carries_only_index_name_change(paired, harness):
    """The journal reaches mobile clients through /sync, so a PSK leaked into a
    channel event would reach every synced device -- and unlike a snapshot
    field, it would persist in the journal table."""
    from repeater.companion.journal import CompanionEventJournal

    data, _etag = paired.snapshot(harness.companion_name)
    journal = CompanionEventJournal(harness.handler, harness.companion_hash)
    journal.record_channel(4, "#quiet", "update")

    result = paired.sync(harness.companion_name, data["cursor"])
    events = [e for e in result.events if e.get("type") == "channel"]
    assert events
    payload = events[-1].get("data", events[-1])
    assert set(payload) == {"index", "name", "change"}


def test_bad_cursor_is_rejected(paired, harness):
    with pytest.raises(RestError) as excinfo:
        paired.sync(harness.companion_name, "not-a-number")
    assert excinfo.value.status == 400


# --- push registration -----------------------------------------------------


def test_device_token_cannot_list_devices(paired):
    """GET /devices is admin-scoped: a device token must not enumerate the
    fleet, only manage its own push registration."""
    with pytest.raises(RestError) as excinfo:
        paired.devices()
    assert excinfo.value.status == 403


def test_register_and_unregister_push(paired, harness):
    result = paired.register_push(
        paired.device_id,
        push_token="a" * 64,
        push_relay_url="https://relay.example/notify",
        push_detail="count",
    )
    assert result is not None

    def row():
        # Listing is admin-scoped, so verify through an operator client.
        admin = CompanionRestClient(harness.base_url, token=harness.admin_token())
        return next(d for d in admin.devices() if d["device_id"] == paired.device_id)

    assert row()["push_detail"] == "count"

    paired.unregister_push(paired.device_id)
    # DELETE clears the token but keeps detail/relay (documented behaviour).
    assert not row().get("push_token")


def test_register_push_rejects_bad_detail(paired):
    with pytest.raises(RestError) as excinfo:
        paired.register_push(
            paired.device_id,
            push_token="a" * 64,
            push_relay_url="https://relay.example/notify",
            push_detail="not-a-mode",
        )
    assert excinfo.value.status == 400


def test_register_push_rejects_non_http_relay(paired):
    with pytest.raises(RestError) as excinfo:
        paired.register_push(
            paired.device_id,
            push_token="a" * 64,
            push_relay_url="ftp://relay.example/notify",
        )
    assert excinfo.value.status == 400


def test_device_cannot_register_push_for_another_device(paired):
    """A scoped device token must not touch another device's row."""
    with pytest.raises(RestError) as excinfo:
        paired.register_push(
            "somebody-elses-device",
            push_token="b" * 64,
            push_relay_url="https://relay.example/notify",
        )
    assert excinfo.value.status in (403, 404)


# --- sending (POST /companions/{name}/messages) ----------------------------


def test_send_channel_message_reaches_the_bridge(paired, harness):
    before = len(harness.bridge.sent)
    result = paired.send_message(harness.companion_name, "hello over REST", channel_idx=0)
    assert result["sent"] is True
    assert harness.bridge.sent[before:] == [{"channel_idx": 0, "text": "hello over REST"}]


def test_send_requires_exactly_one_target(paired, harness):
    """'to' and 'channel_idx' are mutually exclusive; the client refuses before
    the round trip rather than letting the server 400."""
    with pytest.raises(ValueError):
        paired.send_message(harness.companion_name, "x")
    with pytest.raises(ValueError):
        paired.send_message(harness.companion_name, "x", channel_idx=0, to="aa" * 32)


def test_send_requires_idempotency_key(paired, harness):
    """The header is mandatory (design doc 6); omitting it is a 400."""
    with pytest.raises(RestError) as excinfo:
        paired._data(
            "POST",
            f"/companions/{harness.companion_name}/messages",
            body={"text": "no key", "channel_idx": 0},
        )
    assert excinfo.value.status == 400


def test_retry_with_same_key_replays_without_touching_the_radio(paired, harness):
    key = "replay-key-1"
    first = paired.send_message(harness.companion_name, "once", channel_idx=0, idempotency_key=key)
    before = len(harness.bridge.sent)
    second = paired.send_message(harness.companion_name, "once", channel_idx=0, idempotency_key=key)

    assert second == first
    assert len(harness.bridge.sent) == before, "replay must not re-send over the radio"


def test_same_key_different_body_is_409(paired, harness):
    key = "conflict-key-1"
    paired.send_message(harness.companion_name, "original", channel_idx=0, idempotency_key=key)
    with pytest.raises(RestError) as excinfo:
        paired.send_message(harness.companion_name, "changed", channel_idx=0, idempotency_key=key)
    assert excinfo.value.status == 409


def test_empty_text_is_rejected(paired, harness):
    with pytest.raises(RestError) as excinfo:
        paired.send_message(harness.companion_name, "", channel_idx=0)
    assert excinfo.value.status == 400
