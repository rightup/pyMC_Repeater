"""Client side of the companion frame protocol.

The framing and command/response codes are imported from ``openhop_core``
rather than restated here, so this cannot drift from what the frame server
actually speaks. Only the *client* direction lives in this module: the server
side is ``openhop_core.companion.frame_server``.

Wire format (both directions)::

    [prefix u8][length u16 LE][payload ...]

with ``0x3C`` ('<') for client→server and ``0x3E`` ('>') for server→client.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from openhop_core.companion.constants import (
    CHANNEL_NAME_SIZE,
    CMD_APP_START,
    CMD_DEVICE_QUERY,
    CMD_GET_CHANNEL,
    CMD_GET_CONTACTS,
    CMD_SEND_CHANNEL_TXT_MSG,
    CMD_SEND_TXT_MSG,
    CMD_SYNC_NEXT_MESSAGE,
    FRAME_INBOUND_PREFIX,
    FRAME_OUTBOUND_PREFIX,
    MAX_FRAME_SIZE,
    RESP_CODE_CHANNEL_INFO,
    RESP_CODE_CHANNEL_MSG_RECV,
    RESP_CODE_CHANNEL_MSG_RECV_V3,
    RESP_CODE_CONTACT_MSG_RECV,
    RESP_CODE_CONTACT_MSG_RECV_V3,
    RESP_CODE_ERR,
    RESP_CODE_NO_MORE_MESSAGES,
    RESP_CODE_OK,
    RESP_CODE_SELF_INFO,
    TXT_TYPE_PLAIN,
)

# Push codes are unsolicited (0x80+); everything below is a reply to a command.
PUSH_CODE_MIN = 0x80


class ProtocolError(Exception):
    """The peer sent something that is not a well-formed frame."""


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def encode_frame(payload: bytes) -> bytes:
    """Wrap a command payload in a client→server frame."""
    if len(payload) > MAX_FRAME_SIZE:
        raise ProtocolError(f"payload {len(payload)} exceeds MAX_FRAME_SIZE {MAX_FRAME_SIZE}")
    return bytes([FRAME_INBOUND_PREFIX]) + struct.pack("<H", len(payload)) + payload


class FrameDecoder:
    """Incremental decoder for the server→client byte stream.

    TCP gives no message boundaries, so frames must be reassembled from
    whatever chunk sizes arrive. Feed bytes in, take whole payloads out.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Add received bytes; return every complete payload now available."""
        self._buffer.extend(data)
        payloads: list[bytes] = []
        while True:
            if len(self._buffer) < 3:
                return payloads
            if self._buffer[0] != FRAME_OUTBOUND_PREFIX:
                # Resynchronise rather than dying: drop one byte and retry.
                del self._buffer[0]
                continue
            length = struct.unpack("<H", bytes(self._buffer[1:3]))[0]
            if length > MAX_FRAME_SIZE:
                raise ProtocolError(f"frame length {length} exceeds MAX_FRAME_SIZE")
            if len(self._buffer) < 3 + length:
                return payloads  # incomplete; wait for more
            payloads.append(bytes(self._buffer[3 : 3 + length]))
            del self._buffer[: 3 + length]


# --------------------------------------------------------------------------
# Commands (client → server)
# --------------------------------------------------------------------------


def cmd_app_start() -> bytes:
    """CMD_APP_START: 7 reserved bytes after the code (server rejects shorter).

    Those bytes really are reserved -- they are NOT a version. The app's target
    version is negotiated separately via :func:`cmd_device_query`, and until
    that happens the server's ``_app_target_ver`` stays 0 and it replies with
    the pre-V3 message frames.
    """
    return bytes([CMD_APP_START]) + bytes(7)


def cmd_device_query(app_target_ver: int = 3) -> bytes:
    """CMD_DEVICE_QUERY: [ver u8].

    This is what actually sets the server's app target version, which selects
    between the V3 message frames (with SNR) and the older ones. Send it before
    syncing messages if you want V3.
    """
    return bytes([CMD_DEVICE_QUERY, app_target_ver])


def cmd_send_channel_text(channel_idx: int, text: str, timestamp: int) -> bytes:
    """CMD_SEND_CHANNEL_TXT_MSG: [type u8][channel u8][timestamp u32][text]."""
    return (
        bytes([CMD_SEND_CHANNEL_TXT_MSG, TXT_TYPE_PLAIN, channel_idx])
        + struct.pack("<I", timestamp)
        + text.encode("utf-8")
    )


def cmd_send_text(pubkey_prefix: bytes, text: str, timestamp: int, attempt: int = 0) -> bytes:
    """CMD_SEND_TXT_MSG: [type u8][attempt u8][timestamp u32][pubkey[6]][text]."""
    prefix = pubkey_prefix[:6].ljust(6, b"\x00")
    return (
        bytes([CMD_SEND_TXT_MSG, TXT_TYPE_PLAIN, attempt])
        + struct.pack("<I", timestamp)
        + prefix
        + text.encode("utf-8")
    )


def cmd_get_channel(channel_idx: int) -> bytes:
    """CMD_GET_CHANNEL for ONE channel: [idx u8].

    Sending this with an empty body instead asks for the whole table, which the
    server answers with one CHANNEL_INFO frame *per* channel index. That breaks
    the one-command-one-response invariant this client relies on (the protocol
    has no request IDs), so callers enumerate by index instead.
    """
    return bytes([CMD_GET_CHANNEL, channel_idx])


def cmd_sync_next_message() -> bytes:
    return bytes([CMD_SYNC_NEXT_MESSAGE])


def cmd_get_contacts() -> bytes:
    return bytes([CMD_GET_CONTACTS])


# --------------------------------------------------------------------------
# Responses (server → client)
# --------------------------------------------------------------------------


@dataclass
class SelfInfo:
    """Decoded RESP_CODE_SELF_INFO -- the reply to CMD_APP_START."""

    adv_type: int
    tx_power_dbm: int
    max_tx_power: int
    public_key: bytes
    latitude: float
    longitude: float
    frequency_khz: int
    bandwidth_hz: int
    spreading_factor: int
    coding_rate: int
    node_name: str

    @property
    def companion_hash(self) -> str:
        """First pubkey byte as lowercase hex -- how the repeater keys a companion."""
        return f"{self.public_key[0]:02x}"


@dataclass
class Channel:
    """A group channel slot. Unconfigured slots come back with an empty name."""

    idx: int
    name: str
    secret: bytes = b""

    @property
    def is_configured(self) -> bool:
        """An empty name means the slot is unused -- the server zero-fills it."""
        return bool(self.name)


@dataclass
class ReceivedMessage:
    """A message pulled via CMD_SYNC_NEXT_MESSAGE."""

    text: str
    timestamp: int
    is_channel: bool
    channel_idx: Optional[int] = None
    sender_prefix: Optional[bytes] = None
    txt_type: int = TXT_TYPE_PLAIN
    path_len: int = 0
    snr: Optional[float] = None
    raw: bytes = field(default=b"", repr=False)


def parse_self_info(payload: bytes) -> SelfInfo:
    """Decode RESP_CODE_SELF_INFO.

    Layout: [code][adv_type][tx_power][max_tx_power][pubkey 32][lat i32][lon i32]
    [multi_acks][advert_loc_policy][telemetry_mode][manual_add][freq u32]
    [bandwidth u32][sf][cr][name...]
    """
    if len(payload) < 4 or payload[0] != RESP_CODE_SELF_INFO:
        raise ProtocolError("not a SELF_INFO frame")
    if len(payload) < 58:
        raise ProtocolError(f"SELF_INFO too short: {len(payload)}")
    adv_type, tx_power, max_tx_power = payload[1], payload[2], payload[3]
    public_key = payload[4:36]
    lat, lon = struct.unpack("<ii", payload[36:44])
    # payload[44] multi_acks, [45] advert_loc_policy, [46] telemetry, [47] manual_add
    freq_khz, bandwidth = struct.unpack("<II", payload[48:56])
    sf, cr = payload[56], payload[57]
    name = payload[58:].decode("utf-8", errors="replace").rstrip("\x00")
    return SelfInfo(
        adv_type=adv_type,
        tx_power_dbm=tx_power,
        max_tx_power=max_tx_power,
        public_key=public_key,
        latitude=lat / 1e6,
        longitude=lon / 1e6,
        frequency_khz=freq_khz,
        bandwidth_hz=bandwidth,
        spreading_factor=sf,
        coding_rate=cr,
        node_name=name,
    )


def _decode_snr(snr_byte: int) -> float:
    # Firmware packs SNR as a signed byte scaled by 4.
    signed = snr_byte - 256 if snr_byte > 127 else snr_byte
    return signed / 4.0


def parse_channel_info(payload: bytes) -> Channel:
    """Decode RESP_CODE_CHANNEL_INFO: [code][idx][name 32][secret 16]."""
    if not payload or payload[0] != RESP_CODE_CHANNEL_INFO:
        raise ProtocolError("not a CHANNEL_INFO frame")
    if len(payload) < 2 + CHANNEL_NAME_SIZE:
        raise ProtocolError("CHANNEL_INFO too short")
    name_raw = payload[2 : 2 + CHANNEL_NAME_SIZE]
    secret = payload[2 + CHANNEL_NAME_SIZE : 2 + CHANNEL_NAME_SIZE + 16]
    return Channel(
        idx=payload[1],
        name=name_raw.split(b"\x00")[0].decode("utf-8", errors="replace").strip(),
        secret=secret,
    )


def parse_message(payload: bytes) -> Optional[ReceivedMessage]:
    """Decode a message frame, or return None for NO_MORE_MESSAGES.

    Handles both the V3 frames (which carry SNR) and the older non-V3 forms,
    since the server picks its reply based on the app_target_ver we sent at
    APP_START.
    """
    if not payload:
        raise ProtocolError("empty frame")
    code = payload[0]

    if code == RESP_CODE_NO_MORE_MESSAGES:
        return None

    if code == RESP_CODE_CHANNEL_MSG_RECV_V3:
        # [code][snr][0][0][channel][path_len][txt_type][timestamp u32][text]
        if len(payload) < 11:
            raise ProtocolError("CHANNEL_MSG_RECV_V3 too short")
        return ReceivedMessage(
            text=payload[11:].decode("utf-8", errors="replace").rstrip("\x00"),
            timestamp=struct.unpack("<I", payload[7:11])[0],
            is_channel=True,
            channel_idx=payload[4],
            path_len=payload[5],
            txt_type=payload[6],
            snr=_decode_snr(payload[1]),
            raw=payload,
        )

    if code == RESP_CODE_CHANNEL_MSG_RECV:
        # [code][channel][path_len][txt_type][timestamp u32][text]
        if len(payload) < 8:
            raise ProtocolError("CHANNEL_MSG_RECV too short")
        return ReceivedMessage(
            text=payload[8:].decode("utf-8", errors="replace").rstrip("\x00"),
            timestamp=struct.unpack("<I", payload[4:8])[0],
            is_channel=True,
            channel_idx=payload[1],
            path_len=payload[2],
            txt_type=payload[3],
            raw=payload,
        )

    if code == RESP_CODE_CONTACT_MSG_RECV_V3:
        # [code][snr][0][0][prefix 6][path_len][txt_type][timestamp u32][text]
        if len(payload) < 16:
            raise ProtocolError("CONTACT_MSG_RECV_V3 too short")
        return ReceivedMessage(
            text=payload[16:].decode("utf-8", errors="replace").rstrip("\x00"),
            timestamp=struct.unpack("<I", payload[12:16])[0],
            is_channel=False,
            sender_prefix=payload[4:10],
            path_len=payload[10],
            txt_type=payload[11],
            snr=_decode_snr(payload[1]),
            raw=payload,
        )

    if code == RESP_CODE_CONTACT_MSG_RECV:
        # [code][prefix 6][path_len][txt_type][timestamp u32][text]
        if len(payload) < 13:
            raise ProtocolError("CONTACT_MSG_RECV too short")
        return ReceivedMessage(
            text=payload[13:].decode("utf-8", errors="replace").rstrip("\x00"),
            timestamp=struct.unpack("<I", payload[9:13])[0],
            is_channel=False,
            sender_prefix=payload[1:7],
            path_len=payload[7],
            txt_type=payload[8],
            raw=payload,
        )

    raise ProtocolError(f"unexpected message frame code 0x{code:02x}")


def is_push(payload: bytes) -> bool:
    """Push frames are unsolicited; everything else answers a command."""
    return bool(payload) and payload[0] >= PUSH_CODE_MIN


def is_ok(payload: bytes) -> bool:
    return bool(payload) and payload[0] == RESP_CODE_OK


def error_code(payload: bytes) -> Optional[int]:
    """Return the error code of a RESP_CODE_ERR frame, else None."""
    if payload and payload[0] == RESP_CODE_ERR:
        return payload[1] if len(payload) > 1 else -1
    return None
