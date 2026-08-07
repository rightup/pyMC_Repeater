import hashlib
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

import companion_client.rest as rest_module
from companion_client.rest import CompanionRestClient, RestError, SyncResult


def _sync_data(**overrides):
    data = {
        "journal_epoch": "abc123",
        "events": [],
        "next_cursor": "abc123:0",
        "has_more": False,
        "snapshot_required": False,
    }
    data.update(overrides)
    return data


def _public_prefs(**overrides):
    prefs = {
        "node_name": "Field Radio",
        "adv_type": 1,
        "latitude": 47.6,
        "longitude": -122.3,
        "autoadd_config": 0,
        "autoadd_max_hops": 0,
        "path_hash_mode": 0,
        "rx_delay_base": 0.0,
        "airtime_factor": 1.0,
        "client_repeat": 0,
        "manual_add_contacts": 0,
        "telemetry_mode_base": 0,
        "telemetry_mode_location": 0,
        "telemetry_mode_environment": 0,
        "advert_loc_policy": 0,
        "multi_acks": 0,
        "default_scope_name": "",
    }
    prefs.update(overrides)
    return prefs


def _message_data(**overrides):
    message = {
        "id": 1,
        "companion_hash": "0xaa",
        "sender_key": "",
        "recipient_key": "",
        "sender_prefix": "",
        "txt_type": 0,
        "timestamp": 1,
        "text": "hello",
        "is_channel": False,
        "channel_idx": 0,
        "path_len": 0,
        "snr": 0.0,
        "rssi": 0,
        "channel_data_type": 0,
        "channel_data_payload": "",
        "packet_hash": None,
        "created_at": 1.0,
        "observation_count": 1,
        "unique_path_count": 1,
        "direction": "in",
        "state": "received",
        "expected_ack": None,
        "source": "radio",
    }
    message.update(overrides)
    return message


def _snapshot_data(**overrides):
    data = {
        "journal_epoch": "abc123",
        "cursor": "abc123:0",
        "self": {"public_key": "11" * 32, **_public_prefs()},
        "contacts": [
            {
                "public_key": "aa" * 32,
                "name": "Alice",
                "adv_type": 1,
                "flags": 0,
                "favorite": False,
                "out_path_len": -1,
                "last_advert_timestamp": 1,
                "lastmod": 1,
                "gps_lat": 0.0,
                "gps_lon": 0.0,
            }
        ],
        "channels": [{"index": 0, "name": "Public"}],
        "messages": [],
        "server": {"version": "test"},
    }
    data.update(overrides)
    return data


def _self_info_without(field):
    self_info = {"public_key": "11" * 32, **_public_prefs()}
    self_info.pop(field)
    return self_info


def test_events_does_not_open_socket_until_iteration():
    client = CompanionRestClient("http://127.0.0.1:8000", token="secret")

    with patch("companion_client.rest._opener.open") as open_response:
        stream = client.events("field-radio")
        open_response.assert_not_called()
        stream.close()
        open_response.assert_not_called()


