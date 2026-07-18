"""Unit tests for the companion client's frame codec.

These need no server: they pin the wire format against the constants the real
frame server imports, so a change on either side shows up here.
"""

from __future__ import annotations

import struct

import pytest
from openhop_core.companion.constants import (
    FRAME_INBOUND_PREFIX,
    FRAME_OUTBOUND_PREFIX,
    MAX_FRAME_SIZE,
    RESP_CODE_CHANNEL_MSG_RECV_V3,
    RESP_CODE_CONTACT_MSG_RECV_V3,
    RESP_CODE_NO_MORE_MESSAGES,
    RESP_CODE_SELF_INFO,
)

from companion_client import protocol


def outbound(payload: bytes) -> bytes:
    return bytes([FRAME_OUTBOUND_PREFIX]) + struct.pack("<H", len(payload)) + payload


# --- framing --------------------------------------------------------------


def test_encode_frame_uses_inbound_prefix_and_le_length():
    frame = protocol.encode_frame(b"\x01\x02\x03")
    assert frame[0] == FRAME_INBOUND_PREFIX
    assert struct.unpack("<H", frame[1:3])[0] == 3
    assert frame[3:] == b"\x01\x02\x03"


def test_encode_frame_rejects_oversized_payload():
    with pytest.raises(protocol.ProtocolError, match="MAX_FRAME_SIZE"):
        protocol.encode_frame(b"x" * (MAX_FRAME_SIZE + 1))


def test_decoder_returns_whole_frames():
    decoder = protocol.FrameDecoder()
    assert decoder.feed(outbound(b"\x00") + outbound(b"\x01\x02")) == [b"\x00", b"\x01\x02"]


def test_decoder_reassembles_split_frames():
    """TCP gives no message boundaries; a frame may arrive one byte at a time."""
    decoder = protocol.FrameDecoder()
    frame = outbound(b"\x05hello")
    collected = []
    for i in range(len(frame)):
        collected.extend(decoder.feed(frame[i : i + 1]))
    assert collected == [b"\x05hello"]


def test_decoder_holds_incomplete_frame():
    decoder = protocol.FrameDecoder()
    frame = outbound(b"abcdef")
    assert decoder.feed(frame[:-2]) == []
    assert decoder.feed(frame[-2:]) == [b"abcdef"]


def test_decoder_resynchronises_after_garbage():
    decoder = protocol.FrameDecoder()
    assert decoder.feed(b"\x00\x11garbage" + outbound(b"\x07")) == [b"\x07"]


def test_decoder_rejects_absurd_length():
    decoder = protocol.FrameDecoder()
    bogus = bytes([FRAME_OUTBOUND_PREFIX]) + struct.pack("<H", MAX_FRAME_SIZE + 50)
    with pytest.raises(protocol.ProtocolError, match="exceeds"):
        decoder.feed(bogus + b"x" * 10)


# --- command builders -----------------------------------------------------
# The server strips the command byte before its length checks, so each builder
# is asserted against the minimum its handler enforces.


def test_app_start_meets_server_minimum():
    # _cmd_app_start rejects data shorter than 7 bytes.
    assert len(protocol.cmd_app_start()) - 1 >= 7


def test_device_query_carries_target_version():
    # This -- not APP_START -- is what sets the server's _app_target_ver.
    assert protocol.cmd_device_query(3)[1] == 3


def test_channel_text_layout():
    payload = protocol.cmd_send_channel_text(2, "hi", 1234)
    data = payload[1:]
    assert len(data) >= 6  # _cmd_send_channel_txt_msg minimum
    assert data[1] == 2  # channel index
    assert struct.unpack("<I", data[2:6])[0] == 1234
    assert data[6:] == b"hi"


def test_direct_text_layout():
    payload = protocol.cmd_send_text(b"\xaa" * 6, "yo", 99)
    data = payload[1:]
    assert len(data) >= 13  # _cmd_send_txt_msg minimum
    assert struct.unpack("<I", data[2:6])[0] == 99
    assert data[6:12] == b"\xaa" * 6
    assert data[12:] == b"yo"


def test_short_pubkey_prefix_is_padded():
    data = protocol.cmd_send_text(b"\x01\x02", "x", 1)[1:]
    assert data[6:12] == b"\x01\x02\x00\x00\x00\x00"


# --- response parsing -----------------------------------------------------


def build_self_info(name: str = "Node", pubkey_first: int = 0xF5) -> bytes:
    pubkey = bytes([pubkey_first]) + bytes(range(31))
    return (
        bytes([RESP_CODE_SELF_INFO, 1, 20, 30])
        + pubkey
        + struct.pack("<ii", 47_606_200, -122_332_100)
        + bytes([0, 0, 0, 0])
        + struct.pack("<II", 906_875, 250_000)
        + bytes([10, 5])
        + name.encode()
    )


def test_parse_self_info():
    info = protocol.parse_self_info(build_self_info("TestNode"))
    assert info.node_name == "TestNode"
    assert info.companion_hash == "f5"
    assert info.spreading_factor == 10
    assert info.coding_rate == 5
    assert info.frequency_khz == 906_875
    assert round(info.latitude, 4) == 47.6062
    assert round(info.longitude, 4) == -122.3321


def test_parse_self_info_rejects_truncated():
    with pytest.raises(protocol.ProtocolError, match="too short"):
        protocol.parse_self_info(build_self_info()[:40])


def test_parse_self_info_rejects_wrong_code():
    with pytest.raises(protocol.ProtocolError, match="not a SELF_INFO"):
        protocol.parse_self_info(b"\x63" + bytes(80))


def test_no_more_messages_parses_as_none():
    assert protocol.parse_message(bytes([RESP_CODE_NO_MORE_MESSAGES])) is None


def test_parse_channel_message_v3():
    payload = (
        bytes([RESP_CODE_CHANNEL_MSG_RECV_V3, 0x10, 0, 0, 3, 2, 0])
        + struct.pack("<I", 555)
        + b"hello channel"
    )
    message = protocol.parse_message(payload)
    assert message.is_channel is True
    assert message.channel_idx == 3
    assert message.text == "hello channel"
    assert message.timestamp == 555
    assert message.snr == 4.0  # 0x10 / 4


def test_parse_contact_message_v3():
    payload = (
        bytes([RESP_CODE_CONTACT_MSG_RECV_V3, 0, 0, 0])
        + b"\xaa" * 6
        + bytes([1, 0])
        + struct.pack("<I", 777)
        + b"direct hello"
    )
    message = protocol.parse_message(payload)
    assert message.is_channel is False
    assert message.sender_prefix == b"\xaa" * 6
    assert message.text == "direct hello"
    assert message.timestamp == 777


def test_negative_snr_decodes_signed():
    payload = (
        bytes([RESP_CODE_CHANNEL_MSG_RECV_V3, 0xF0, 0, 0, 0, 0, 0]) + struct.pack("<I", 1) + b"x"
    )
    assert protocol.parse_message(payload).snr == -4.0


def test_unknown_frame_code_raises():
    with pytest.raises(protocol.ProtocolError, match="unexpected"):
        protocol.parse_message(bytes([0x7F]))


def test_push_classification():
    assert protocol.is_push(bytes([0x83])) is True
    assert protocol.is_push(bytes([0x00])) is False


def test_error_code_extraction():
    assert protocol.error_code(bytes([1, 5])) == 5
    assert protocol.error_code(bytes([0])) is None
