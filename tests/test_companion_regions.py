import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openhop_core.companion.constants import CMD_SEND_CHANNEL_TXT_MSG, RESP_CODE_ERR, RESP_CODE_OK
from openhop_core.protocol import LocalIdentity
from openhop_core.protocol.constants import ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD
from openhop_core.protocol.packet_builder import PacketBuilder
from openhop_core.protocol.transport_keys import calc_transport_code, get_auto_key_for

from repeater.companion import bridge as bridge_module
from repeater.companion.bridge import RepeaterCompanionBridge
from repeater.companion.frame_server import CompanionFrameServer, _BaseFrameServer


def _scoped_channel_body(region=b"usa", text=b"hello", channel_idx=1):
    return (
        bytes([0x80, channel_idx]) + struct.pack("<I", 123) + bytes([len(region)]) + region + text
    )


def _region_test_bridge(injector, *, node_name="Alice"):
    bridge = RepeaterCompanionBridge(LocalIdentity(), injector, node_name=node_name)
    # Nonzero upper half ensures the Core builder must use the complete secret.
    assert bridge.set_channel(1, "test", bytes(range(32)))
    return bridge


@pytest.mark.asyncio
async def test_scoped_channel_preserves_full_secret_and_echo_tracking():
    packets = []

    async def inject(packet, **_kwargs):
        packets.append(packet)
        return True

    bridge = _region_test_bridge(inject)
    bridge.set_flood_scope(get_auto_key_for("default"))

    assert await bridge.send_channel_message(1, "hello", timestamp=123, region=" #USA ")
    packet = packets[0]
    expected = PacketBuilder.create_group_datagram(
        "test", bridge._identity, "hello", "Alice", bridge.channels.get_channels(), timestamp=123
    )
    assert packet.get_payload() == expected.get_payload()
    assert packet.get_route_type() == ROUTE_TYPE_TRANSPORT_FLOOD
    assert packet.transport_codes == [calc_transport_code(get_auto_key_for("usa"), packet), 0]
    assert packet._flood_scope_applied is True
    assert bridge._check_and_track_group_packet(packet) is True
    assert bridge._flood_transport_key == get_auto_key_for("default")