@pytest.mark.parametrize(
    "timeout",
    [
        False,
        True,
        0,
        -1,
        "not-a-number",
        None,
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_client_rejects_nonpositive_or_nonfinite_timeout(timeout):
    with pytest.raises(ValueError, match="finite number greater than zero"):
        CompanionRestClient("http://127.0.0.1:8000", timeout=timeout)


@pytest.mark.parametrize(
    "payload",
    [
        (
            lambda client: client.companions(),
            [
                {
                    "name": "field-radio",
                    "companion_hash": "0xaa",
                    "node_name": "Field Radio",
                    "public_key": "aa" * 32,
                }
            ],
            "GET",
            "/companions",
        ),
        None,
        {},
        {"success": True},
        {"success": False, "data": {}},
        {"success": 1, "data": {}},
    ],
)
def test_data_requires_explicit_success_envelope(payload):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError) as excinfo:
            client.server_info()

    assert excinfo.value.status == 502


@pytest.mark.parametrize(
    "entry",
    [
        {
            "name": "field-radio",
            "companion_hash": "0xaa",
            "node_name": "Field Radio",
            "public_key": "aa" * 32,
        },
        {
            "name": "field-radio",
            "companion_hash": "0xaa",
            "node_name": "Field Radio",
            "public_key": "aa" * 32,
            "capabilities": {},
        },
        {
            "name": "field-radio",
            "companion_hash": "0xaa",
            "node_name": "Field Radio",
            "public_key": "aa" * 32,
            "capabilities": {"max_channels": -1},
        },
        {
            "name": "field-radio",
            "companion_hash": "0xaa",
            "node_name": "Field Radio",
            "public_key": "aa" * 32,
            "capabilities": {"max_channels": True},
        },
    ],
)
def test_companions_requires_valid_channel_capacity(entry):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_list_data", return_value=[entry]):
        with pytest.raises(RestError, match="companion entry"):
            client.companions()


@pytest.mark.parametrize(
    "response",
    [
        {
            "site_name": "Field Repeater",
            "api_versions": ["v1"],
            "auth_modes": ["jwt", "api_token"],
            "transport": {
                "scheme": "http",
                "secure": True,
                "trusted_network_required": True,
            },
            "server": {"version": "1.0", "time": 1.0},
        },
        {
            "site_name": "Field Repeater",
            "api_versions": ["v1"],
            "auth_modes": ["jwt", "api_token"],
            "transport": {
                "scheme": "https",
                "secure": True,
                "trusted_network_required": False,
            },
            "server": {"version": "1.0", "time": True},
        },
        {
            "site_name": "Field Repeater",
            "api_versions": ["v1"],
            "auth_modes": ["jwt", "api_token"],
            "transport": {
                "scheme": "https",
                "secure": True,
                "trusted_network_required": False,
            },
            "server": {"version": "1.0", "time": 10**1000},
        },
    ],
)
def test_server_info_rejects_ambiguous_transport_or_time(response):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_data", return_value=response):
        with pytest.raises(RestError, match="server_info response"):
            client.server_info()


def test_http_error_exposes_case_insensitive_retry_metadata():
    client = CompanionRestClient("http://127.0.0.1:8000")
    body = io.BytesIO(json.dumps({"success": False, "error": "rate limited"}).encode("utf-8"))
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8000/api/v1/companions",
        429,
        "Too Many Requests",
        {"Retry-After": "7"},
        body,
    )

    with patch("companion_client.rest._opener.open", side_effect=error):
        with pytest.raises(RestError) as excinfo:
            client.companions()

    assert excinfo.value.status == 429
    assert excinfo.value.headers["retry-after"] == "7"
    assert excinfo.value.headers.get("Retry-After") == "7"
    assert body.closed


@pytest.mark.parametrize(
    "base_url",
    [
        "http://:8000",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://@127.0.0.1:8000",
        "http://:@127.0.0.1:8000",
    ],
)
def test_client_rejects_invalid_base_url_authority(base_url):
    with pytest.raises(ValueError, match="base_url"):
        CompanionRestClient(base_url)


@pytest.mark.parametrize(
    "token",
    [
        "two words",
        "two\twords",
        "line\nbreak",
        "control\x00byte",
        "unicode-\N{SNOWMAN}",
        "abc=def",
        "a" * 4097,
    ],
)
def test_bearer_token_rejects_whitespace_controls_and_non_token_characters(token):
    client = CompanionRestClient("http://127.0.0.1:8000", token=token)

    with patch("companion_client.rest._opener.open") as open_response:
        with pytest.raises(ValueError, match="no whitespace or controls"):
            client.companions()

    open_response.assert_not_called()


@pytest.mark.parametrize("method", ["snapshot", "sync"])
def test_snapshot_and_sync_require_success_envelopes(method):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_request", return_value=(200, {"data": {}}, {})):
        with pytest.raises(RestError) as excinfo:
            if method == "snapshot":
                client.snapshot("field-radio")
            else:
                client.sync("field-radio", "abcd:0")

    assert excinfo.value.status == 502


@pytest.mark.parametrize("method", ["snapshot", "sync"])
def test_snapshot_and_sync_require_object_data(method):
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {"success": True, "data": []}

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError) as excinfo:
            if method == "snapshot":
                client.snapshot("field-radio")
            else:
                client.sync("field-radio", "abcd:0")

    assert excinfo.value.status == 502


def test_snapshot_accepts_complete_public_preferences():
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {"success": True, "data": _snapshot_data()}

    with patch.object(client, "_request", return_value=(200, payload, {"ETag": '"v1"'})):
        snapshot, etag = client.snapshot("field-radio")

    assert snapshot["self"]["path_hash_mode"] == 0
    assert etag == '"v1"'


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "contacts": [
                    {
                        **_snapshot_data()["contacts"][0],
                        "private": "must-not-cross-the-wire",
                    }
                ]
            },
            "contact",
        ),
        (
            {
                "channels": [
                    {
                        "index": 0,
                        "name": "Public",
                        "secret": "must-not-cross-the-wire",
                    }
                ]
            },
            "channel",
        ),
        (
            {"messages": [_message_data(private="must-not-cross-the-wire")]},
            "message",
        ),
        (
            {"messages": [_message_data(id=0)]},
            "message event id",
        ),
    ],
)
def test_snapshot_rejects_noncanonical_public_collection_items(overrides, message):
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {"success": True, "data": _snapshot_data(**overrides)}

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError, match=message):
            client.snapshot("field-radio")


