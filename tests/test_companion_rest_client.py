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


# --- contact management ----------------------------------------------------
# Closes the gap inventoried in docs/architecture/companion-frame-vs-rest.md:
# before these, a REST client's contact list was read-only.


def journal_events(harness, event_type: str) -> list:
    return [
        e
        for e in harness.handler.companion_get_events(harness.companion_hash, 0)
        if e["event_type"] == event_type
    ]


def test_add_contact_appears_in_snapshot(paired, harness):
    pubkey = "b1" * 32
    result = paired.upsert_contact(harness.companion_name, pubkey, name="Bob", adv_type=1)
    assert result["contact"]["public_key"] == pubkey
    assert result["contact"]["name"] == "Bob"

    data, _etag = paired.snapshot(harness.companion_name)
    assert "Bob" in [c["name"] for c in data["contacts"]]


def test_add_contact_journals_a_contact_event(paired, harness):
    before = len(journal_events(harness, "contact"))
    paired.upsert_contact(harness.companion_name, "b2" * 32, name="Carol")
    events = journal_events(harness, "contact")
    assert len(events) == before + 1
    assert events[-1]["payload"]["change"] == "new"


def test_update_preserves_learned_routing_state(paired, harness):
    """out_path is learned from the mesh; renaming a contact must not erase it."""
    pubkey = "aa" * 32  # seeded contact "Alice"
    paired.upsert_contact(harness.companion_name, pubkey, name="Alice Renamed")

    contact = harness.bridge.contacts.get_by_key(bytes.fromhex(pubkey))
    assert contact.name == "Alice Renamed"
    assert contact.last_advert_timestamp == 123, "advert state must survive an update"


def test_delete_contact_removes_it(paired, harness):
    pubkey = "b3" * 32
    paired.upsert_contact(harness.companion_name, pubkey, name="Dave")
    assert paired.delete_contact(harness.companion_name, pubkey)["removed"] is True

    data, _etag = paired.snapshot(harness.companion_name)
    assert "Dave" not in [c["name"] for c in data["contacts"]]


def test_delete_contact_journals_a_removal(paired, harness):
    pubkey = "b4" * 32
    paired.upsert_contact(harness.companion_name, pubkey, name="Erin")
    before = len(journal_events(harness, "contact"))
    paired.delete_contact(harness.companion_name, pubkey)

    events = journal_events(harness, "contact")
    assert len(events) == before + 1
    assert events[-1]["payload"]["change"] == "removed"


def test_delete_unknown_contact_is_404(paired, harness):
    with pytest.raises(RestError) as excinfo:
        paired.delete_contact(harness.companion_name, "cc" * 32)
    assert excinfo.value.status == 404


def test_full_contact_store_is_507(paired, harness):
    """The frame protocol signals this with PUSH_CODE_CONTACTS_FULL.

    Restores the store afterwards: the harness is module-scoped, so leaving it
    full would fail whichever contact test happened to run next.
    """
    store = harness.bridge.contacts
    original_limit = store.max_contacts
    store.max_contacts = len(store.values()) + 2  # room for exactly two more

    added = []
    try:
        with pytest.raises(RestError) as excinfo:
            for i in range(6):
                pubkey = f"{0xD0 + i:02x}" * 32
                paired.upsert_contact(harness.companion_name, pubkey, name=f"n{i}")
                added.append(pubkey)
        assert excinfo.value.status == 507
        assert added, "should have added some before filling"
    finally:
        store.max_contacts = original_limit
        for pubkey in added:
            paired.delete_contact(harness.companion_name, pubkey)


def test_contact_endpoint_rejects_get(paired, harness):
    with pytest.raises(RestError) as excinfo:
        paired._data("GET", f"/companions/{harness.companion_name}/contacts/{'aa' * 32}")
    assert excinfo.value.status == 405


# --- channel management ----------------------------------------------------


def test_join_channel_with_a_psk(paired, harness):
    """Without this a REST-only client could not join a channel at all: the
    snapshot withholds secrets and there was no way to supply one."""
    result = paired.set_channel(harness.companion_name, 4, "#joined", bytes(range(16)))
    assert result["channel"] == {"index": 4, "name": "#joined"}

    data, _etag = paired.snapshot(harness.companion_name)
    assert {"index": 4, "name": "#joined"} in data["channels"]


def test_join_never_echoes_the_secret_back(paired, harness):
    secret = bytes([0x5A]) * 16
    result = paired.set_channel(harness.companion_name, 5, "#quiet", secret)
    assert secret.hex() not in str(result)

    data, _etag = paired.snapshot(harness.companion_name)
    assert secret.hex() not in str(data["channels"])
    for channel in data["channels"]:
        assert set(channel) == {"index", "name"}


def test_join_journals_a_channel_event(paired, harness):
    before = len(journal_events(harness, "channel"))
    paired.set_channel(harness.companion_name, 6, "#eventful", bytes(16))
    events = journal_events(harness, "channel")
    assert len(events) == before + 1
    assert events[-1]["payload"] == {"index": 6, "name": "#eventful", "change": "update"}


def test_channel_event_from_join_carries_no_secret(paired, harness):
    secret = bytes([0x7B]) * 32
    paired.set_channel(harness.companion_name, 7, "#nosecret", secret)
    events = journal_events(harness, "channel")
    assert secret.hex() not in str(events[-1]["payload"])
    assert set(events[-1]["payload"]) == {"index", "name", "change"}


