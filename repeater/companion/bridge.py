"""
Repeater CompanionBridge with SQLite-backed preference persistence.

Persists full NodePrefs as a JSON blob so companion settings (including
auto-add config) survive repeater restarts. Merge-on-load supports
schema evolution when NodePrefs gains or loses fields.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextvars import ContextVar
from enum import Enum
from typing import Any, Awaitable, Callable, Optional
from weakref import WeakValueDictionary

from openhop_core.companion import CompanionBridge
from openhop_core.companion.constants import (
    ADV_TYPE_NONE,
    ADV_TYPE_SENSOR,
    MAX_PENDING_ACK_CRCS,
    NODE_NAME_MAX_BYTES,
    TXT_TYPE_PLAIN,
)
from openhop_core.node.events import MeshEvents
from openhop_core.protocol.constants import MAX_TEXT_LEN, PAYLOAD_TYPE_PATH
from openhop_core.protocol.packet_utils import PathUtils
from openhop_core.util.callbacks import invoke_maybe_awaitable

from repeater.companion.correlation import (
    injected_tx_outcome,
    outbound_send_capture,
)
from repeater.companion.inbound_history import (
    message_dict_from_event,
    persist_inbound_message,
)

logger = logging.getLogger("RepeaterCompanionBridge")

_AMBIGUOUS_ACK_TOKEN = -1
_TRACE_TAG_ATTEMPTS = 64

# Separate from ``outbound_send_capture``: the public REST capture belongs to
# its caller, while this bridge-owned capture lets every send surface one
# semantic event, including sends originating from a frame client.
_message_packet_capture: ContextVar[Optional[dict]] = ContextVar(
    "companion_message_packet_capture", default=None
)
# One opaque token follows a semantic message from packet construction through
# RF correlation, history persistence, and ACK ownership.  CRCs and truncated
# packet hashes are protocol correlation hints, not globally unique request IDs.
_message_send_token: ContextVar[Optional[int]] = ContextVar(
    "companion_message_send_token", default=None
)
# Frame-protocol sends are the default. REST and the legacy operator API set
# this only around their bridge calls, so the shared observer can attribute one
# canonical event without separate bridge methods.
outbound_message_source: ContextVar[str] = ContextVar(
    "companion_outbound_message_source", default="frame"
)
# REST reserves its durable row before RF and publishes that row id here.
# Frame sends leave it unset because main stores their row from message_sent.
outbound_message_id: ContextVar[Optional[int]] = ContextVar(
    "companion_outbound_message_id", default=None
)

# One public preference vocabulary for journal deltas and reset snapshots.
# Radio tuning and secret scope material are intentionally absent.
PUBLIC_PREF_FIELDS = (
    "node_name",
    "adv_type",
    "latitude",
    "longitude",
    "autoadd_config",
    "autoadd_max_hops",
    "path_hash_mode",
    "rx_delay_base",
    "airtime_factor",
    "client_repeat",
    "manual_add_contacts",
    "telemetry_mode_base",
    "telemetry_mode_location",
    "telemetry_mode_environment",
    "advert_loc_policy",
    "multi_acks",
    "default_scope_name",
)

_PERSISTED_PREF_INT_RANGES = {
    "adv_type": (ADV_TYPE_NONE, ADV_TYPE_SENSOR),
    # The virtual companion does not own the shared radio, but these persisted
    # values still need to remain representable by the upstream protocol.
    "tx_power_dbm": (-9, 127),
    "frequency_hz": (100_000_000, 2_500_000_000),
    "bandwidth_hz": (7_000, 500_000),
    "spreading_factor": (5, 12),
    "coding_rate": (5, 8),
    "advert_loc_policy": (0, 0xFF),
    "multi_acks": (0, 0xFF),
    "telemetry_mode_base": (0, 3),
    "telemetry_mode_location": (0, 3),
    "telemetry_mode_environment": (0, 3),
    "manual_add_contacts": (0, 0xFF),
    "autoadd_config": (0, 0xFF),
    "autoadd_max_hops": (0, 64),
    "client_repeat": (0, 0xFF),
    "path_hash_mode": (0, 2),
}
_PERSISTED_PREF_FLOAT_RANGES = {
    "latitude": (-90.0, 90.0),
    "longitude": (-180.0, 180.0),
    # CMD_SET_TUNING_PARAMS carries unsigned milliseconds.
    "rx_delay_base": (0.0, 0xFFFFFFFF / 1000.0),
    "airtime_factor": (0.0, 0xFFFFFFFF / 1000.0),
}
_PERSISTED_PREF_STRING_MAX_BYTES = {
    "node_name": NODE_NAME_MAX_BYTES,
    # CMD_SET_DEFAULT_FLOOD_SCOPE reserves one byte for the terminator.
    "default_scope_name": 30,
}


class ChannelTextCapacityError(ValueError):
    """A REST channel message no longer fits the committed sender name."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = int(max_bytes)
        super().__init__(
            f"text exceeds {self.max_bytes} UTF-8 bytes for the current channel sender name"
        )


def channel_text_capacity(node_name: str) -> int:
    """Return Core's exact UTF-8 channel-text capacity for ``node_name``."""

    prefix = f"{node_name}: ".encode("utf-8")
    return max(0, MAX_TEXT_LEN - len(prefix))


@dataclasses.dataclass(frozen=True)
class OutboundMessageEvent:
    """One text message accepted by the shared radio injector.

    This is deliberately a small transport-neutral record. Frame and REST
    callers both use the same bridge methods, so observers get one canonical
    event without parsing encrypted packets or depending on either API.
    """

    companion_hash: str
    packet_hash: str
    text: str
    timestamp: Optional[int]
    is_channel: bool
    recipient_key: Optional[bytes]
    channel_idx: Optional[int]
    txt_type: int
    expected_ack: Optional[int]
    source: str
    message_id: Optional[int]
    result: Any
    correlation_token: Optional[int] = None
    initial_state: str = "transmitted"


@dataclasses.dataclass(frozen=True)
class SendConfirmedEvent:
    """A MeshCore ACK correlated to a previously emitted outbound message."""

    companion_hash: str
    packet_hash: str
    expected_ack: int
    trip_ms: int
    source: str
    message_id: Optional[int]
    correlation_token: Optional[int] = None