@pytest.mark.parametrize(
    "cursor",
    [
        "abc123:" + ("9" * 129),
        "ABC:0",
        "\N{SNOWMAN}:0",
    ],
)
def test_snapshot_rejects_unbounded_or_noncanonical_cursor(cursor):
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {
        "success": True,
        "data": _snapshot_data(cursor=cursor),
    }

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError, match="invalid cursor"):
            client.snapshot("field-radio")


@pytest.mark.parametrize(
    "self_info",
    [
        {"public_key": "11" * 32, **_public_prefs()} | {"path_hash_mode": True},
        {"public_key": "11" * 32, **_public_prefs()} | {"latitude": float("nan")},
        {"public_key": "11" * 32, **_public_prefs()} | {"latitude": 10**1000},
        _self_info_without("autoadd_config"),
    ],
)
def test_snapshot_rejects_incomplete_or_invalid_public_preferences(self_info):
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {
        "success": True,
        "data": _snapshot_data(self=self_info),
    }

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError, match="public preference"):
            client.snapshot("field-radio")


@pytest.mark.parametrize(
    "event",
    [
        {
            "seq": 1,
            "type": "prefs",
            "ts": 1.0,
            "packet_hash": None,
            "data": {"path_hash_mode": False},
        },
        {
            "seq": 1,
            "type": "contact",
            "ts": 1.0,
            "packet_hash": None,
            "data": {"public_key": "not-hex", "change": "update"},
        },
        {
            "seq": 1,
            "type": "message",
            "ts": 10**1000,
            "packet_hash": None,
            "data": {},
        },
        {
            "seq": 10**1000,
            "type": "message",
            "ts": 1.0,
            "packet_hash": None,
            "data": {},
        },
        {
            "seq": 1,
            "type": "message\ndata: injected",
            "ts": 1.0,
            "packet_hash": None,
            "data": {},
        },
        {
            "seq": 1,
            "type": "message",
            "ts": 1.0,
            "packet_hash": None,
            "data": {"private": "not part of the mobile contract"},
        },
        {
            "seq": 1,
            "type": "message_reception",
            "ts": 1.0,
            "packet_hash": "1111111111111111",
            "data": {
                "message_id": 1,
                "packet_hash": "1111111111111111",
            },
        },
        {
            "seq": 1,
            "type": "snapshot_required",
            "ts": 1.0,
            "packet_hash": None,
            "data": {},
        },
    ],
)
def test_sync_rejects_invalid_public_state_events(event):
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {
        "success": True,
        "data": _sync_data(events=[event], next_cursor="abc123:1"),
    }

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError):
            client.sync("field-radio", "abc123:0")


def test_sync_accepts_unknown_safe_event_type_and_payload():
    client = CompanionRestClient("http://127.0.0.1:8000")
    event = {
        "seq": 1,
        "type": "future.event-v2",
        "ts": 1.0,
        "packet_hash": None,
        "data": {"new_public_field": True},
    }
    payload = {
        "success": True,
        "data": _sync_data(events=[event], next_cursor="abc123:1"),
    }

    with patch.object(client, "_request", return_value=(200, payload, {})):
        result = client.sync("field-radio", "abc123:0")

    assert result.events == [event]


def test_sync_accepts_complete_contact_event():
    client = CompanionRestClient("http://127.0.0.1:8000")
    contact = {**_snapshot_data()["contacts"][0], "change": "update"}
    event = {
        "seq": 1,
        "type": "contact",
        "ts": 1.0,
        "packet_hash": None,
        "data": contact,
    }
    payload = {
        "success": True,
        "data": _sync_data(events=[event], next_cursor="abc123:1"),
    }

    with patch.object(client, "_request", return_value=(200, payload, {})):
        result = client.sync("field-radio", "abc123:0")

    assert result.events == [event]


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        (
            "message",
            _message_data(packet_hash="2222222222222222"),
        ),
        (
            "message_reception",
            {
                "message_id": 1,
                "packet_hash": "2222222222222222",
                "path": ["01"],
                "rssi": -80,
                "snr": 2.0,
                "observed_at": 1.0,
                "observation_count": 1,
                "unique_path_count": 1,
            },
        ),
        (
            "message_send_state",
            {
                "message_id": 1,
                "state": "transmitted",
                "packet_hash": "2222222222222222",
                "expected_ack": None,
            },
        ),
        (
            "rf_reception",
            {
                "packet_hash": "2222222222222222",
                "path": ["01"],
                "rssi": -80,
                "snr": 2.0,
                "observed_at": 1.0,
            },
        ),
    ],
)
def test_sync_rejects_mismatched_event_and_payload_packet_hashes(event_type, data):
    client = CompanionRestClient("http://127.0.0.1:8000")
    event = {
        "seq": 1,
        "type": event_type,
        "ts": 1.0,
        "packet_hash": "1111111111111111",
        "data": data,
    }
    payload = {
        "success": True,
        "data": _sync_data(events=[event], next_cursor="abc123:1"),
    }

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError, match="does not match"):
            client.sync("field-radio", "abc123:0")


