"""
Repeater-specific CompanionFrameServer with SQLite persistence.

Thin subclass of :class:`openhop_core.companion.frame_server.CompanionFrameServer`
that adds SQLite-backed message, contact, and channel persistence via a
``sqlite_handler`` dependency.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

from openhop_core.companion.constants import (
    ERR_CODE_ILLEGAL_ARG,
    ERR_CODE_NOT_FOUND,
    MAX_FRAME_SIZE,
    PUB_KEY_SIZE,
    RESP_CODE_NO_MORE_MESSAGES,
)
from openhop_core.companion.frame_server import CompanionFrameServer as _BaseFrameServer
from openhop_core.companion.models import QueuedMessage

from repeater.companion.bridge import normalize_region_name

logger = logging.getLogger("CompanionFrameServer")


class CompanionFrameServer(_BaseFrameServer):
    """Adds SQLite persistence for messages, contacts, and channels.

    Constructor signature is intentionally kept compatible with the
    previous monolithic implementation so ``main.py`` call-sites need
    zero changes.
    """

    def __init__(
        self,
        bridge,
        companion_hash: str,
        port: int = 5000,
        bind_address: str = "0.0.0.0",  # nosec B104 - intentional default for LAN reachability
        client_idle_timeout_sec: Optional[int] = 8 * 60 * 60,  # 8 hours
        sqlite_handler=None,
        local_hash: Optional[int] = None,
        stats_getter=None,
        control_handler=None,
    ):
        super().__init__(
            bridge=bridge,
            companion_hash=companion_hash,
            port=port,
            bind_address=bind_address,
            client_idle_timeout_sec=client_idle_timeout_sec,
            device_model="openHop-Repeater-Companion",
            device_version=None,  # use FIRMWARE_VER_CODE from openhop-core
            build_date="13 Feb 2026",
            local_hash=local_hash,
            stats_getter=stats_getter,
            control_handler=control_handler,
        )
        self.sqlite_handler = sqlite_handler

    async def _cmd_send_channel_txt_msg(self, data: bytes) -> None:
        if data and data[0] == 0x81:
            # A distinct response cannot be confused with an ordinary send's OK.
            if data != b"\x81\x00\x00\x00\x00\x00":
                self._write_err(ERR_CODE_ILLEGAL_ARG)
                return
            public_key = self.bridge.get_public_key()
            if len(public_key) != PUB_KEY_SIZE:
                self._write_err(ERR_CODE_ILLEGAL_ARG)
                return
            self._write_frame(b"\xf0OHREG1" + public_key)
            return
        if data and data[0] == 0x80:
            # Count the command byte as part of the upstream Frame payload cap.
            if len(data) < 9 or len(data) + 1 > MAX_FRAME_SIZE:
                self._write_err(ERR_CODE_ILLEGAL_ARG)
                return
            channel_idx = data[1]
            timestamp = struct.unpack("<I", data[2:6])[0]
            region_length = data[6]
            if not 1 <= region_length <= 30 or len(data) <= 7 + region_length:
                self._write_err(ERR_CODE_ILLEGAL_ARG)
                return
            try:
                region = data[7 : 7 + region_length].decode("ascii")
                if normalize_region_name(region) != region:
                    raise ValueError("wire region must be canonical")
                text = data[7 + region_length :].decode("utf-8")
                if not text.strip() or "\x00" in text:
                    raise ValueError("invalid channel text")
            except ValueError:
                self._write_err(ERR_CODE_ILLEGAL_ARG)
                return
            if self.bridge.get_channel(channel_idx) is None:
                self._write_err(ERR_CODE_NOT_FOUND)
                return
            try:
                ok = await self.bridge.send_channel_message(
                    channel_idx, text, timestamp=timestamp, region=region
                )
            except ValueError:
                self._write_err(ERR_CODE_ILLEGAL_ARG)
                return
            self._write_ok() if ok else self._write_err(ERR_CODE_NOT_FOUND)
            return
        await super()._cmd_send_channel_txt_msg(data)

    async def start(self) -> None:
        """Start persistence before accepting companion client connections."""
        if self.sqlite_handler:
            self.bridge.on_message_event(self._on_message_event)
            self.bridge.on_channel_message_event(self._on_channel_message_event)
            self.bridge.on_channel_data_event(self._on_channel_data_event)
        await super().start()

    # -----------------------------------------------------------------
    # Persistence hook overrides
    # -----------------------------------------------------------------

    async def _persist_companion_message(self, msg_dict: dict, queue_entry=None) -> None:
        """Persist message to SQLite and remove it from the bridge queue.

        The bridge's ``offline_queue_size`` (``message_queue.max_size``) doubles
        as the SQLite retention limit: 0 disables offline storage entirely, so the
        message is dropped instead of persisted.

        ``queue_entry`` is the exact in-memory entry this message came from.  The
        persisted entry is removed by identity (``message_queue.remove``) rather
        than by ``pop_last``: pushes happen synchronously in sibling receive
        tasks, so during the awaited ``to_thread`` another task can append a newer
        entry and ``pop_last`` would remove that one instead — duplicating this
        message and losing the newer one.
        """
        if not self.sqlite_handler:
            return
        # Older cores predate the public max_size property.
        retention = getattr(
            self.bridge.message_queue,
            "max_size",
            getattr(self.bridge.message_queue, "_max_size", None),
        )
        if retention == 0:
            self._remove_queue_entry(queue_entry)
            return
        persisted = await asyncio.to_thread(
            self.sqlite_handler.companion_push_message,
            self.companion_hash,
            msg_dict,
            retention,
        )
        if persisted:
            self._remove_queue_entry(queue_entry)
        else:
            logger.debug(
                "Companion %s: retaining message in memory after SQLite queue rejection",
                self.companion_hash,
            )

    def _remove_queue_entry(self, queue_entry) -> None:
        """Remove exactly the persisted entry from the bridge queue by identity.

        Falling back to ``pop_last`` when ``queue_entry`` is None would reopen the
        interleaving race (it could evict a newer, unpersisted entry), so an event
        from an older core that carries no entry is left in memory: a possible
        duplicate is preferable to losing a message.
        """
        if queue_entry is None:
            logger.debug(
                "Companion %s: no queue entry on persisted message; leaving in memory",
                self.companion_hash,
            )
            return
        self.bridge.message_queue.remove(queue_entry)

    def _sync_next_from_persistence(self) -> Optional[QueuedMessage]:
        """Retrieve next message from SQLite when bridge queue is empty."""
        if not self.sqlite_handler:
            return None
        msg_dict = self.sqlite_handler.companion_pop_message(self.companion_hash)
        if not msg_dict:
            return None
        sender_prefix = msg_dict.get("sender_prefix", b"")
        if isinstance(sender_prefix, str):
            sender_prefix = bytes.fromhex(sender_prefix) if sender_prefix else b""
        return QueuedMessage(
            sender_key=msg_dict.get("sender_key", b""),
            txt_type=msg_dict.get("txt_type", 0),
            timestamp=msg_dict.get("timestamp", 0),
            text=msg_dict.get("text", ""),
            is_channel=bool(msg_dict.get("is_channel", False)),
            channel_idx=msg_dict.get("channel_idx", 0),
            path_len=msg_dict.get("path_len", 0),
            snr=float(msg_dict.get("snr") or 0.0),
            rssi=int(msg_dict.get("rssi") or 0),
            channel_data_type=int(msg_dict.get("channel_data_type") or 0),
            channel_data_payload=bytes(msg_dict.get("channel_data_payload") or b""),
            sender_prefix=sender_prefix,
        )

    # -----------------------------------------------------------------
    # Non-blocking command overrides (keep event loop responsive)
    # -----------------------------------------------------------------

    async def _cmd_sync_next_message(self, data: bytes) -> None:
        """Sync next message; run persistence read in thread so SQLite does not block."""
        msg = self.bridge.sync_next_message()
        if msg is None:
            msg = await asyncio.to_thread(self._sync_next_from_persistence)
        if msg is None:
            self._write_frame(bytes([RESP_CODE_NO_MORE_MESSAGES]))
            return
        self._write_frame(self._build_message_frame(msg))

    @staticmethod
    def _contact_to_dict(c) -> dict:
        """Convert a Contact object to a persistence dict."""
        pk = c.public_key if isinstance(c.public_key, bytes) else bytes.fromhex(c.public_key)
        raw_advert = getattr(c, "last_advert_packet", None)
        if isinstance(raw_advert, bytearray):
            raw_advert = bytes(raw_advert)
        elif isinstance(raw_advert, str):
            try:
                raw_advert = bytes.fromhex(raw_advert)
            except ValueError:
                raw_advert = None
        elif not isinstance(raw_advert, bytes):
            raw_advert = None
        return {
            "pubkey": pk,
            "name": c.name,
            "adv_type": c.adv_type,
            "flags": c.flags,
            "out_path_len": c.out_path_len,
            "out_path": (
                c.out_path
                if isinstance(c.out_path, bytes)
                else (bytes.fromhex(c.out_path) if c.out_path else b"")
            ),
            "last_advert_timestamp": c.last_advert_timestamp,
            "last_advert_packet": raw_advert,
            "lastmod": c.lastmod,
            "gps_lat": c.gps_lat,
            "gps_lon": c.gps_lon,
            "sync_since": c.sync_since,
        }

    async def _persist_contact(self, contact) -> None:
        """Upsert a single contact to SQLite (non-blocking)."""
        if not self.sqlite_handler:
            return
        contact_dict = self._contact_to_dict(contact)
        await asyncio.to_thread(
            self.sqlite_handler.companion_upsert_contact,
            self.companion_hash,
            contact_dict,
        )

    async def _save_contacts(self) -> None:
        """Persist all contacts to SQLite (non-blocking)."""
        if not self.sqlite_handler:
            return
        contacts = self.bridge.get_contacts()
        dicts = [self._contact_to_dict(c) for c in contacts]
        await asyncio.to_thread(
            self.sqlite_handler.companion_save_contacts,
            self.companion_hash,
            dicts,
        )

    async def _save_channels(self) -> None:
        """Persist channels to SQLite (non-blocking)."""
        if not self.sqlite_handler:
            return
        channels = []
        max_ch = getattr(getattr(self.bridge, "channels", None), "max_channels", 40)
        for idx in range(max_ch):
            ch = self.bridge.get_channel(idx)
            if ch is not None:
                channels.append(
                    {
                        "channel_idx": idx,
                        "name": ch.name,
                        "secret": ch.secret,
                    }
                )
        await asyncio.to_thread(
            self.sqlite_handler.companion_save_channels,
            self.companion_hash,
            channels,
        )

    async def stop(self) -> None:
        """Persist contacts and channels before stopping (so they survive daemon restart)."""
        if self.sqlite_handler:
            try:
                await self._save_contacts()
                await self._save_channels()
            except Exception as e:
                logger.warning("Failed to persist contacts/channels on stop: %s", e)
        await super().stop()