@pytest.mark.asyncio
async def test_scoped_channel_context_is_consumed_before_nested_or_concurrent_sends():
    packets = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def inject(packet, **_kwargs):
        packets.append(packet)
        if len(packets) == 1:
            # Same-task and child-task sends from an injector must not inherit it.
            await bridge.send_channel_message(1, "nested", timestamp=124)
            await asyncio.create_task(bridge.send_channel_message(1, "child", timestamp=125))
            started.set()
            await release.wait()
        return True

    bridge = _region_test_bridge(inject)
    bridge.set_flood_scope(get_auto_key_for("default"))
    task = asyncio.create_task(bridge.send_channel_message(1, "scoped", region="usa"))
    await asyncio.wait_for(started.wait(), 1)
    assert await bridge.send_channel_message(1, "parallel")
    other = _region_test_bridge(inject)
    assert await other.send_channel_message(1, "other")
    release.set()
    assert await task
    assert packets[0].transport_codes[0] == calc_transport_code(get_auto_key_for("usa"), packets[0])
    for packet in packets[1:4]:
        assert packet.transport_codes[0] == calc_transport_code(get_auto_key_for("default"), packet)
    assert packets[4].get_route_type() == ROUTE_TYPE_FLOOD


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["builder", "injector", "cancel"])
async def test_scoped_channel_errors_and_cancellation_do_not_leak(failure):
    packets = []
    started = asyncio.Event()

    async def inject(packet, **_kwargs):
        packets.append(packet)
        if len(packets) == 1 and failure != "builder":
            started.set()
            if failure == "cancel":
                await asyncio.Event().wait()
            raise RuntimeError("injection failed")
        return True

    bridge = _region_test_bridge(inject)
    if failure == "builder":
        with patch.object(
            PacketBuilder, "create_group_datagram", side_effect=ValueError("builder")
        ):
            assert not await bridge.send_channel_message(1, "scoped", region="usa")
    elif failure == "cancel":
        task = asyncio.create_task(bridge.send_channel_message(1, "scoped", region="usa"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert not await bridge.send_channel_message(1, "scoped", region="usa")
    assert await bridge.send_channel_message(1, "normal")
    assert packets[-1].get_route_type() == ROUTE_TYPE_FLOOD


@pytest.mark.asyncio
async def test_frame_regions_capability_and_scoped_send_use_existing_command():
    packets = []

    async def inject(packet, **_kwargs):
        packets.append(packet)
        return True

    bridge = _region_test_bridge(inject)
    server = CompanionFrameServer(bridge, "0x01", port=0)
    server._write_frame = MagicMock()
    await server._handle_cmd(bytes([CMD_SEND_CHANNEL_TXT_MSG, 0x81, 0, 0, 0, 0, 0]))
    server._write_frame.assert_called_once_with(b"\xf0OHREG1" + bridge.get_public_key())
    assert packets == []
    server._write_frame.reset_mock()
    await server._handle_cmd(bytes([CMD_SEND_CHANNEL_TXT_MSG]) + _scoped_channel_body())
    server._write_frame.assert_called_once_with(bytes([RESP_CODE_OK]))
    assert packets[0].transport_codes[0] == calc_transport_code(get_auto_key_for("usa"), packets[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"\x81",
        b"\x81\x00\x00\x00\x00\x01",
        b"\x81" + bytes(6),
        _scoped_channel_body(region=b""),
        _scoped_channel_body(region=b"a" * 31),
        _scoped_channel_body(region=b"USA"),
        _scoped_channel_body(region=b"#usa"),
        _scoped_channel_body(region=b" usa"),
        _scoped_channel_body(region=b"us_a"),
        _scoped_channel_body(region=b"\xff"),
        _scoped_channel_body(text=b""),
        _scoped_channel_body(text=b" \t"),
        _scoped_channel_body(text=b"hi\x00"),
        _scoped_channel_body(text=b"\xff"),
        _scoped_channel_body(text=b"x" * 166),
        _scoped_channel_body(channel_idx=255),
        b"\x80\x01" + bytes(4) + b"\x03us",
    ],
)
async def test_frame_regions_invalid_input_never_reaches_rf(body):
    injector = AsyncMock(return_value=True)
    bridge = _region_test_bridge(injector)
    server = CompanionFrameServer(bridge, "0x01", port=0)
    server._write_frame = MagicMock()
    await server._cmd_send_channel_txt_msg(body)
    assert server._write_frame.call_args.args[0][0] == RESP_CODE_ERR
    injector.assert_not_awaited()


@pytest.mark.asyncio
async def test_frame_scoped_capacity_rejects_while_normal_keeps_legacy_truncation():
    injector = AsyncMock(return_value=True)
    bridge = _region_test_bridge(injector, node_name="N" * 31)
    server = CompanionFrameServer(bridge, "0x01", port=0)
    server._write_frame = MagicMock()
    await server._cmd_send_channel_txt_msg(_scoped_channel_body(text=b"x" * 128))
    assert server._write_frame.call_args.args[0][0] == RESP_CODE_ERR
    injector.assert_not_awaited()
    await server._cmd_send_channel_txt_msg(bytes([0, 1]) + bytes(4) + b"x" * 128)
    assert server._write_frame.call_args.args[0] == bytes([RESP_CODE_OK])
    injector.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_frame_server_rejects_scoped_subtypes_before_rf():
    injector = AsyncMock(return_value=True)
    bridge = _region_test_bridge(injector)
    server = _BaseFrameServer(bridge, "0x01", port=0)
    server._write_frame = MagicMock()
    for body in (_scoped_channel_body(), bytes([0x81, 0, 0, 0, 0, 0])):
        await server._cmd_send_channel_txt_msg(body)
        assert server._write_frame.call_args.args[0][0] == RESP_CODE_ERR
    injector.assert_not_awaited()


@pytest.mark.parametrize("value,expected", [(" #USA-West ", "usa-west"), ("a" * 30, "a" * 30)])
def test_region_name_normalizes_public_names(value, expected):
    assert bridge_module.normalize_region_name(value) == expected


@pytest.mark.parametrize("value", [None, 123, "", "#", "a" * 31, "a_b", "a b", "é", "K", "a\x00"])
def test_region_name_rejects_invalid_or_non_ascii_input(value):
    with pytest.raises(ValueError):
        bridge_module.normalize_region_name(value)


@pytest.mark.asyncio
async def test_scoped_frame_accepts_exact_maximum_frame_and_utf8_text():
    injector = AsyncMock(return_value=True)
    bridge = _region_test_bridge(injector, node_name="A")
    server = CompanionFrameServer(bridge, "0x01", port=0)
    server._write_frame = MagicMock()
    # 1 command + 7 fixed bytes + 12 region + 156 text = 176.
    body = _scoped_channel_body(region=b"a" * 12, text="é".encode() * 78)
    assert len(body) + 1 == 176
    await server._cmd_send_channel_txt_msg(body)
    assert server._write_frame.call_args.args[0] == bytes([RESP_CODE_OK])
    injector.assert_awaited_once()
    injector.reset_mock()
    await server._cmd_send_channel_txt_msg(body + b"x")
    assert server._write_frame.call_args.args[0][0] == RESP_CODE_ERR
    injector.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_channel_missing_target_clears_intent_without_persisting_settings():
    injector = AsyncMock(return_value=True)
    bridge = _region_test_bridge(injector)
    bridge._save_prefs = MagicMock()
    assert not await bridge.send_channel_message(255, "missing", region="usa")
    injector.assert_not_awaited()
    assert await bridge.send_channel_message(1, "normal")
    assert injector.call_args.args[0].get_route_type() == ROUTE_TYPE_FLOOD
    bridge._save_prefs.assert_not_called()


@pytest.mark.asyncio
async def test_two_concurrent_scoped_sends_keep_independent_regions():
    packets = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def inject(packet, **_kwargs):
        packets.append(packet)
        if len(packets) == 1:
            started.set()
            await release.wait()
        return True

    bridge = _region_test_bridge(inject)
    first = asyncio.create_task(bridge.send_channel_message(1, "first", region="usa"))
    await asyncio.wait_for(started.wait(), 1)
    assert await bridge.send_channel_message(1, "second", region="europe")
    release.set()
    assert await first
    for packet, region in zip(packets, ("usa", "europe")):
        assert packet.transport_codes[0] == calc_transport_code(get_auto_key_for(region), packet)