def test_sync_accepts_matching_null_send_state_packet_hashes():
    client = CompanionRestClient("http://127.0.0.1:8000")
    event = {
        "seq": 1,
        "type": "message_send_state",
        "ts": 1.0,
        "packet_hash": None,
        "data": {
            "message_id": 1,
            "state": "pending",
            "packet_hash": None,
            "expected_ack": None,
        },
    }
    payload = {
        "success": True,
        "data": _sync_data(events=[event], next_cursor="abc123:1"),
    }

    with patch.object(client, "_request", return_value=(200, payload, {})):
        result = client.sync("field-radio", "abc123:0")

    assert result.events == [event]


@pytest.mark.parametrize(
    "data",
    [
        _sync_data(has_more="false"),
        _sync_data(snapshot_required=0),
        _sync_data(next_cursor=None),
        _sync_data(next_cursor="abc123:" + ("9" * 129)),
        _sync_data(next_cursor="ABC:0"),
        _sync_data(journal_epoch="ABC"),
        _sync_data(events={}),
        _sync_data(
            events=[
                {
                    "seq": 2,
                    "type": "message",
                    "ts": 1.0,
                    "packet_hash": None,
                    "data": _message_data(id=2),
                },
                {
                    "seq": 1,
                    "type": "message",
                    "ts": 2.0,
                    "packet_hash": None,
                    "data": _message_data(id=1),
                },
            ],
            next_cursor="abc123:2",
        ),
        _sync_data(has_more=True),
    ],
)
def test_sync_rejects_malformed_control_flow_data(data):
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {"success": True, "data": data}

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError) as excinfo:
            client.sync("field-radio", "abc123:0")

    assert excinfo.value.status == 502


def test_sync_does_not_coerce_false_strings_to_true():
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {
        "success": True,
        "data": _sync_data(has_more="false", snapshot_required="false"),
    }

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError):
            client.sync("field-radio", "abc123:0")


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        " ABC:0",
        "ABC:0",
        "abc:+1",
        "abc:" + ("9" * 129),
        "\N{SNOWMAN}:0",
    ],
)
def test_sync_rejects_invalid_requested_cursor_before_network_io(cursor):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_request") as request:
        with pytest.raises(ValueError, match="cursor must be"):
            client.sync("field-radio", cursor)

    request.assert_not_called()


def test_sync_accepts_bounded_legacy_decimal_cursor():
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {"success": True, "data": _sync_data(next_cursor="abc123:1")}

    with patch.object(client, "_request", return_value=(200, payload, {})):
        result = client.sync("field-radio", "0")

    assert result.next_cursor == "abc123:1"


@pytest.mark.parametrize(
    "include",
    ["", "bogus", "rf_receptions,", ",rf_receptions", "bogus,rf_receptions"],
)
def test_sync_rejects_unknown_or_blank_include_before_network_io(include):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_request") as request:
        with pytest.raises(ValueError, match="only rf_receptions"):
            client.sync("field-radio", "abc123:0", include=include)

    request.assert_not_called()


def test_sync_rejects_an_event_at_or_before_the_requested_cursor():
    client = CompanionRestClient("http://127.0.0.1:8000")
    payload = {
        "success": True,
        "data": _sync_data(
            events=[
                {
                    "seq": 1,
                    "type": "message",
                    "ts": 1.0,
                    "packet_hash": None,
                    "data": _message_data(),
                }
            ],
            next_cursor="abc123:1",
        ),
    }

    with patch.object(client, "_request", return_value=(200, payload, {})):
        with pytest.raises(RestError, match="at or before"):
            client.sync("field-radio", "abc123:1")


def test_pair_requires_trusted_expected_fingerprint():
    client = CompanionRestClient("http://127.0.0.1:8000")

    with pytest.raises(TypeError):
        client.pair("code", "device", "Phone")


def test_pair_start_validates_the_trusted_identity_document():
    client = CompanionRestClient("http://127.0.0.1:8000")
    identity = bytes(range(32))
    response = {
        "code": "a" * 32,
        "expires_in": 300,
        "companion_name": "field-radio",
        "companion_identity": identity.hex(),
        "fingerprint": "0" * 64,
    }

    with patch.object(client, "_data", return_value=response):
        with pytest.raises(RestError, match="fingerprint"):
            client.pair_start("field-radio", "admin-token")


def test_pair_rejects_wrong_echoed_device_without_adopting_token():
    client = CompanionRestClient("http://127.0.0.1:8000", token="existing")
    identity = bytes(range(32))
    fingerprint = hashlib.sha256(identity).hexdigest()
    response = {
        "companion_identity": identity.hex(),
        "fingerprint": fingerprint,
        "token": "new-token",
        "device_id": "some-other-device",
        "companion_name": "field-radio",
        "scope": "companion:field-radio",
    }

    with patch.object(client, "_data", return_value=response):
        with pytest.raises(RestError, match="device_id"):
            client.pair(
                "code",
                "expected-device",
                "Phone",
                expected_fingerprint=fingerprint,
            )

    assert client.token == "existing"