def _prefs_bytes_from_json(value: Any) -> bytes:
    """Restore a ``bytes`` NodePrefs field from JSON (hex string from :func:`_to_json_safe`)."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return b""
        try:
            return bytes.fromhex(s)
        except ValueError:
            # NodePrefs byte fields can contain scope keys. Never echo even a
            # malformed persisted value into logs.
            logger.debug("Invalid hex for prefs bytes field; using an empty value")
            return b""
    return b""


def _to_json_safe(value: Any) -> Any:
    """Convert a value to a JSON-serializable form (avoids TypeError from enums, bytes, etc.)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not valid companion JSON")
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_json_safe(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return value


def _validated_persisted_pref(key: str, value: Any) -> Any:
    """Validate one known NodePrefs value without coercing durable state."""

    if key in _PERSISTED_PREF_INT_RANGES:
        if type(value) is not int:
            raise ValueError(f"persisted preference {key!r} must be an integer")
        low, high = _PERSISTED_PREF_INT_RANGES[key]
        if not low <= value <= high:
            raise ValueError(f"persisted preference {key!r} must be between {low} and {high}")
        return value

    if key in _PERSISTED_PREF_FLOAT_RANGES:
        if type(value) not in (int, float):
            raise ValueError(f"persisted preference {key!r} must be a number")
        parsed = float(value)
        low, high = _PERSISTED_PREF_FLOAT_RANGES[key]
        if not math.isfinite(parsed) or not low <= parsed <= high:
            raise ValueError(
                f"persisted preference {key!r} must be finite and between {low} and {high}"
            )
        return parsed

    if key in _PERSISTED_PREF_STRING_MAX_BYTES:
        if type(value) is not str:
            raise ValueError(f"persisted preference {key!r} must be a string")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError(f"persisted preference {key!r} must contain valid UTF-8") from exc
        max_bytes = _PERSISTED_PREF_STRING_MAX_BYTES[key]
        if size > max_bytes:
            raise ValueError(f"persisted preference {key!r} exceeds {max_bytes} UTF-8 bytes")
        if key == "default_scope_name" and value and value != value.strip():
            raise ValueError(
                "persisted preference 'default_scope_name' must not have "
                "leading or trailing whitespace"
            )
        return value

    if key == "default_scope_key":
        if type(value) is not str:
            raise ValueError("persisted preference 'default_scope_key' must be a hex string")
        if len(value) not in (0, 32):
            raise ValueError("persisted preference 'default_scope_key' must be empty or 16 bytes")
        try:
            return bytes.fromhex(value)
        except ValueError as exc:
            # Scope material is secret. Never include its value in diagnostics.
            raise ValueError("persisted preference 'default_scope_key' must be valid hex") from exc

    raise ValueError(f"no validation rule for persisted preference {key!r}")


class RepeaterCompanionBridge(CompanionBridge):
    """CompanionBridge that persists and loads prefs (full NodePrefs) via SQLite JSON blob."""

    def __init__(
        self,
        identity,
        packet_injector: Callable[..., Any],
        node_name: str = "pyMC",
        adv_type: int = 1,
        max_contacts: int = 1000,
        max_channels: int = 40,
        offline_queue_size: int = 512,
        radio_config: Optional[dict] = None,
        authenticate_callback: Optional[Callable[..., tuple[bool, int]]] = None,
        initial_contacts: Optional[Any] = None,
        radio_settings_getter: Optional[Callable[[], Mapping[str, Any]]] = None,
        max_tx_power_getter: Optional[Callable[[], Optional[int]]] = None,
        *,
        sqlite_handler=None,
        companion_hash: str = "",
        on_prefs_saved: Optional[Callable[[str], None]] = None,
        journal=None,
        tracker=None,
        trace_tag_conflict: Optional[Callable[[object, int], bool]] = None,
    ) -> None:
        self._sqlite_handler = sqlite_handler
        self._companion_hash = companion_hash
        self._on_prefs_saved = on_prefs_saved
        self._journal = journal
        self._persisted_prefs = None
        self._unknown_persisted_prefs: dict[str, Any] = {}
        self._last_prefs_save_error: Optional[Exception] = None
        # RF correlation tracker (design doc §10.4): registers each
        # outbound send so a later heard-repeat can be journaled as
        # message_send_state. Optional/None when correlation isn't wired up.
        self._tracker = tracker
        self._trace_tag_conflict = trace_tag_conflict
        self._trace_waiters: dict[int, dict[str, Any]] = {}
        # Host observers are intentionally separate from openhop-core's
        # connection callbacks. FrameServer clears/rebuilds its callbacks on
        # each reconnect; durable repeater observers must survive that cycle.
        self._observers: dict[str, list[Callable[..., Any]]] = {}
        self._outbound_by_ack: OrderedDict[int, OutboundMessageEvent] = OrderedDict()
        self._ack_tokens_by_crc: OrderedDict[int, int] = OrderedDict()
        self._message_sources_by_token: OrderedDict[int, str] = OrderedDict()
        self._early_confirmations_by_token: OrderedDict[int, tuple[int, int]] = OrderedDict()
        self._local_message_token = 0
        # Snapshot-visible bridge state and its journal commit move together.
        # Frame commands, REST mutations, snapshots, and inbound contact
        # updates all share this event-loop lock.
        self._state_mutation_lock = asyncio.Lock()
        # Contact commits become visible to Frame observers in commit order.
        # The observer may yield while building a push frame; without this
        # narrow lock, a later REST or RF mutation could overtake that frame.
        self._contact_observer_lock = asyncio.Lock()
        self._contact_commit_pending = False
        # Core's login response handler stores the password by one-byte
        # destination hash. Two simultaneous logins to colliding contacts
        # would otherwise overwrite that shared slot. Both Frame and REST
        # enter through _start_login_request, so one lock here protects both
        # transports without adding a second request path.
        self._login_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        # Status and telemetry use Core's same (public key, request tag)
        # callback table. Serialize those request types per exact contact so a
        # Frame request and a parallel REST request can never replace each
        # other's waiter when their timestamp-derived tags match.
        self._protocol_request_locks: WeakValueDictionary[bytes, asyncio.Lock] = (
            WeakValueDictionary()
        )
        # Core owns one repeater-command response waiter per full public key.
        # Serialize only callers targeting that same contact; unrelated
        # repeaters remain concurrent.
        self._repeater_command_locks: WeakValueDictionary[bytes, asyncio.Lock] = (
            WeakValueDictionary()
        )
        super().__init__(
            identity=identity,
            packet_injector=packet_injector,
            node_name=node_name,
            adv_type=adv_type,
            max_contacts=max_contacts,
            max_channels=max_channels,
            offline_queue_size=offline_queue_size,
            radio_config=radio_config,
            authenticate_callback=authenticate_callback,
            initial_contacts=initial_contacts,
            radio_settings_getter=radio_settings_getter,
            max_tx_power_getter=max_tx_power_getter,
        )
        if self._persisted_prefs is None:
            self._persisted_prefs = copy.deepcopy(self.prefs)

    @property
    def state_mutation_lock(self) -> asyncio.Lock:
        """Lock shared by every snapshot-visible state mutation/read."""
        return self._state_mutation_lock

    async def await_committed_state(self) -> None:
        """Wait until any visible state mutation has committed or rolled back.

        The lock is released before RF I/O. Calling an upstream async send
        immediately after this method is safe: its state reads and packet
        construction run synchronously until the transport's first await.
        """
        async with self._state_mutation_lock:
            pass

    @staticmethod
    def _contact_storage_dict(contact) -> dict:
        public_key = contact.public_key
        if not isinstance(public_key, bytes):
            public_key = bytes.fromhex(public_key)
        out_path = contact.out_path
        if not isinstance(out_path, bytes):
            out_path = bytes(out_path or b"")
        raw_advert = getattr(contact, "last_advert_packet", None)
        if isinstance(raw_advert, bytearray):
            raw_advert = bytes(raw_advert)
        elif not isinstance(raw_advert, bytes):
            raw_advert = None
        return {
            "pubkey": public_key,
            "name": contact.name,
            "adv_type": contact.adv_type,
            "flags": contact.flags,
            "out_path_len": contact.out_path_len,
            "out_path": out_path,
            "last_advert_timestamp": contact.last_advert_timestamp,
            "last_advert_packet": raw_advert,
            "lastmod": contact.lastmod,
            "gps_lat": contact.gps_lat if contact.gps_lat is not None else 0.0,
            "gps_lon": contact.gps_lon if contact.gps_lon is not None else 0.0,
            "sync_since": contact.sync_since,
        }

    @staticmethod
    async def _await_blocking_commit(function, *args):
        """Let an already-started local commit finish before cancellation wins."""
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            try:
                await task
            except BaseException:
                raise
            raise cancellation

    @staticmethod
    def _contact_changes(before_contacts, after_contacts) -> list[dict]:
        before = {
            contact.public_key: RepeaterCompanionBridge._contact_storage_dict(contact)
            for contact in before_contacts
            if contact.adv_type != ADV_TYPE_NONE
        }
        after = {
            contact.public_key: RepeaterCompanionBridge._contact_storage_dict(contact)
            for contact in after_contacts
            if contact.adv_type != ADV_TYPE_NONE
        }
        changes = []
        for public_key in sorted(before.keys() - after.keys()):
            changes.append({"change": "remove", "contact": before[public_key]})
        for public_key in sorted(after):
            current = after[public_key]
            previous = before.get(public_key)
            if current == previous:
                continue
            if previous is None:
                change = "new"
            else:
                changed_fields = {
                    field for field in current if current.get(field) != previous.get(field)
                }
                change = "path" if changed_fields <= {"out_path_len", "out_path"} else "update"
            changes.append({"change": change, "contact": current})
        return changes

    def _restore_contact_state(self, contacts, advert_paths) -> None:
        self.contacts.load_from(copy.deepcopy(contacts))
        self.path_cache.clear()
        for advert_path in copy.deepcopy(advert_paths):
            self.path_cache.update(advert_path)

    async def _persist_contact_changes(self, changes: list[dict]) -> None:
        if not changes or self._sqlite_handler is None:
            return
        # A complete before/after diff can remove every old contact and add
        # every new one, so its natural bound is twice this bridge's configured
        # contact capacity. The low-level transaction stays policy-free.
        max_changes = max(2, int(self.contacts.max_contacts) * 2)
        if len(changes) > max_changes:
            raise RuntimeError(f"Contact diff has {len(changes)} changes; maximum is {max_changes}")
        if self._journal is not None:
            await self._await_blocking_commit(
                self._journal.apply_contact_changes,
                changes,
            )
            return
        await self._await_blocking_commit(
            self._sqlite_handler.companion_apply_contact_changes,
            self._companion_hash,
            changes,
        )

    async def _persist_automatic_contact_delete(self, contact_or_key) -> None:
        """Commit a Core contact-eviction callback before transient delivery."""
        public_key = getattr(contact_or_key, "public_key", contact_or_key)
        if isinstance(public_key, bytearray):
            public_key = bytes(public_key)
        elif isinstance(public_key, str):
            try:
                public_key = bytes.fromhex(public_key)
            except ValueError as exc:
                raise ValueError("contact deletion key must be valid hex") from exc
        if not isinstance(public_key, bytes) or len(public_key) < 32:
            raise ValueError("contact deletion key must contain 32 bytes")
        public_key = public_key[:32]
        if self._journal is not None:
            await self._await_blocking_commit(
                self._journal.remove_contact,
                public_key,
            )
            return
        await self._await_blocking_commit(
            self._sqlite_handler.companion_delete_contact,
            self._companion_hash,
            public_key,
        )

    async def _process_contact_packet(self, operation):
        committed_changes = []
        async with self._state_mutation_lock:
            before_contacts = copy.deepcopy(self.contacts.get_all())
            before_paths = copy.deepcopy(self.path_cache.get_all())
            self._contact_commit_pending = True
            try:
                try:
                    result = await operation()
                except BaseException:
                    self._restore_contact_state(before_contacts, before_paths)
                    raise
                after_contacts = copy.deepcopy(self.contacts.get_all())
                committed_changes = self._contact_changes(
                    before_contacts,
                    after_contacts,
                )
                try:
                    await self._persist_contact_changes(committed_changes)
                except Exception:
                    self._restore_contact_state(before_contacts, before_paths)
                    logger.exception(
                        "Inbound contact change rolled back for companion %s",
                        self._companion_hash,
                    )
                    committed_changes = []
            finally:
                self._contact_commit_pending = False

        # Notify only after releasing the state lock. Observers may safely read
        # the committed bridge state without creating a recursive lock path.
        await self.notify_contact_changes(committed_changes)
        return result

    async def process_received_packet(self, packet):
        """Persist only contact-mutating RF packets behind the state guard.

        ACK, advert parsing, message, login, status, and telemetry packets never
        wait on a contact database write. Core publishes an advert's
        ``NODE_DISCOVERED`` event on a deferred task, so the actual advert
        mutation is guarded in :meth:`_handle_mesh_event` below. PATH response
        handling mutates contacts inline and remains guarded here.
        """
        if packet.get_payload_type() != PAYLOAD_TYPE_PATH:
            return await super().process_received_packet(packet)
        process = super().process_received_packet
        return await self._process_contact_packet(lambda: process(packet))

    async def _handle_mesh_event(self, event_type: str, data: dict) -> None:
        """Commit deferred advert mutations before making them observable."""
        handle = super()._handle_mesh_event
        if event_type != MeshEvents.NODE_DISCOVERED:
            await handle(event_type, data)
            return
        await self._process_contact_packet(lambda: handle(event_type, data))

    async def _loopback_imported_advert(self, packet) -> None:
        """Run Frame-imported adverts through the same durable contact boundary."""
        payload = packet.get_payload()
        if len(payload) >= 32 and payload[:32] == self._identity.get_public_key():
            return
        await self.process_received_packet(packet)

    async def _start_login_request(self, pub_key: bytes, password: str) -> dict:
        """Serialize logins that share Core's destination-hash password slot."""

        await self.await_committed_state()
        proxy = self.contacts.get_proxy_by_key(pub_key)
        if proxy is None:
            return await super()._start_login_request(pub_key, password)

        lock = self._login_locks.setdefault(int(proxy.dest_hash), asyncio.Lock())
        await lock.acquire()
        try:
            # A queued login may have waited while a contact mutation ran.
            # Re-check the commit boundary immediately before Core re-reads it.
            await self.await_committed_state()
            started = await super()._start_login_request(pub_key, password)
        except BaseException:
            lock.release()
            raise
        if not started.get("success") or started.get("task") is None:
            lock.release()
            return started

        raw_task = started["task"]

        async def _wait_and_release():
            try:
                return await raw_task
            finally:
                lock.release()

        # Preserve upstream's "start now, await result task later" shape so
        # Frame can emit SENT immediately while the lock remains held until
        # the matching response/timeout has finished.
        wrapped = dict(started)
        wrapped["task"] = self._spawn_background_task(
            _wait_and_release(),
            "serialized login result",
        )
        return wrapped

    async def share_contact(self, pub_key: bytes) -> bool:
        await self.await_committed_state()
        return await super().share_contact(pub_key)

    async def send_binary_req(
        self,
        pub_key: bytes,
        data: bytes,
        timeout_seconds: float = 15.0,
    ):
        await self.await_committed_state()
        return await super().send_binary_req(
            pub_key,
            data,
            timeout_seconds=timeout_seconds,
        )

    async def send_anon_req(
        self,
        pub_key: bytes,
        data: bytes,
        timeout_seconds: float = 15.0,
    ):
        await self.await_committed_state()
        return await super().send_anon_req(
            pub_key,
            data,
            timeout_seconds=timeout_seconds,
        )

    async def send_path_discovery_req(self, pub_key: bytes):
        await self.await_committed_state()
        return await super().send_path_discovery_req(pub_key)

    @staticmethod
    def _response_tag(value: Any) -> int:
        if isinstance(value, (bytes, bytearray)):
            return int.from_bytes(value, "little")
        return int(value)

    async def _send_and_wait_for_response(
        self,
        event_name: str,
        send: Callable[[], Awaitable[Any]],
        *,
        timeout: float,
    ) -> tuple[Any, Optional[tuple[Any, ...]]]:
        """Send one tagged request and await its matching Core response event."""

        responses: asyncio.Queue[tuple[Any, ...]] = asyncio.Queue()

        def capture(*args: Any) -> None:
            responses.put_nowait(args)

        self.add_observer(event_name, capture)
        try:
            sent = await send()
            expected_tag = getattr(sent, "expected_ack", None)
            if not getattr(sent, "success", False) or expected_tag is None:
                return sent, None

            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(0.1, float(timeout))
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return sent, None
                try:
                    response = await asyncio.wait_for(responses.get(), remaining)
                except asyncio.TimeoutError:
                    return sent, None
                if response and self._response_tag(response[0]) == int(expected_tag):
                    return sent, response
        finally:
            self.remove_observer(event_name, capture)

    async def request_anonymous(
        self,
        pub_key: bytes,
        data: bytes,
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Request public metadata without adding the target as a contact."""

        sent, response = await self._send_and_wait_for_response(
            "binary_response",
            lambda: self.send_anon_req(pub_key, data, timeout_seconds=timeout),
            timeout=timeout,
        )
        if not getattr(sent, "success", False):
            return {
                "success": False,
                "error": getattr(sent, "error", None) or "Anonymous request could not be sent",
            }
        if response is None:
            return {"success": False, "error": "Anonymous request timed out"}

        raw_data = bytes(response[1]) if len(response) > 1 else b""
        parsed = response[2] if len(response) > 2 else None
        if not isinstance(parsed, dict):
            parsed = {"raw_hex": raw_data.hex()}
        return {"success": True, "response": parsed}

    @staticmethod
    def _discovered_route(path_len: int, path: bytes) -> dict[str, Any]:
        encoded_length = int(path_len)
        if not PathUtils.is_valid_path_len(encoded_length):
            raise ValueError("Invalid path length in discovery response")

        hash_size = PathUtils.get_path_hash_size(encoded_length)
        hop_count = PathUtils.get_path_hash_count(encoded_length)
        raw_path = bytes(path)
        expected_bytes = hash_size * hop_count
        if len(raw_path) != expected_bytes:
            raise ValueError("Invalid path data in discovery response")

        return {
            "encoded_length": encoded_length,
            "hop_count": hop_count,
            "hash_size": hash_size,
            "hops": [
                raw_path[offset : offset + hash_size].hex()
                for offset in range(0, expected_bytes, hash_size)
            ],
        }

    async def discover_path(
        self,
        pub_key: bytes,
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Actively discover the outbound and return paths for one contact."""

        sent, response = await self._send_and_wait_for_response(
            "path_discovery_response",
            lambda: self.send_path_discovery_req(pub_key),
            timeout=timeout,
        )
        if not getattr(sent, "success", False):
            error = getattr(sent, "error", None)
            return {
                "success": False,
                "error": (
                    "Contact not found"
                    if error == "not_found"
                    else error or "Route discovery failed"
                ),
            }
        if response is None:
            return {"success": False, "error": "Route discovery timed out"}

        try:
            response_key = bytes(response[1])
            if response_key != bytes(pub_key):
                return {"success": False, "error": "Route discovery target did not match"}
            return {
                "success": True,
                "outbound": self._discovered_route(response[2], response[3]),
                "inbound": self._discovered_route(response[4], response[5]),
            }
        except (IndexError, TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    async def send_channel_data(
        self,
        channel_idx: int,
        data_type: int,
        payload: bytes,
        *,
        path: Optional[bytes] = None,
        path_len_encoded: Optional[int] = None,
    ) -> bool:
        await self.await_committed_state()
        return await super().send_channel_data(
            channel_idx,
            data_type,
            payload,
            path=path,
            path_len_encoded=path_len_encoded,
        )

    async def send_raw_data(
        self,
        dest_key: bytes,
        data: bytes,
        path: Optional[bytes] = None,
    ):
        await self.await_committed_state()
        return await super().send_raw_data(dest_key, data, path=path)

    async def send_trace_path(
        self,
        pub_key: bytes,
        tag: int,
        auth_code: int,
        flags: int = 0,
    ) -> bool:
        await self.await_committed_state()
        return await super().send_trace_path(
            pub_key,
            tag,
            auth_code,
            flags=flags,
        )

    def owns_trace_tag(self, tag: int) -> bool:
        return (int(tag) & 0xFFFFFFFF) in self._trace_waiters

    def _allocate_trace_tag(self) -> int:
        for _ in range(_TRACE_TAG_ATTEMPTS):
            tag = secrets.randbits(32)
            if self.owns_trace_tag(tag):
                continue
            conflict = self._trace_tag_conflict
            try:
                if callable(conflict) and conflict(self, tag):
                    continue
            except Exception as exc:
                raise RuntimeError("Could not verify TRACE tag ownership") from exc
            return tag
        raise RuntimeError("No unique TRACE tag is available")

    async def ping_contact(
        self,
        pub_key: bytes,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Send one direct TRACE and await its correlated response."""

        await self.await_committed_state()
        contact = self.contacts.get_by_key(pub_key)
        if contact is None:
            return {"success": False, "error": "Contact not found"}

        path_hash_size = int(getattr(self.prefs, "path_hash_mode", 0)) + 1
        trace_hash_size = 4 if path_hash_size == 3 else path_hash_size
        flags = {1: 0, 2: 1, 4: 2}.get(trace_hash_size)
        if flags is None or len(pub_key) < trace_hash_size:
            return {"success": False, "error": "Unsupported TRACE hash size"}

        tag = self._allocate_trace_tag()
        # Match the Companion TRACE command used by existing clients and
        # deployed MeshCore repeaters. The field is reflected in the response;
        # it is not a per-request authentication mechanism.
        auth_code = 0
        path = bytes(pub_key[:trace_hash_size])
        future = asyncio.get_running_loop().create_future()
        waiter = {
            "future": future,
            "auth_code": auth_code,
            "flags": flags,
            "path": path,
            "started_at": time.monotonic(),
            "trace_hash_size": trace_hash_size,
        }
        self._trace_waiters[tag] = waiter

        try:
            sent = await self.send_trace_path_raw(tag, auth_code, flags, path)
            if not sent.success:
                return {"success": False, "error": sent.error or "TRACE send failed"}
            try:
                return await asyncio.wait_for(future, timeout=max(0.1, float(timeout)))
            except asyncio.TimeoutError:
                return {"success": False, "error": "Ping timed out"}
        finally:
            if self._trace_waiters.get(tag) is waiter:
                self._trace_waiters.pop(tag, None)

    def resolve_trace_ping(self, packet, parsed_data: dict) -> bool:
        """Resolve a pending API ping; return whether this bridge owns the tag."""

        tag = int(parsed_data.get("tag", 0)) & 0xFFFFFFFF
        waiter = self._trace_waiters.get(tag)
        if waiter is None:
            return False

        if (
            int(parsed_data.get("auth_code", -1)) != waiter["auth_code"]
            or int(parsed_data.get("flags", -1)) != waiter["flags"]
            or bytes(parsed_data.get("trace_path_bytes") or b"") != waiter["path"]
        ):
            return True

        rssi = int(getattr(packet, "rssi", 0) or 0)
        if rssi == 0:
            return True

        future = waiter["future"]
        if not future.done():
            future.set_result(
                {
                    "success": True,
                    "snr_db": float(packet.get_snr()),
                    "rssi": rssi,
                    "rtt_ms": max(
                        0,
                        round((time.monotonic() - waiter["started_at"]) * 1000),
                    ),
                    "hop_count": 1,
                    "trace_hop_count": len(waiter["path"]) // waiter["trace_hash_size"],
                    "trace_hash_size": waiter["trace_hash_size"],
                }
            )
        return True

    async def send_logout(self, pub_key: bytes) -> bool:
        """Serialize logout with any login sharing Core's destination slot."""

        await self.await_committed_state()
        proxy = self.contacts.get_proxy_by_key(pub_key)
        if proxy is None:
            self.clear_login_connection(pub_key)
            return await super().send_logout(pub_key)

        lock = self._login_locks.setdefault(int(proxy.dest_hash), asyncio.Lock())
        async with lock:
            await self.await_committed_state()
            # Local session state ends even if the best-effort RF logout
            # cannot be queued. Keep the registry update and packet ordering
            # inside the same lock as login completion.
            self.clear_login_connection(pub_key)
            return await super().send_logout(pub_key)

    async def _start_protocol_request(
        self,
        pub_key: bytes,
        protocol_code: int,
        data: bytes,
        *,
        timeout: float,
        log_label: str,
    ) -> dict:
        key = bytes(pub_key)
        lock = self._protocol_request_locks.setdefault(key, asyncio.Lock())
        await lock.acquire()
        try:
            await self.await_committed_state()
            started = await super()._start_protocol_request(
                key,
                protocol_code,
                data,
                timeout=timeout,
                log_label=log_label,
            )
        except BaseException:
            lock.release()
            raise
        if not started.get("success") or started.get("task") is None:
            lock.release()
            return started

        raw_task = started["task"]

        async def _wait_and_release():
            try:
                return await raw_task
            finally:
                lock.release()

        wrapped = dict(started)
        wrapped["task"] = self._spawn_background_task(
            _wait_and_release(),
            "serialized protocol request result",
        )
        return wrapped

    async def _send_protocol_request(
        self,
        pub_key: bytes,
        protocol_code: int,
        data: bytes,
    ) -> dict:
        await self.await_committed_state()
        return await super()._send_protocol_request(
            pub_key,
            protocol_code,
            data,
        )

    async def send_repeater_command(
        self,
        pub_key: bytes,
        command: str,
        parameters: Optional[str] = None,
    ) -> dict:
        key = bytes(pub_key)
        lock = self._repeater_command_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self.await_committed_state()
            return await super().send_repeater_command(
                key,
                command,
                parameters,
            )

    def add_observer(self, event_name: str, callback: Callable[..., Any]) -> None:
        """Register a durable host observer.

        Core event names (for example ``send_confirmed`` or
        ``channel_updated``) are forwarded unchanged. This subclass also emits
        ``message_sent`` with :class:`OutboundMessageEvent`.
        """
        callbacks = self._observers.setdefault(str(event_name), [])
        if callback not in callbacks:
            callbacks.append(callback)

    def remove_observer(self, event_name: str, callback: Callable[..., Any]) -> None:
        """Remove a previously registered durable host observer."""
        callbacks = self._observers.get(str(event_name), [])
        try:
            callbacks.remove(callback)
        except ValueError:
            return
        if not callbacks:
            self._observers.pop(str(event_name), None)

    async def _notify_observers(self, event_name: str, *args: Any) -> None:
        """Notify a stable snapshot so callbacks may register/remove safely."""
        if event_name == "contact_committed":
            async with self._contact_observer_lock:
                await self._notify_observers_unlocked(event_name, *args)
            return
        await self._notify_observers_unlocked(event_name, *args)

    async def _notify_observers_unlocked(self, event_name: str, *args: Any) -> None:
        """Run observers; contact commit ordering is applied by the caller."""
        cancellation = None
        for callback in tuple(self._observers.get(event_name, ())):
            try:
                await invoke_maybe_awaitable(callback, *args)
            except asyncio.CancelledError as exc:
                # A durable observer may finish its own reconciliation before
                # surfacing cancellation. Do not make observer ordering decide
                # which other host components see the committed event.
                if cancellation is None:
                    cancellation = exc
            except Exception:
                logger.exception("Durable %s observer failed", event_name)
        if cancellation is not None:
            raise cancellation

    async def notify_observers(self, event_name: str, *args: Any) -> None:
        """Emit a repeater-owned semantic event to durable observers."""
        await self._notify_observers(str(event_name), *args)

    async def notify_contact_changes(self, changes: list[dict]) -> None:
        """Publish one committed contact transaction in its commit order."""

        cancellation = None
        async with self._contact_observer_lock:
            for change in changes:
                try:
                    await self._notify_observers_unlocked(
                        "contact_committed",
                        change["change"],
                        change["contact"],
                    )
                except asyncio.CancelledError as exc:
                    # The state and journal transaction already committed.
                    # Finish publishing the batch before cancellation escapes.
                    if cancellation is None:
                        cancellation = exc
        if cancellation is not None:
            raise cancellation

    async def _fire_callbacks(self, event_name: str, *args: Any) -> None:
        """Forward core events to transient clients and durable host observers."""
        if (
            event_name in {"message_event", "channel_message_event", "channel_data_event"}
            and args
            and self._sqlite_handler is not None
        ):
            event = args[0]
            try:
                await persist_inbound_message(
                    bridge=self,
                    sqlite_handler=self._sqlite_handler,
                    companion_hash=self._companion_hash,
                    msg_dict=message_dict_from_event(event_name, event),
                    queue_entry=event.queue_entry if event.queued else None,
                    journal=self._journal,
                    tracker=self._tracker,
                )
            except asyncio.CancelledError:
                # The helper reconciles an already-started worker before
                # cancellation escapes. Do not publish an uncertain event.
                raise
            except Exception:
                # Core isolates callback failures. Preserve that receive-path
                # contract while keeping the exact queued entry for retry.
                logger.exception(
                    "Inbound companion %s persistence failed; suppressing "
                    "transient delivery for %s",
                    self._companion_hash,
                    event_name,
                )
                return
        if (
            event_name == "contact_deleted"
            and args
            and self._sqlite_handler is not None
            and not self._contact_commit_pending
        ):
            try:
                await self._persist_automatic_contact_delete(args[0])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Inbound companion %s contact deletion persistence failed; "
                    "suppressing transient delivery",
                    self._companion_hash,
                )
                return
        if event_name != "send_confirmed" or not args:
            await super()._fire_callbacks(event_name, *args)
            await self._notify_observers(event_name, *args)
            return

        try:
            expected_ack = int(args[0])
            trip_ms = int(args[1]) if len(args) > 1 else 0
        except (TypeError, ValueError):
            # Ownership is unknowable, so preserve host observability while
            # withholding a false confirmation from a Frame client.
            cancellation = None
            try:
                await self._fire_owned_send_confirmed_callbacks(
                    *args,
                    allow_frame=False,
                )
            except asyncio.CancelledError as exc:
                cancellation = exc
            try:
                await self._notify_observers(event_name, *args)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
            if cancellation is not None:
                raise cancellation
            return

        correlation_token = self._ack_tokens_by_crc.pop(expected_ack, None)
        outbound = self._outbound_by_ack.pop(expected_ack, None)
        ambiguous = correlation_token == _AMBIGUOUS_ACK_TOKEN
        source = outbound.source if outbound is not None else None
        if source is None and correlation_token is not None and not ambiguous:
            source = self._message_sources_by_token.get(correlation_token)
        if correlation_token is not None and not ambiguous:
            self._message_sources_by_token.pop(correlation_token, None)

        # Only the Frame transport that owns this exact semantic send receives
        # Core's PUSH_CODE_SEND_CONFIRMED callback. Operator/v1 ACKs remain
        # visible to their host observers and durable history without leaking
        # into a parallel Frame client's protocol stream.
        cancellation = None
        try:
            await self._fire_owned_send_confirmed_callbacks(
                *args,
                allow_frame=not ambiguous and source == "frame",
            )
        except asyncio.CancelledError as exc:
            cancellation = exc
        try:
            await self._notify_observers(event_name, *args)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc

        if ambiguous:
            logger.warning(
                "Companion %s: ACK %s ownership is ambiguous; history confirmation suppressed",
                self._companion_hash,
                expected_ack,
            )
            if cancellation is not None:
                raise cancellation
            return
        if outbound is not None:
            try:
                await self._notify_observers(
                    "message_confirmed",
                    SendConfirmedEvent(
                        companion_hash=outbound.companion_hash,
                        packet_hash=outbound.packet_hash,
                        expected_ack=expected_ack,
                        trip_ms=trip_ms,
                        source=outbound.source,
                        message_id=outbound.message_id,
                        correlation_token=(
                            outbound.correlation_token
                            if outbound.correlation_token is not None
                            else correlation_token
                        ),
                    ),
                )
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
        elif correlation_token is not None:
            # The ACK can race packet_injector's post-TX echo/queue work.
            # Keep it by the exact send token until the semantic message
            # event exists; never attach it to a future send reusing a CRC.
            self._early_confirmations_by_token.pop(correlation_token, None)
            self._early_confirmations_by_token[correlation_token] = (
                expected_ack,
                trip_ms,
            )
            while len(self._early_confirmations_by_token) > MAX_PENDING_ACK_CRCS:
                self._early_confirmations_by_token.popitem(last=False)
        if cancellation is not None:
            raise cancellation

    @property
    def manages_inbound_history(self) -> bool:
        """Whether this bridge commits inbound history before push callbacks."""
        return self._sqlite_handler is not None

    @property
    def manages_contact_history(self) -> bool:
        """Whether this bridge commits inbound contacts before push callbacks."""
        return self._sqlite_handler is not None

    async def _fire_owned_send_confirmed_callbacks(
        self,
        *args: Any,
        allow_frame: bool,
    ) -> None:
        """Emit a raw ACK without leaking another API's send into Frame."""

        for callback in tuple(self._push_callbacks.get("send_confirmed", ())):
            if not allow_frame and self._is_frame_server_callback(callback):
                continue
            try:
                await invoke_maybe_awaitable(callback, *args)
            except Exception as exc:
                logger.error("Error in send_confirmed callback: %s", exc)

    def clear_push_callbacks(self) -> None:
        """Remove stale frame-server callbacks while preserving host callbacks.

        Upstream rebuilds frame callbacks for every TCP connection. Its base
        implementation clears *all* callbacks, which also removes independent
        repeater/API listeners sharing this bridge. A frame callback is a bound
        method owned by a server connected to this exact bridge; everything
        else is host-owned and remains registered.
        """
        for callbacks in self._push_callbacks.values():
            callbacks[:] = [
                callback for callback in callbacks if not self._is_frame_server_callback(callback)
            ]

    def remove_frame_server_callbacks(self, server) -> None:
        """Detach callbacks owned by one stopped Frame transport."""
        for callbacks in self._push_callbacks.values():
            callbacks[:] = [
                callback
                for callback in callbacks
                if getattr(callback, "__self__", None) is not server
            ]

    def _is_frame_server_callback(self, callback: Callable[..., Any]) -> bool:
        owner = getattr(callback, "__self__", None)
        return (
            owner is not None
            and getattr(owner, "bridge", None) is self
            and callable(getattr(owner, "_setup_push_callbacks", None))
        )

    def _new_message_token(self) -> int:
        tracker = self._tracker
        reserve = getattr(tracker, "new_registration_token", None)
        if callable(reserve):
            token = int(reserve())
        else:
            self._local_message_token += 1
            token = self._local_message_token
        source = outbound_message_source.get()
        if source not in {"frame", "rest", "operator"}:
            source = "frame"
        self._message_sources_by_token.pop(token, None)
        self._message_sources_by_token[token] = source
        while len(self._message_sources_by_token) > MAX_PENDING_ACK_CRCS:
            self._message_sources_by_token.popitem(last=False)
        return token

    def _discard_message_send(
        self,
        correlation_token: Optional[int],
        expected_ack: Optional[int] = None,
    ) -> None:
        if correlation_token is None:
            return
        tracker = self._tracker
        discard = getattr(tracker, "discard_registration", None)
        if callable(discard):
            discard(correlation_token)
        self._message_sources_by_token.pop(int(correlation_token), None)
        self._early_confirmations_by_token.pop(int(correlation_token), None)
        for crc, token in tuple(self._ack_tokens_by_crc.items()):
            if token != int(correlation_token):
                continue
            self._ack_tokens_by_crc.pop(crc, None)
            self._outbound_by_ack.pop(crc, None)
            self._pending_ack_crcs.pop(crc, None)
        if expected_ack is not None:
            crc = int(expected_ack)
            if self._ack_tokens_by_crc.get(crc) == int(correlation_token):
                self._ack_tokens_by_crc.pop(crc, None)
                self._outbound_by_ack.pop(crc, None)
                self._pending_ack_crcs.pop(crc, None)

    def _track_pending_ack(self, ack_crc: int) -> None:
        """Track Core's ACK CRC and bind it to this exact semantic send."""

        super()._track_pending_ack(ack_crc)
        correlation_token = _message_send_token.get()
        if correlation_token is None:
            return
        crc = int(ack_crc)
        previous_token = self._ack_tokens_by_crc.pop(crc, None)
        if previous_token is not None and previous_token != int(correlation_token):
            self._ack_tokens_by_crc[crc] = _AMBIGUOUS_ACK_TOKEN
            self._outbound_by_ack.pop(crc, None)
            if previous_token != _AMBIGUOUS_ACK_TOKEN:
                self._message_sources_by_token.pop(previous_token, None)
            self._message_sources_by_token.pop(int(correlation_token), None)
            logger.warning(
                "Companion %s: concurrent sends reused ACK CRC %s; "
                "history ownership will fail closed",
                self._companion_hash,
                crc,
            )
        else:
            self._ack_tokens_by_crc[crc] = int(correlation_token)
        holder = _message_packet_capture.get()
        if holder is not None:
            holder["expected_ack"] = crc
        while len(self._ack_tokens_by_crc) > MAX_PENDING_ACK_CRCS:
            old_crc, old_token = self._ack_tokens_by_crc.popitem(last=False)
            self._outbound_by_ack.pop(old_crc, None)
            if old_token != _AMBIGUOUS_ACK_TOKEN:
                self._message_sources_by_token.pop(old_token, None)

    async def _send_packet(self, pkt, wait_for_ack: bool = False, expected_crc=None) -> bool:
        """Send via the shared injector and publish the packet hash to callers.

        A semantic message is provisionally registered before the injector is
        awaited.  The injector includes the actual radio call plus small
        post-TX echo/queue awaits, so registering afterward can miss an
        immediate OTA repeat. Login/status/telemetry calls have no message
        token and remain outside chat history.
        """
        holder = _message_packet_capture.get()
        correlation_token = _message_send_token.get()
        capture = outbound_send_capture.get()
        packet_hash = None
        if holder is not None or capture is not None:
            try:
                packet_hash = pkt.calculate_packet_hash().hex().upper()
            except Exception as e:
                logger.debug("Could not compute packet hash for correlation: %s", e)
        if (
            holder is not None
            and correlation_token is not None
            and packet_hash
            and self._tracker is not None
            and self._companion_hash
        ):
            registered = self._tracker.register_outbound(
                packet_hash,
                self._companion_hash,
                outbound_message_id.get(),
                registration_token=correlation_token,
            )
            holder["correlation_registered"] = registered is not None

        tx_outcome: dict[str, bool] = {}
        outcome_context = injected_tx_outcome.set(tx_outcome)

        def _publish_send_outcome(initial_state: str) -> None:
            expected_ack = holder.get("expected_ack") if holder is not None else expected_crc
            if holder is not None:
                holder["initial_state"] = initial_state
                if packet_hash and initial_state != "failed":
                    holder["hash"] = packet_hash
            if capture is not None:
                capture["initial_state"] = initial_state
                if expected_ack is not None:
                    capture["expected_ack"] = expected_ack
                if packet_hash and initial_state != "failed":
                    capture["hash"] = packet_hash

        try:
            try:
                if expected_crc is None:
                    sent = await self._packet_injector(
                        pkt,
                        wait_for_ack=wait_for_ack,
                    )
                else:
                    sent = await self._packet_injector(
                        pkt,
                        wait_for_ack=wait_for_ack,
                        expected_crc=expected_crc,
                    )
            except BaseException:
                if tx_outcome.get("accepted") is True:
                    initial_state = "transmitted"
                elif tx_outcome.get("uncertain") is True:
                    initial_state = "indeterminate"
                elif tx_outcome.get("accepted") is False:
                    initial_state = "failed"
                else:
                    initial_state = "indeterminate"
                _publish_send_outcome(initial_state)
                raise
        finally:
            injected_tx_outcome.reset(outcome_context)

        tx_accepted = tx_outcome.get("accepted")
        if sent:
            initial_state = "transmitted"
        elif tx_accepted is True:
            # In particular, legacy wait-for-ACK can return False after a
            # successful TX when the ACK times out.
            initial_state = "transmitted"
        elif tx_outcome.get("uncertain") is True:
            initial_state = "indeterminate"
        elif tx_accepted is False:
            initial_state = "failed"
        elif wait_for_ack:
            # Third-party injectors may not expose whether False means local
            # rejection or ACK timeout. Never erase potentially-on-air work.
            initial_state = "indeterminate"
        else:
            initial_state = "failed"

        _publish_send_outcome(initial_state)

        if initial_state == "failed":
            self._discard_message_send(
                correlation_token,
                holder.get("expected_ack") if holder is not None else expected_crc,
            )
        return sent

    async def _record_outbound_message(self, event: OutboundMessageEvent) -> None:
        """Register RF correlation and emit one canonical outbound event."""
        event_token = event.correlation_token
        if event_token is not None:
            token = int(event_token)
            self._message_sources_by_token.pop(token, None)
            self._message_sources_by_token[token] = event.source
            while len(self._message_sources_by_token) > MAX_PENDING_ACK_CRCS:
                self._message_sources_by_token.popitem(last=False)
        if event.correlation_token is None and self._tracker is not None and self._companion_hash:
            self._tracker.register_outbound(
                event.packet_hash,
                self._companion_hash,
                event.message_id,
            )
        if event.expected_ack is not None:
            expected_ack = int(event.expected_ack)
            prior_event = self._outbound_by_ack.get(expected_ack)
            prior_token = self._ack_tokens_by_crc.get(expected_ack)
            collision = (
                prior_token == _AMBIGUOUS_ACK_TOKEN
                or (
                    prior_token is not None
                    and event_token is not None
                    and prior_token != int(event_token)
                )
                or (prior_event is not None and prior_event is not event)
            )
            if collision:
                if prior_event is not None and prior_event.correlation_token is not None:
                    self._message_sources_by_token.pop(
                        int(prior_event.correlation_token),
                        None,
                    )
                if event_token is not None:
                    self._message_sources_by_token.pop(int(event_token), None)
                self._outbound_by_ack.pop(expected_ack, None)
                self._ack_tokens_by_crc.pop(expected_ack, None)
                self._ack_tokens_by_crc[expected_ack] = _AMBIGUOUS_ACK_TOKEN
            else:
                self._outbound_by_ack.pop(expected_ack, None)
                self._outbound_by_ack[expected_ack] = event
                if event_token is not None:
                    self._ack_tokens_by_crc.pop(expected_ack, None)
                    self._ack_tokens_by_crc[expected_ack] = int(event_token)
            while len(self._outbound_by_ack) > MAX_PENDING_ACK_CRCS:
                old_crc, old_event = self._outbound_by_ack.popitem(last=False)
                if old_event.correlation_token is not None:
                    self._message_sources_by_token.pop(
                        int(old_event.correlation_token),
                        None,
                    )
                if (
                    old_event.correlation_token is None
                    or self._ack_tokens_by_crc.get(old_crc) == old_event.correlation_token
                ):
                    self._ack_tokens_by_crc.pop(old_crc, None)
        early_confirmation = (
            self._early_confirmations_by_token.get(
                int(event_token),
                None,
            )
            if event_token is not None
            else None
        )
        cancellation = None
        try:
            await self._notify_observers("message_sent", event)
        except asyncio.CancelledError as exc:
            # The durable observer finishes its SQLite reconciliation before
            # surfacing cancellation.  Complete an ACK that raced ahead too,
            # then propagate cancellation to the original caller.
            cancellation = exc
        if (
            early_confirmation is not None
            and event_token is not None
            and event.expected_ack is not None
            and self._outbound_by_ack.get(int(event.expected_ack)) is event
        ):
            self._early_confirmations_by_token.pop(
                int(event_token),
                None,
            )
            expected_ack, trip_ms = early_confirmation
            self._outbound_by_ack.pop(int(event.expected_ack), None)
            self._ack_tokens_by_crc.pop(int(event.expected_ack), None)
            self._message_sources_by_token.pop(
                int(event_token),
                None,
            )
            await self._notify_observers(
                "message_confirmed",
                SendConfirmedEvent(
                    companion_hash=event.companion_hash,
                    packet_hash=event.packet_hash,
                    expected_ack=expected_ack,
                    trip_ms=trip_ms,
                    source=event.source,
                    message_id=event.message_id,
                    correlation_token=event.correlation_token,
                ),
            )
        elif event.expected_ack is None and event.correlation_token is not None:
            self._message_sources_by_token.pop(
                int(event.correlation_token),
                None,
            )
        if cancellation is not None:
            raise cancellation

    async def send_text_message(
        self,
        pub_key: bytes,
        text: str,
        txt_type: int = TXT_TYPE_PLAIN,
        attempt: int = 1,
        wait_for_ack: bool = True,
        timestamp: Optional[int] = None,
    ):
        """Send a direct message and emit ``message_sent`` after RF acceptance."""
        await self.await_committed_state()
        correlation_token = self._new_message_token()
        holder: dict[str, Any] = {}
        capture_token = _message_packet_capture.set(holder)
        send_token = _message_send_token.set(correlation_token)
        result = None
        send_error = None
        try:
            try:
                result = await super().send_text_message(
                    pub_key,
                    text,
                    txt_type=txt_type,
                    attempt=attempt,
                    wait_for_ack=wait_for_ack,
                    timestamp=timestamp,
                )
            except BaseException as exc:
                send_error = exc

            packet_hash = holder.get("hash")
            initial_state = holder.get("initial_state")
            if (
                isinstance(packet_hash, str)
                and packet_hash
                and initial_state in {"transmitted", "indeterminate"}
            ):
                source = outbound_message_source.get()
                if source not in {"frame", "rest", "operator"}:
                    logger.warning(
                        "Unknown companion message source %r; using frame",
                        source,
                    )
                    source = "frame"
                await self._record_outbound_message(
                    OutboundMessageEvent(
                        companion_hash=str(self._companion_hash),
                        packet_hash=packet_hash,
                        text=text,
                        timestamp=timestamp,
                        is_channel=False,
                        recipient_key=bytes(pub_key),
                        channel_idx=None,
                        txt_type=int(txt_type),
                        expected_ack=(
                            getattr(result, "expected_ack", None)
                            if result is not None
                            else holder.get("expected_ack")
                        ),
                        source=source,
                        message_id=outbound_message_id.get(),
                        result=result,
                        correlation_token=correlation_token,
                        initial_state=str(initial_state),
                    )
                )
            elif not holder.get("correlation_registered"):
                # No packet was built/registered. Clear any ACK token installed
                # before a builder or transport failure surfaced.
                self._discard_message_send(
                    correlation_token,
                    holder.get("expected_ack"),
                )

            if send_error is not None:
                raise send_error
            return result
        finally:
            _message_send_token.reset(send_token)
            _message_packet_capture.reset(capture_token)

    async def send_channel_message(
        self, channel_idx: int, text: str, timestamp: Optional[int] = None
    ) -> bool:
        """Send a channel message and emit ``message_sent`` after RF acceptance."""
        await self.await_committed_state()
        if outbound_message_source.get() in {"rest", "operator"}:
            # Keep this check adjacent to Core's packet builder. There is no
            # await between here and Core reading prefs.node_name, so a Frame
            # rename cannot make REST validate one prefix and transmit a
            # truncated message with another.
            max_bytes = channel_text_capacity(self.prefs.node_name)
            try:
                text_bytes = len(text.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ChannelTextCapacityError(max_bytes) from exc
            if text_bytes > max_bytes:
                raise ChannelTextCapacityError(max_bytes)
        correlation_token = self._new_message_token()
        holder: dict[str, Any] = {}
        capture_token = _message_packet_capture.set(holder)
        send_token = _message_send_token.set(correlation_token)
        sent = None
        send_error = None
        try:
            try:
                sent = await super().send_channel_message(
                    channel_idx,
                    text,
                    timestamp=timestamp,
                )
            except BaseException as exc:
                send_error = exc

            packet_hash = holder.get("hash")
            initial_state = holder.get("initial_state")
            if (
                isinstance(packet_hash, str)
                and packet_hash
                and initial_state in {"transmitted", "indeterminate"}
            ):
                source = outbound_message_source.get()
                if source not in {"frame", "rest", "operator"}:
                    logger.warning(
                        "Unknown companion message source %r; using frame",
                        source,
                    )
                    source = "frame"
                await self._record_outbound_message(
                    OutboundMessageEvent(
                        companion_hash=str(self._companion_hash),
                        packet_hash=packet_hash,
                        text=text,
                        timestamp=timestamp,
                        is_channel=True,
                        recipient_key=None,
                        channel_idx=int(channel_idx),
                        txt_type=TXT_TYPE_PLAIN,
                        expected_ack=None,
                        source=source,
                        message_id=outbound_message_id.get(),
                        result=sent,
                        correlation_token=correlation_token,
                        initial_state=str(initial_state),
                    )
                )
            elif not holder.get("correlation_registered"):
                self._discard_message_send(correlation_token)

            if send_error is not None:
                raise send_error
            return bool(sent)
        finally:
            _message_send_token.reset(send_token)
            _message_packet_capture.reset(capture_token)

    def _save_prefs(self) -> None:
        """Persist full prefs and their public sync event atomically."""
        if not self._sqlite_handler or not self._companion_hash:
            return
        previous = getattr(self, "_persisted_prefs", None)
        storage_committed = False
        prefs_safe: dict[str, Any] = {}
        previous_safe: dict[str, Any] = {}
        public_fields: tuple[str, ...] = ()
        try:
            prefs_dict = dataclasses.asdict(self.prefs)
            current_known = _to_json_safe(prefs_dict)
            previous_known = (
                _to_json_safe(dataclasses.asdict(previous)) if previous is not None else {}
            )
            # Ignore unknown future fields at runtime, but do not destroy them
            # when this older process later saves a known preference.
            unknown = copy.deepcopy(getattr(self, "_unknown_persisted_prefs", {}))
            prefs_safe = {**unknown, **current_known}
            previous_safe = {**unknown, **previous_known}
            if prefs_safe == previous_safe:
                return

            changed_public = {
                field: prefs_safe.get(field)
                for field in PUBLIC_PREF_FIELDS
                if prefs_safe.get(field) != previous_safe.get(field)
            }
            public_fields = tuple(changed_public)
            journal = getattr(self, "_journal", None)
            if journal is not None:
                journal.store_prefs(prefs_safe, changed_public)
            elif not self._sqlite_handler.companion_save_prefs(
                str(self._companion_hash),
                prefs_safe,
            ):
                raise RuntimeError("storage rejected the preferences write")
            storage_committed = True

            old_node_name = getattr(previous, "node_name", None) if previous is not None else None
            if self._on_prefs_saved and self.prefs.node_name != old_node_name:
                self._on_prefs_saved(self.prefs.node_name)
            self._persisted_prefs = copy.deepcopy(self.prefs)
            self._last_prefs_save_error = None
        except Exception as e:
            if storage_committed and previous is not None:
                reverted_public = {
                    field: previous_safe.get(field)
                    for field in public_fields
                    if prefs_safe.get(field) != previous_safe.get(field)
                }
                try:
                    journal = getattr(self, "_journal", None)
                    if journal is not None:
                        journal.store_prefs(previous_safe, reverted_public)
                    elif not self._sqlite_handler.companion_save_prefs(
                        str(self._companion_hash),
                        previous_safe,
                    ):
                        raise RuntimeError("storage rejected the preference compensation")
                except Exception:
                    logger.exception(
                        "Failed to compensate companion prefs after config sync failure"
                    )
            if previous is not None:
                for pref_field in dataclasses.fields(previous):
                    setattr(
                        self.prefs,
                        pref_field.name,
                        copy.deepcopy(getattr(previous, pref_field.name)),
                    )
                self._persisted_prefs = copy.deepcopy(previous)
            self._last_prefs_save_error = e
            logger.warning("Failed to persist companion prefs: %s", e)

    def clear_prefs_save_error(self) -> None:
        """Clear an earlier non-command failure before a new Frame command."""
        self._last_prefs_save_error = None

    def consume_prefs_save_error(self):
        """Return and clear the current Frame preference durability error."""
        error = self._last_prefs_save_error
        self._last_prefs_save_error = None
        return error

    def _load_prefs(self) -> None:
        """Load prefs from SQLite JSON and merge into self.prefs (only known keys)."""
        if not self._sqlite_handler or not self._companion_hash:
            return
        try:
            stored = self._sqlite_handler.companion_load_prefs(self._companion_hash)
            if stored is not None and not isinstance(stored, dict):
                raise ValueError("persisted companion prefs are not a JSON object")
            if stored:
                unknown = {}
                for key, value in stored.items():
                    if not hasattr(self.prefs, key):
                        unknown[key] = copy.deepcopy(value)
                        continue
                    setattr(
                        self.prefs,
                        key,
                        _validated_persisted_pref(key, value),
                    )

                scope_name = self.prefs.default_scope_name
                scope_key = self.prefs.default_scope_key
                if bool(scope_name) != bool(scope_key):
                    raise ValueError(
                        "persisted default scope name and key must both be set or both be empty"
                    )
                if scope_key and len(scope_key) != 16:
                    raise ValueError(
                        "persisted preference 'default_scope_key' must be exactly 16 bytes when set"
                    )
                self._unknown_persisted_prefs = unknown
            self._persisted_prefs = copy.deepcopy(self.prefs)
        except Exception as e:
            logger.error(
                "Refusing companion activation because persisted prefs could not be loaded: %s",
                e,
            )
            raise
