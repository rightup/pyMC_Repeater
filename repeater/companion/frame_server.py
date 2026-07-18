"""
Repeater-specific CompanionFrameServer with SQLite persistence.

Thin subclass of :class:`openhop_core.companion.frame_server.CompanionFrameServer`
that adds SQLite-backed message, contact, and channel persistence via a
``sqlite_handler`` dependency.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from openhop_core.companion.constants import RESP_CODE_NO_MORE_MESSAGES
from openhop_core.companion.frame_server import CompanionFrameServer as _BaseFrameServer
from openhop_core.companion.models import QueuedMessage

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
        *,
        journal=None,
        tracker=None,
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
        self.journal = journal
        # RF correlation tracker (design doc §10.4): registers each freshly
        # persisted inbound message so a later duplicate reception can be
        # journaled as message_reception. Optional/None when correlation
        # isn't wired up (e.g. some tests construct a frame server directly).
        self.tracker = tracker

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

    async def _persist_companion_message(self, msg_dict: dict) -> None:
        """Persist message to SQLite and pop from bridge queue.

        The bridge's ``offline_queue_size`` (``message_queue.max_size``) doubles
        as the SQLite retention limit: 0 disables offline storage entirely, so the
        message is dropped instead of persisted.
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
            self.bridge.message_queue.pop_last()
            return
        persisted = await asyncio.to_thread(
            self.sqlite_handler.companion_push_message,
            self.companion_hash,
            msg_dict,
            retention,
        )
        if persisted:
            self.bridge.message_queue.pop_last()
            if self.journal is not None:
                await asyncio.to_thread(self.journal.record_message, msg_dict)
            tracker = getattr(self, "tracker", None)
            packet_hash = msg_dict.get("packet_hash")
            if tracker is not None and packet_hash:
                message_id = await asyncio.to_thread(
                    self.sqlite_handler.companion_get_message_id, self.companion_hash, packet_hash
                )
                tracker.register_inbound(packet_hash, self.companion_hash, message_id)
        else:
            logger.debug(
                "Companion %s: retaining message in memory after SQLite queue rejection",
                self.companion_hash,
            )

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
        if self.journal is not None:
            await asyncio.to_thread(self.journal.record_contact, contact_dict, "update")

    async def _save_contacts(self) -> None:
        """Persist all contacts to SQLite (non-blocking).

        Bulk stop-time save: not journaled (would spam the journal with the
        entire contact set on every restart). Channel/bulk journal events are
        deferred to phase 2.
        """
        if not self.sqlite_handler:
            return
        contacts = self.bridge.get_contacts()
        dicts = [self._contact_to_dict(c) for c in contacts]
        await asyncio.to_thread(
            self.sqlite_handler.companion_save_contacts,
            self.companion_hash,
            dicts,
        )

    def _channel_state(self, idx) -> Optional[dict]:
        """Name-only view of one channel slot, or None when unset.

        Secrets are excluded on purpose — see ``journal.record_channel``.
        """
        if idx is None:
            return None
        ch = self.bridge.get_channel(idx)
        return None if ch is None else {"index": idx, "name": ch.name}

    async def _cmd_set_channel(self, data: bytes) -> None:
        """Journal a channel change on top of the core handler.

        The bulk ``_save_channels`` path stays unjournaled (it also runs at
        stop time, which would replay the whole table on every restart), so
        the single-slot event is emitted here instead — mirroring how
        ``_persist_contact`` journals one contact while ``_save_contacts``
        does not.

        Before/after comparison rather than trusting the command to have
        succeeded: a rejected or no-op SET_CHANNEL must not produce an event.
        """
        idx = data[0] if data else None
        before = self._channel_state(idx)
        await super()._cmd_set_channel(data)
        after = self._channel_state(idx)

        if self.journal is None or after == before:
            return
        if after is None:
            await asyncio.to_thread(self.journal.record_channel, idx, None, "remove")
        else:
            await asyncio.to_thread(
                self.journal.record_channel, idx, after["name"], "update"
            )

    async def _save_channels(self) -> None:
        """Persist channels to SQLite (non-blocking).

        Bulk stop-time save: not journaled (it also runs on shutdown, which
        would replay the entire channel set into the journal on every
        restart). Single-slot changes are journaled in ``_cmd_set_channel``.
        """
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