@pytest.mark.parametrize(
    "token",
    [None, "", "   ", "two words", "two\twords", "unicode-\N{SNOWMAN}", 123],
)
def test_pair_rejects_unusable_token_without_adopting_it(token):
    client = CompanionRestClient("http://127.0.0.1:8000", token="existing")
    identity = bytes(range(32))
    fingerprint = hashlib.sha256(identity).hexdigest()
    response = {
        "companion_identity": identity.hex(),
        "fingerprint": fingerprint,
        "token": token,
    }

    with patch.object(client, "_data", return_value=response):
        with pytest.raises(RestError) as excinfo:
            client.pair(
                "code",
                "device",
                "Phone",
                expected_fingerprint=fingerprint,
            )

    assert excinfo.value.status == 502
    assert client.token == "existing"


@pytest.mark.parametrize("fingerprint", [None, 123, "gg" * 32, "é" * 64])
def test_pair_rejects_malformed_returned_fingerprint(fingerprint):
    client = CompanionRestClient("http://127.0.0.1:8000", token="existing")
    identity = bytes(range(32))
    expected = hashlib.sha256(identity).hexdigest()
    response = {
        "companion_identity": identity.hex(),
        "fingerprint": fingerprint,
        "token": "new-token",
    }

    with patch.object(client, "_data", return_value=response):
        with pytest.raises(RestError) as excinfo:
            client.pair(
                "code",
                "device",
                "Phone",
                expected_fingerprint=expected,
            )

    assert excinfo.value.status == 502
    assert client.token == "existing"


class _EventResponse:
    def __init__(self, content_type):
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()

    def close(self):
        self.closed = True

    def readline(self, _limit):
        raise AssertionError("invalid Content-Type must fail before reading")


class _JsonResponse:
    def __init__(self, content_type, body):
        self.headers = {"Content-Type": content_type}
        self.body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return None

    def read(self, _limit):
        return self.body

    def geturl(self):
        return "http://127.0.0.1:8000/api/v1/server_info"


def test_success_response_rejects_duplicate_json_fields():
    client = CompanionRestClient("http://127.0.0.1:8000")
    response = _JsonResponse(
        "application/json",
        b'{"success":true,"success":false,"data":{}}',
    )

    with patch("companion_client.rest._opener.open", return_value=response):
        with pytest.raises(RestError, match="invalid JSON"):
            client.server_info()


def test_success_response_rejects_decimal_overflow_as_invalid_json():
    client = CompanionRestClient("http://127.0.0.1:8000")
    response = _JsonResponse(
        "application/json",
        (
            b'{"success":true,"data":{"site_name":"Field",'
            b'"api_versions":["v1"],"auth_modes":[],'
            b'"transport":{"scheme":"http","secure":false,'
            b'"trusted_network_required":true},'
            b'"server":{"version":null,"time":1e999}}}'
        ),
    )

    with patch("companion_client.rest._opener.open", return_value=response):
        with pytest.raises(RestError, match="invalid JSON"):
            client.server_info()


@pytest.mark.parametrize(
    ("before_id", "response", "message"),
    [
        (
            None,
            {"messages": [{"id": 3}, {"id": 3}], "next_before_id": 3},
            "strictly descending",
        ),
        (
            None,
            {"messages": [{"id": 2}, {"id": 3}], "next_before_id": 3},
            "strictly descending",
        ),
        (
            5,
            {"messages": [{"id": 5}], "next_before_id": 5},
            "does not precede before_id",
        ),
        (
            None,
            {"messages": [{"id": 3}, {"id": 2}], "next_before_id": 1},
            "invalid next_before_id",
        ),
        (
            None,
            {"messages": [], "next_before_id": 1},
            "invalid next_before_id",
        ),
        (
            None,
            {"messages": [{"id": 0}], "next_before_id": 0},
            "invalid message ids",
        ),
    ],
)
def test_messages_rejects_unsafe_pagination_contract(
    before_id,
    response,
    message,
):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_data", return_value=response):
        with pytest.raises(RestError, match=message):
            client.messages("field-radio", before_id=before_id)


@pytest.mark.parametrize("before_id", [0, True, "5", 10**1000])
def test_messages_rejects_invalid_before_id_before_network(before_id):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_data") as request:
        with pytest.raises(ValueError, match="before_id"):
            client.messages("field-radio", before_id=before_id)

    request.assert_not_called()