@pytest.mark.parametrize("secret", [b"", bytes(8), bytes(20)])
def test_bad_secret_length_is_400(paired, harness, secret):
    with pytest.raises(RestError) as excinfo:
        paired.set_channel(harness.companion_name, 2, "#bad", secret)
    assert excinfo.value.status == 400


def test_non_hex_secret_is_400(paired, harness):
    with pytest.raises(RestError) as excinfo:
        paired._data(
            "PUT",
            f"/companions/{harness.companion_name}/channels/2",
            body={"name": "#bad", "secret": "not-hex-at-all"},
        )
    assert excinfo.value.status == 400


def test_join_without_name_is_400(paired, harness):
    """Clearing is DELETE, not an empty-name PUT."""
    with pytest.raises(RestError) as excinfo:
        paired._data(
            "PUT",
            f"/companions/{harness.companion_name}/channels/2",
            body={"name": "", "secret": bytes(16).hex()},
        )
    assert excinfo.value.status == 400


def test_channel_index_out_of_range_is_404(paired, harness):
    with pytest.raises(RestError) as excinfo:
        paired.set_channel(harness.companion_name, 999, "#nope", bytes(16))
    assert excinfo.value.status == 404


def test_non_integer_channel_index_is_400(paired, harness):
    with pytest.raises(RestError) as excinfo:
        paired._data(
            "PUT",
            f"/companions/{harness.companion_name}/channels/abc",
            body={"name": "#x", "secret": bytes(16).hex()},
        )
    assert excinfo.value.status == 400


def test_clear_channel_removes_it_and_journals(paired, harness):
    paired.set_channel(harness.companion_name, 3, "#temporary", bytes(16))
    before = len(journal_events(harness, "channel"))

    assert paired.clear_channel(harness.companion_name, 3)["removed"] is True
    data, _etag = paired.snapshot(harness.companion_name)
    assert 3 not in [c["index"] for c in data["channels"]]

    events = journal_events(harness, "channel")
    assert len(events) == before + 1
    assert events[-1]["payload"] == {"index": 3, "name": None, "change": "remove"}


def test_clear_unconfigured_channel_is_404(paired, harness):
    with pytest.raises(RestError) as excinfo:
        paired.clear_channel(harness.companion_name, 2)
    assert excinfo.value.status == 404


# --- favourites ------------------------------------------------------------
# Favourites are flags bit 0. They are protected from forced-trim eviction
# (companion/utils.trim_contacts), so getting this wrong loses contacts a user
# deliberately kept. Live data uses other bits too (flags 129 and 145 both
# occur), which is why `favorite` is a server-side read-modify-write rather
# than something the client computes.


def test_contacts_expose_a_favorite_field(paired, harness):
    pubkey = "f1" * 32
    result = paired.upsert_contact(harness.companion_name, pubkey, name="Fav", favorite=True)
    assert result["contact"]["favorite"] is True
    assert result["contact"]["flags"] & 0x01

    data, _etag = paired.snapshot(harness.companion_name)
    entry = next(c for c in data["contacts"] if c["public_key"] == pubkey)
    assert entry["favorite"] is True


def test_favorite_defaults_false(paired, harness):
    result = paired.upsert_contact(harness.companion_name, "f2" * 32, name="Plain")
    assert result["contact"]["favorite"] is False


def test_setting_favorite_preserves_other_flag_bits(paired, harness):
    """Live contacts carry flags like 129 (0x81) and 145 (0x91). Favouriting
    must not clobber the high bits."""
    pubkey = "f3" * 32
    paired.upsert_contact(harness.companion_name, pubkey, name="Flagged", flags=0x90)
    result = paired.set_favorite(harness.companion_name, pubkey, True)

    assert result["contact"]["flags"] == 0x91
    assert result["contact"]["favorite"] is True


def test_unfavoriting_preserves_other_flag_bits(paired, harness):
    pubkey = "f4" * 32
    paired.upsert_contact(harness.companion_name, pubkey, name="Flagged2", flags=0x91)
    result = paired.set_favorite(harness.companion_name, pubkey, False)

    assert result["contact"]["flags"] == 0x90
    assert result["contact"]["favorite"] is False


def test_favorite_survives_an_unrelated_update(paired, harness):
    """Renaming a favourite must not silently unfavourite it."""
    pubkey = "f5" * 32
    paired.set_favorite(harness.companion_name, pubkey, True)
    result = paired.upsert_contact(harness.companion_name, pubkey, name="Renamed")

    assert result["contact"]["name"] == "Renamed"
    assert result["contact"]["favorite"] is True


def test_favorite_beats_raw_flags_when_both_sent(paired, harness):
    """`favorite` is the more specific instruction, so it wins."""
    pubkey = "f6" * 32
    result = paired.upsert_contact(
        harness.companion_name, pubkey, name="Both", flags=0x00, favorite=True
    )
    assert result["contact"]["favorite"] is True


def test_clear_does_not_leave_an_empty_named_channel(paired, harness):
    """Regression: DELETE used to call set_channel(idx, "") which creates an
    empty-named channel still occupying the slot. Found on a live repeater,
    where the "cleared" channel reappeared in the snapshot with a blank name.
    """
    paired.set_channel(harness.companion_name, 5, "#willclear", bytes(16))
    paired.clear_channel(harness.companion_name, 5)

    data, _etag = paired.snapshot(harness.companion_name)
    assert 5 not in [c["index"] for c in data["channels"]]
    assert "" not in [c["name"] for c in data["channels"]]
    assert harness.bridge.get_channel(5) is None