def test_success_response_turns_excessive_json_nesting_into_a_wire_error():
    client = CompanionRestClient("http://127.0.0.1:8000")
    response = _JsonResponse("application/json", b'{"success":true,"data":{}}')

    with (
        patch("companion_client.rest._opener.open", return_value=response),
        patch("companion_client.rest._json_loads", side_effect=RecursionError),
    ):
        with pytest.raises(RestError, match="invalid JSON"):
            client.server_info()


def test_set_channel_rejects_invalid_secret_length_before_network():
    client = CompanionRestClient("http://127.0.0.1:8000", token="secret")

    with patch("companion_client.rest._opener.open") as open_response:
        with pytest.raises(ValueError, match="16 or 32 bytes"):
            client.set_channel("field-radio", 1, "#test", b"short")

    open_response.assert_not_called()


class _LinesEventResponse:
    def __init__(self, *lines):
        self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        self.lines = iter(lines)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()

    def close(self):
        self.closed = True

    def readline(self, _limit):
        return next(self.lines, b"")


def test_events_requires_event_stream_content_type():
    client = CompanionRestClient("http://127.0.0.1:8000", token="secret")
    response = _EventResponse("application/json")

    with patch("companion_client.rest._opener.open", return_value=response):
        stream = client.events("field-radio")
        with pytest.raises(RestError) as excinfo:
            next(stream)

    assert excinfo.value.status == 502
    assert response.closed is True


def test_events_turns_excessive_json_nesting_into_a_wire_error():
    client = CompanionRestClient("http://127.0.0.1:8000", token="secret")
    response = _LinesEventResponse(b"data: {}\n", b"\n")

    with (
        patch("companion_client.rest._opener.open", return_value=response),
        patch("companion_client.rest._json_loads", side_effect=RecursionError),
    ):
        stream = client.events("field-radio")
        with pytest.raises(RestError, match="invalid JSON in SSE event"):
            next(stream)

    assert response.closed is True


def test_events_bounds_empty_data_line_count_before_list_growth():
    client = CompanionRestClient("http://127.0.0.1:8000", token="secret")
    response = _LinesEventResponse(*([b"data:\n"] * (rest_module._MAX_SSE_DATA_LINES + 1)))

    with patch("companion_client.rest._opener.open", return_value=response):
        stream = client.events("field-radio")
        with pytest.raises(RestError, match="too many data lines") as excinfo:
            next(stream)

    assert excinfo.value.status == 502
    assert response.closed is True


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        " abc123:0",
        "ABC123:0",
        "abc123:+1",
        "abc123:" + ("9" * 129),
        "\N{SNOWMAN}:0",
    ],
)
def test_events_rejects_invalid_cursor_before_network_io(cursor):
    client = CompanionRestClient("http://127.0.0.1:8000", token="secret")

    with patch("companion_client.rest._opener.open") as open_response:
        with pytest.raises(ValueError, match="cursor must be"):
            client.events("field-radio", cursor=cursor)

    open_response.assert_not_called()


def test_json_methods_require_application_json_content_type():
    client = CompanionRestClient("http://127.0.0.1:8000")
    response = _JsonResponse(
        "text/plain",
        b'{"success":true,"data":{"api_versions":["v1"]}}',
    )

    with patch("companion_client.rest._opener.open", return_value=response):
        with pytest.raises(RestError, match="Content-Type") as excinfo:
            client.server_info()

    assert excinfo.value.status == 502


def test_events_rejects_an_id_that_disagrees_with_event_sequence():
    client = CompanionRestClient("http://127.0.0.1:8000", token="secret")
    event = {
        "seq": 2,
        "type": "message",
        "ts": 1.0,
        "packet_hash": None,
        "data": _message_data(id=2),
    }
    response = _LinesEventResponse(
        b"id: abc123:1\n",
        b"event: message\n",
        f"data: {json.dumps(event)}\n".encode("utf-8"),
        b"\n",
    )

    with patch("companion_client.rest._opener.open", return_value=response):
        stream = client.events("field-radio")
        with pytest.raises(RestError, match="invalid id"):
            next(stream)

    assert response.closed is True


def test_events_rejects_regressing_event_ids():
    client = CompanionRestClient("http://127.0.0.1:8000", token="secret")

    def event(seq):
        return {
            "seq": seq,
            "type": "message",
            "ts": float(seq),
            "packet_hash": None,
            "data": _message_data(id=seq),
        }

    response = _LinesEventResponse(
        b"id: abc123:2\n",
        b"event: message\n",
        f"data: {json.dumps(event(2))}\n".encode("utf-8"),
        b"\n",
        b"id: abc123:1\n",
        b"event: message\n",
        f"data: {json.dumps(event(1))}\n".encode("utf-8"),
        b"\n",
    )

    with patch("companion_client.rest._opener.open", return_value=response):
        stream = client.events("field-radio")
        assert next(stream).event_id == "abc123:2"
        with pytest.raises(RestError, match="strictly ordered"):
            next(stream)

    assert response.closed is True


def test_follow_page_bound_fails_instead_of_returning_partial_events():
    client = CompanionRestClient("http://127.0.0.1:8000")
    first_page = SyncResult(
        events=[],
        next_cursor="abc123:1",
        has_more=True,
        journal_epoch="abc123",
    )

    with patch.object(client, "sync", return_value=first_page) as sync:
        with pytest.raises(RestError, match="page limit") as excinfo:
            client.follow("field-radio", "abc123:0", max_pages=1)

    sync.assert_called_once()
    assert excinfo.value.url == ("http://127.0.0.1:8000/api/v1/companions/field-radio/sync")


@pytest.mark.parametrize(
    "result",
    [
        SyncResult(
            events=[],
            next_cursor="abc123:0",
            has_more=False,
            journal_epoch="abc123",
            snapshot_required=True,
            reset_reason="pruned_cursor",
        ),
        SyncResult(
            events=[],
            next_cursor="abc123:0",
            has_more=True,
            journal_epoch="abc123",
        ),
    ],
)
def test_follow_local_errors_report_the_actual_sync_url(result):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "sync", return_value=result):
        with pytest.raises(RestError) as excinfo:
            client.follow("field radio", "abc123:0")

    assert excinfo.value.url == ("http://127.0.0.1:8000/api/v1/companions/field%20radio/sync")


def test_dynamic_message_id_is_encoded_as_one_path_segment():
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_data", return_value={}) as request:
        client.message_receptions("field-radio", "1/../../devices")

    assert request.call_args.args[1] == (
        "/companions/field-radio/messages/1%2F..%2F..%2Fdevices/receptions"
    )


def test_idempotency_header_is_validated_before_network_io():
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch("companion_client.rest._opener.open") as open_response:
        with pytest.raises(ValueError, match="visible ASCII"):
            client.send_message(
                "field-radio",
                "hello",
                channel_idx=0,
                idempotency_key="x" * 129,
            )

    open_response.assert_not_called()


@pytest.mark.parametrize(
    "key",
    [" padded", "padded ", "internal space", "\tpadded", "padded\t"],
)
def test_idempotency_key_rejects_all_whitespace_without_normalizing(key):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch("companion_client.rest._opener.open") as open_response:
        with pytest.raises(ValueError, match="visible ASCII"):
            client.send_message(
                "field-radio",
                "hello",
                channel_idx=0,
                idempotency_key=key,
            )

    open_response.assert_not_called()


@pytest.mark.parametrize(
    "key",
    [
        "internal\ttab",
        "nul\x00byte",
        "delete\x7fbyte",
        "next\u0085line",
        "zero\u200bwidth",
        "line\u2028separator",
        "bidi\u202eoverride",
        "emoji-\N{ROCKET}",
    ],
)
def test_idempotency_key_rejects_non_visible_ascii(key):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch("companion_client.rest._opener.open") as open_response:
        with pytest.raises(ValueError, match="visible ASCII"):
            client.send_message(
                "field-radio",
                "hello",
                channel_idx=0,
                idempotency_key=key,
            )

    open_response.assert_not_called()


def test_idempotency_key_rejects_lone_unicode_surrogate():
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch("companion_client.rest._opener.open") as open_response:
        with pytest.raises(ValueError, match="visible ASCII"):
            client.send_message(
                "field-radio",
                "hello",
                channel_idx=0,
                idempotency_key="draft\ud800key",
            )

    open_response.assert_not_called()


@pytest.mark.parametrize(
    ("response", "channel_idx", "to", "message"),
    [
        (
            {
                "message_id": 10**1000,
                "sent": True,
                "state": "transmitted",
            },
            0,
            None,
            "message_id",
        ),
        (
            {"message_id": 1, "sent": False, "state": "failed"},
            0,
            None,
            "failure reason",
        ),
        (
            {"message_id": 1, "sent": True, "state": "transmitted"},
            None,
            "aa" * 32,
            "is_flood",
        ),
        (
            {
                "message_id": 1,
                "sent": False,
                "state": "failed",
                "reason": "",
            },
            0,
            None,
            "failure reason",
        ),
        (
            {
                "message_id": 1,
                "sent": True,
                "state": "transmitted",
                "packet_hash": "aabbccddeeff0011",
            },
            0,
            None,
            "packet_hash",
        ),
        (
            {
                "message_id": 1,
                "sent": True,
                "state": "transmitted",
                "expected_ack": -1,
            },
            0,
            None,
            "expected_ack",
        ),
        (
            {
                "message_id": 1,
                "sent": True,
                "state": "transmitted",
                "expected_ack": 1 << 32,
            },
            0,
            None,
            "expected_ack",
        ),
    ],
)
def test_send_rejects_incomplete_terminal_results(
    response,
    channel_idx,
    to,
    message,
):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_data", return_value=response):
        with pytest.raises(RestError, match=message):
            client.send_message(
                "field-radio",
                "hello",
                channel_idx=channel_idx,
                to=to,
                idempotency_key="saved-draft-key",
            )


@pytest.mark.parametrize(
    "response",
    [
        {
            "registered": True,
            "device_id": "phone",
            "mention_push": False,
        },
        {
            "registered": True,
            "device_id": "phone",
            "push_detail": "none",
            "mention_push": 0,
        },
    ],
)
def test_register_push_rejects_incomplete_preferences(response):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_data", return_value=response):
        with pytest.raises(RestError, match="push registration"):
            client.register_push("phone", "push-token")


@pytest.mark.parametrize(
    ("invoke", "response", "http_method", "path"),
    [
        (
            lambda client: client.messages("field-radio"),
            {"messages": [], "next_before_id": None},
            "GET",
            "/companions/field-radio/messages",
        ),
        (
            lambda client: client.login("field-radio", "aa" * 32, "password"),
            {},
            "POST",
            f"/companions/field-radio/contacts/{'aa' * 32}/login",
        ),
        (
            lambda client: client.has_connection("field-radio", "aa" * 32),
            {"connected": True},
            "GET",
            f"/companions/field-radio/contacts/{'aa' * 32}/connection",
        ),
        (
            lambda client: client.logout("field-radio", "aa" * 32),
            {"logged_out": True, "sent": True},
            "POST",
            f"/companions/field-radio/contacts/{'aa' * 32}/logout",
        ),
        (
            lambda client: client.status_request("field-radio", "aa" * 32),
            {},
            "POST",
            f"/companions/field-radio/contacts/{'aa' * 32}/status_request",
        ),
        (
            lambda client: client.telemetry_request("field-radio", "aa" * 32),
            {},
            "POST",
            f"/companions/field-radio/contacts/{'aa' * 32}/telemetry_request",
        ),
        (
            lambda client: client.reset_path("field-radio", "aa" * 32),
            {"reset": True},
            "POST",
            f"/companions/field-radio/contacts/{'aa' * 32}/reset_path",
        ),
        (
            lambda client: client.message_receptions("field-radio", 7),
            {},
            "GET",
            "/companions/field-radio/messages/7/receptions",
        ),
        (
            lambda client: client.contact_paths("field-radio", "aa" * 32),
            {},
            "GET",
            f"/companions/field-radio/contacts/{'aa' * 32}/paths",
        ),
        (
            lambda client: client.transmission_repeats(
                "field-radio",
                "AABBCCDDEEFF0011",
            ),
            {},
            "GET",
            "/companions/field-radio/transmissions/AABBCCDDEEFF0011/repeats",
        ),
        (
            lambda client: client.devices(),
            [],
            "GET",
            "/devices",
        ),
        (
            lambda client: client.revoke_device("phone"),
            {"revoked": True, "device_id": "phone"},
            "DELETE",
            "/devices/phone",
        ),
        (
            lambda client: client.register_push("phone", "push-token"),
            {
                "registered": True,
                "device_id": "phone",
                "push_detail": "none",
                "mention_push": False,
            },
            "POST",
            "/devices/phone/push",
        ),
        (
            lambda client: client.unregister_push("phone"),
            {"unregistered": True, "device_id": "phone"},
            "DELETE",
            "/devices/phone/push",
        ),
    ],
)
def test_remaining_v1_methods_match_their_server_routes(
    invoke,
    response,
    http_method,
    path,
):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with patch.object(client, "_data", return_value=response) as request:
        assert invoke(client) == response

    assert request.call_args.args[:2] == (http_method, path)


@pytest.mark.parametrize(
    ("method_name", "response", "message"),
    [
        ("has_connection", {"connected": 1}, "connected flag"),
        ("has_connection", {}, "connected flag"),
        ("logout", {"logged_out": True, "sent": 1}, "logout response"),
        ("logout", {"logged_out": 1, "sent": True}, "logout response"),
    ],
)
def test_contact_session_methods_reject_ambiguous_wire_flags(
    method_name,
    response,
    message,
):
    client = CompanionRestClient("http://127.0.0.1:8000")
    method = getattr(client, method_name)

    with patch.object(client, "_data", return_value=response):
        with pytest.raises(RestError, match=message):
            method("field-radio", "aa" * 32)


@pytest.mark.parametrize(
    "timeout",
    [
        False,
        True,
        0,
        -1,
        "not-a-number",
        object(),
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_events_rejects_nonpositive_or_nonfinite_stream_timeout(timeout):
    client = CompanionRestClient("http://127.0.0.1:8000")

    with pytest.raises(ValueError, match="finite number greater than zero"):
        client.events("field-radio", stream_timeout=timeout)
