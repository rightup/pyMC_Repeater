"""
Repeater-specific CompanionFrameServer with SQLite persistence.

Thin subclass of :class:`openhop_core.companion.frame_server.CompanionFrameServer`
that adds SQLite-backed message, contact, and channel persistence via a
``sqlite_handler`` dependency.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import logging
import struct
import time
from collections import OrderedDict
from contextvars import ContextVar
from typing import Optional

from openhop_core.companion.constants import (
    ERR_CODE_FILE_IO_ERROR,
    ERR_CODE_ILLEGAL_ARG,
    ERR_CODE_TABLE_FULL,
    ERR_CODE_UNSUPPORTED_CMD,
    MAX_PATH_SIZE,
    MAX_PENDING_ACK_CRCS,
    PUB_KEY_SIZE,
    PUSH_CODE_PATH_UPDATED,
    RESP_CODE_NO_MORE_MESSAGES,
)
from openhop_core.companion.frame_server import CompanionFrameServer as _BaseFrameServer
from openhop_core.companion.models import QueuedMessage
from openhop_core.protocol.packet_utils import PathUtils

from repeater.companion.inbound_history import (
    persist_inbound_message,
    remove_queue_entry,
)
from repeater.companion.utils import (
    DEFAULT_COMPANION_TCP_PORT,
    DEFAULT_COMPANION_TCP_TIMEOUT_SEC,
)

logger = logging.getLogger("CompanionFrameServer")

_AMBIGUOUS_SESSION = object()
_RETIRED_SESSION = object()
_frame_command_session: ContextVar[Optional[object]] = ContextVar(
    "companion_frame_command_session",
    default=None,
)
_sent_response_kind: ContextVar[Optional[str]] = ContextVar(
    "companion_frame_sent_response_kind",
    default=None,
)
_CONTROL_RESPONSE_WINDOW_SECONDS = 60.0


class _AsyncNullContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _is_loopback_bind_address(value: str) -> bool:
    """Return whether a listener address is unambiguously loopback-only."""
    address = str(value or "").strip()
    if address.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        # Hostnames can change resolution; only localhost is safely assumed.
        return False


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
        port: int = DEFAULT_COMPANION_TCP_PORT,
        bind_address: str = "127.0.0.1",
        client_idle_timeout_sec: Optional[int] = DEFAULT_COMPANION_TCP_TIMEOUT_SEC,
        sqlite_handler=None,
        local_hash: Optional[int] = None,
        stats_getter=None,
        control_handler=None,
        *,
        device_model: str = "openHop-Repeater-Companion",
        device_version: Optional[str] = None,
        build_date: str = "",
        heartbeat_interval: int = 15,
        journal=None,
        tracker=None,
        response_owner_resolver=None,
        response_tag_conflict=None,
    ):
        super().__init__(
            bridge=bridge,
            companion_hash=companion_hash,
            port=port,
            bind_address=bind_address,
            client_idle_timeout_sec=client_idle_timeout_sec,
            device_model=device_model,
            device_version=device_version,
            build_date=build_date,
            local_hash=local_hash,
            stats_getter=stats_getter,
            control_handler=control_handler,
            heartbeat_interval=heartbeat_interval,
        )
        self.sqlite_handler = sqlite_handler
        self.journal = journal
        self._response_owner_resolver = response_owner_resolver
        self._response_tag_conflict = response_tag_conflict
        # RF correlation tracker (design doc §10.4): registers each freshly
        # persisted inbound message so a later duplicate reception can be
        # journaled as message_reception. Optional/None when correlation
        # isn't wired up (e.g. some tests construct a frame server directly).
        self.tracker = tracker
        self._defer_command_response = False
        self._deferred_command_response = None
        self._command_persistence_error = None
        self._command_persistence_committed = False
        # A reconnect must not replace the response queue while an old client
        # command is still running.  The same lock also lets stop() drain the
        # accepted command before tearing down its transport.
        self._command_session_lock = asyncio.Lock()
        self._client_sessions = {}
        self._active_client_session = None
        self._frame_stopping = False
        self._response_sessions: dict[str, OrderedDict[int, object]] = {
            "ack": OrderedDict(),
            "binary": OrderedDict(),
            "path": OrderedDict(),
            "trace": OrderedDict(),
            "control": OrderedDict(),
        }
        self._inflight_ack_session = None
        self._early_ack_sessions: OrderedDict[int, object] = OrderedDict()
        self._response_deadlines: dict[str, OrderedDict[int, float]] = {
            kind: OrderedDict() for kind in self._response_sessions
        }
        # Retain this descriptive alias for the existing control-response
        # window while all response kinds share the same deadline machinery.
        self._control_response_deadlines = self._response_deadlines["control"]

    def _session_guard(self):
        """Return the command/session ownership lock, including test embeddings."""
        lock = getattr(self, "_command_session_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._command_session_lock = lock
        return lock

    def _current_command_session(self):
        """Return the exact client session responsible for the current command."""
        session = _frame_command_session.get()
        if session is not None:
            return session
        return getattr(self, "_active_client_session", None)

    def _response_session_map(self, kind: str) -> OrderedDict[int, object]:
        """Return one bounded response-owner map, including minimal test embeddings."""
        maps = getattr(self, "_response_sessions", None)
        if maps is None:
            maps = {}
            self._response_sessions = maps
        owners = maps.get(kind)
        if owners is None:
            owners = OrderedDict()
            maps[kind] = owners
        return owners

    def _response_deadline_map(self, kind: str) -> OrderedDict[int, float]:
        """Return the deadline map paired with one response-owner map."""
        maps = getattr(self, "_response_deadlines", None)
        if maps is None:
            maps = {}
            self._response_deadlines = maps
        deadlines = maps.get(kind)
        if deadlines is None:
            if kind == "control":
                deadlines = getattr(self, "_control_response_deadlines", None)
            if deadlines is None:
                deadlines = OrderedDict()
            maps[kind] = deadlines
        if kind == "control":
            self._control_response_deadlines = deadlines
        return deadlines

    def _discard_response_claim(self, kind: str, key: int) -> None:
        """Remove one response owner and its matching timeout."""
        self._response_session_map(kind).pop(key, None)
        self._response_deadline_map(kind).pop(key, None)

    def _response_claim_expired(self, kind: str, key: int) -> bool:
        """Return whether a bounded response claim has expired."""
        deadline = self._response_deadline_map(kind).get(key)
        return deadline is not None and deadline <= time.monotonic()

    def _claim_response_session(
        self,
        kind: str,
        tag: int,
        session=None,
        *,
        timeout_ms: Optional[int] = None,
    ) -> None:
        """Bind one response tag to one Frame client session, failing closed on reuse."""
        if session is None:
            session = self._current_command_session()
        if session is None:
            return
        key = int(tag) & 0xFFFFFFFF
        owners = self._response_session_map(kind)
        previous = owners.pop(key, None)
        if previous is None or previous is session:
            owners[key] = session
        else:
            owners[key] = _AMBIGUOUS_SESSION
            logger.warning(
                "Companion %s: %s response tag 0x%08X was reused across "
                "Frame sessions; response will be dropped",
                self.companion_hash,
                kind,
                key,
            )
        deadlines = self._response_deadline_map(kind)
        previous_deadline = deadlines.pop(key, None)
        if timeout_ms is not None:
            try:
                timeout_seconds = max(0.0, int(timeout_ms) / 1000.0)
            except (TypeError, ValueError):
                timeout_seconds = 0.0
            deadline = time.monotonic() + timeout_seconds
        elif kind == "control":
            deadline = time.monotonic() + _CONTROL_RESPONSE_WINDOW_SECONDS
        else:
            deadline = None
        # When a tag collides with an older request, retain whichever response
        # window ends later. That keeps both possible late replies quarantined.
        if previous_deadline is not None:
            deadline = (
                previous_deadline
                if deadline is None
                else max(previous_deadline, deadline)
            )
        if deadline is not None:
            deadlines[key] = deadline
        while len(owners) > MAX_PENDING_ACK_CRCS:
            old_key, _ = owners.popitem(last=False)
            deadlines.pop(old_key, None)

    def _release_response_session(self, kind: str, tag: int, session=None) -> None:
        """Release a tentative owner only when it still belongs to this session."""
        if session is None:
            session = self._current_command_session()
        owners = self._response_session_map(kind)
        key = int(tag) & 0xFFFFFFFF
        if session is not None and owners.get(key) is session:
            self._discard_response_claim(kind, key)

    def _retire_session_claims(self, session) -> None:
        """Quarantine one disconnected session's tags only until their timeout."""
        for kind, owners in tuple(getattr(self, "_response_sessions", {}).items()):
            deadlines = self._response_deadline_map(kind)
            now = time.monotonic()
            for key, owner in tuple(owners.items()):
                if owner is not session:
                    continue
                deadline = deadlines.get(key)
                if deadline is None or deadline <= now:
                    self._discard_response_claim(kind, key)
                else:
                    owners[key] = _RETIRED_SESSION

        early = getattr(self, "_early_ack_sessions", None)
        if early is not None:
            for key, owner in tuple(early.items()):
                if owner is session:
                    early.pop(key, None)
        if getattr(self, "_inflight_ack_session", None) is session:
            self._inflight_ack_session = None

    def _pop_active_response_session(
        self,
        kind: str,
        tag: int,
        *,
        consume: bool = True,
    ):
        """Resolve one response owner and return it only while still connected."""
        key = int(tag) & 0xFFFFFFFF
        resolver = getattr(self, "_response_owner_resolver", None)
        if callable(resolver) and not resolver(self, kind, key):
            return None
        owners = self._response_session_map(kind)
        if self._response_claim_expired(kind, key):
            self._discard_response_claim(kind, key)
            logger.debug(
                "Companion %s: dropping expired %s response tag 0x%08X",
                self.companion_hash,
                kind,
                key,
            )
            return None
        owner = owners.pop(key, None) if consume else owners.get(key)
        if consume:
            self._response_deadline_map(kind).pop(key, None)
        if owner is None:
            logger.debug(
                "Companion %s: dropping unowned %s response tag 0x%08X",
                self.companion_hash,
                kind,
                key,
            )
            return None
        if owner is _AMBIGUOUS_SESSION:
            self._discard_response_claim(kind, key)
            logger.warning(
                "Companion %s: dropping ambiguous %s response tag 0x%08X",
                self.companion_hash,
                kind,
                key,
            )
            return None
        if owner is _RETIRED_SESSION:
            self._discard_response_claim(kind, key)
            logger.debug(
                "Companion %s: dropping retired-session %s response tag 0x%08X",
                self.companion_hash,
                kind,
                key,
            )
            return None
        if owner is not getattr(self, "_active_client_session", None):
            self._discard_response_claim(kind, key)
            logger.debug(
                "Companion %s: dropping stale-session %s response tag 0x%08X",
                self.companion_hash,
                kind,
                key,
            )
            return None
        return owner

    def owns_response_tag(self, kind: str, tag: int) -> bool:
        """Return whether this server has any live ownership claim for a tag."""
        key = int(tag) & 0xFFFFFFFF
        owners = self._response_session_map(kind)
        if key not in owners:
            if kind == "ack":
                inflight_owner = getattr(self, "_inflight_ack_session", None)
                return inflight_owner is not None and inflight_owner is getattr(
                    self,
                    "_active_client_session",
                    None,
                )
            return False
        if self._response_claim_expired(kind, key):
            self._discard_response_claim(kind, key)
            return False
        owner = owners.get(key)
        active_session = getattr(self, "_active_client_session", None)
        if owner is active_session and active_session is not None:
            return True
        deadline = self._response_deadline_map(kind).get(key)
        if owner in (_AMBIGUOUS_SESSION, _RETIRED_SESSION) and deadline is not None:
            return True
        if deadline is not None:
            # Cleanup normally performs this conversion under the session
            # lock. Keep ownership safe if an embedding bypassed that path.
            owners[key] = _RETIRED_SESSION
            return True
        self._discard_response_claim(kind, key)
        return False

    def discard_response_tag(self, kind: str, tag: int) -> None:
        """Discard an ambiguous global radio-response claim."""
        key = int(tag) & 0xFFFFFFFF
        self._discard_response_claim(kind, key)

    def _host_owns_response_tag(self, kind: str, tag: int) -> bool:
        """Return whether another client of the shared radio owns this tag."""
        key = int(tag) & 0xFFFFFFFF
        if self._response_session_map(kind).get(key) is _AMBIGUOUS_SESSION:
            return True
        if kind == "control":
            callbacks = getattr(
                getattr(self, "_control_handler", None),
                "_response_callbacks",
                {},
            )
            if key in callbacks:
                return True
        conflict = getattr(self, "_response_tag_conflict", None)
        if not callable(conflict):
            return False
        try:
            return bool(conflict(self, kind, key))
        except Exception:
            # Collision checks protect a shared radio. If ownership cannot be
            # verified, do not transmit and risk routing the response to the
            # wrong API client.
            logger.exception(
                "Companion %s: could not verify %s tag 0x%08X ownership",
                self.companion_hash,
                kind,
                key,
            )
            return True

    def _enqueue_frame(self, data: bytes) -> None:
        """Keep command-derived frames on the client session that requested them."""
        session = _frame_command_session.get()
        if (
            session is not None
            and session is not getattr(self, "_active_client_session", None)
        ):
            logger.debug(
                "Companion %s: dropping response for a superseded Frame session",
                self.companion_hash,
            )
            return
        super()._enqueue_frame(data)

    def _write_sent_response(self, is_flood: bool, tag: int, timeout_ms: int) -> None:
        """Record successful request ownership before publishing its SENT response."""
        kind = _sent_response_kind.get()
        session = self._current_command_session()
        if kind is not None and session is not None:
            key = int(tag) & 0xFFFFFFFF
            if kind == "ack":
                early = getattr(self, "_early_ack_sessions", OrderedDict()).pop(
                    key,
                    None,
                )
                if early is not session:
                    self._claim_response_session(
                        kind,
                        key,
                        session,
                        timeout_ms=timeout_ms,
                    )
            else:
                self._claim_response_session(
                    kind,
                    key,
                    session,
                    timeout_ms=timeout_ms,
                )
        super()._write_sent_response(is_flood, tag, timeout_ms)

    def _write_ok(self) -> None:
        """Defer a mutating command's success until persistence commits."""
        consume_prefs_error = getattr(
            self.bridge,
            "consume_prefs_save_error",
            None,
        )
        prefs_error = (
            consume_prefs_error() if callable(consume_prefs_error) else None
        )
        if prefs_error is not None:
            logger.warning(
                "Companion %s preference command rolled back: %s",
                self.companion_hash,
                prefs_error,
            )
            if getattr(self, "_defer_command_response", False):
                self._deferred_command_response = (
                    "err",
                    ERR_CODE_FILE_IO_ERROR,
                )
            else:
                super()._write_err(ERR_CODE_FILE_IO_ERROR)
            return
        if getattr(self, "_defer_command_response", False):
            self._deferred_command_response = ("ok", None)
            return
        super()._write_ok()

    def _write_err(self, err_code: int) -> None:
        """Keep upstream command errors single-frame while responses are deferred."""
        clear_prefs_error = getattr(self.bridge, "clear_prefs_save_error", None)
        if callable(clear_prefs_error):
            clear_prefs_error()
        if getattr(self, "_defer_command_response", False):
            self._deferred_command_response = ("err", int(err_code))
            return
        super()._write_err(err_code)

    async def _handle_cmd(self, payload: bytes) -> None:
        """Run one command for the client session that supplied it."""
        task = asyncio.current_task()
        sessions = getattr(self, "_client_sessions", {})
        session = sessions.get(task)
        async with self._session_guard():
            if getattr(self, "_frame_stopping", False):
                logger.debug(
                    "Ignoring companion command while frame server is stopping "
                    "(port=%s)",
                    self.port,
                )
                return
            if (
                session is not None
                and session is not getattr(self, "_active_client_session", None)
            ):
                logger.debug(
                    "Ignoring command from superseded companion client "
                    "(port=%s)",
                    self.port,
                )
                return
            clear_prefs_error = getattr(
                self.bridge,
                "clear_prefs_save_error",
                None,
            )
            if callable(clear_prefs_error):
                clear_prefs_error()
            session_token = _frame_command_session.set(session)
            try:
                await super()._handle_cmd(payload)
            finally:
                _frame_command_session.reset(session_token)

    def _begin_durable_command(self) -> None:
        self._clear_durable_command()
        self._defer_command_response = True

    def _clear_durable_command(self) -> None:
        """Discard all per-command state without writing a response."""
        self._defer_command_response = False
        self._deferred_command_response = None
        self._command_persistence_error = None
        self._command_persistence_committed = False
        self._contact_command_key = None
        self._contact_command_before = None
        self._channel_command_index = None
        self._channel_command_before = None

    def _finish_durable_command(self) -> bool:
        """Emit exactly one response and report whether the command committed."""
        response = getattr(self, "_deferred_command_response", None)
        persistence_error = getattr(self, "_command_persistence_error", None)
        self._clear_durable_command()

        if persistence_error is not None:
            super()._write_err(ERR_CODE_FILE_IO_ERROR)
            return False
        if response is None:
            logger.error(
                "Companion %s command completed without a response",
                self.companion_hash,
            )
            super()._write_err(ERR_CODE_FILE_IO_ERROR)
            return False
        kind, err_code = response
        if kind == "ok":
            super()._write_ok()
            return True
        super()._write_err(err_code)
        return False

    async def _command_persist(self, function, *args):
        """Run one command write off-loop and remember any durability failure."""
        await_commit = getattr(self.bridge, "_await_blocking_commit", None)
        if not callable(await_commit):
            await_commit = self._await_blocking_commit
        outcome = []

        def _invoke():
            result = function(*args)
            outcome.append(result)
            return result

        try:
            result = await await_commit(_invoke)
            if result is False:
                raise RuntimeError("storage rejected the write")
            self._command_persistence_committed = True
            return result
        except asyncio.CancelledError:
            # Both commit helpers propagate cancellation only after the worker
            # finishes. Preserve memory only when storage accepted the write.
            if outcome and outcome[0] is not False:
                self._command_persistence_committed = True
            elif outcome:
                self._command_persistence_error = RuntimeError(
                    "storage rejected the write"
                )
            raise
        except BaseException as exc:
            self._command_persistence_error = exc
            raise

    @staticmethod
    async def _await_blocking_commit(function, *args):
        """Cancellation-safe fallback for minimal bridge embeddings."""
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            try:
                await task
            except BaseException:
                raise
            raise cancellation

    def _state_guard(self):
        """Return the bridge's shared snapshot/mutation lock when available."""
        return getattr(self.bridge, "state_mutation_lock", _AsyncNullContext())

    async def _await_committed_state(self) -> None:
        """Wait for the current state transaction without holding up RF I/O.

        The upstream send handlers read state and build their packet
        synchronously before their first transport await. Releasing here and
        immediately delegating therefore captures a committed state while
        keeping contact/channel mutations independent of radio latency.
        """
        async with self._state_guard():
            pass

    def _setup_push_callbacks(self) -> None:
        """Restore upstream Frame callbacks without removing host observers.

        The repeater bridge commits durable state before dispatch reaches this
        transport. Within the transient callbacks, preserve upstream Frame
        ordering ahead of optional host callbacks.
        """
        super()._setup_push_callbacks()
        callbacks_by_event = getattr(self.bridge, "_push_callbacks", {})
        for callbacks in callbacks_by_event.values():
            frame_callbacks = [
                callback
                for callback in callbacks
                if getattr(callback, "__self__", None) is self
            ]
            if not frame_callbacks:
                continue
            host_callbacks = [
                callback
                for callback in callbacks
                if getattr(callback, "__self__", None) is not self
            ]
            callbacks[:] = frame_callbacks + host_callbacks

    async def start(self) -> None:
        """Start persistence before accepting companion client connections."""
        self._frame_stopping = False
        if not _is_loopback_bind_address(self.bind_address):
            logger.warning(
                "SECURITY: companion frame TCP for %s is exposed on %r:%s; "
                "the MeshCore frame protocol is not authenticated. Use "
                "127.0.0.1 unless direct LAN access is explicitly required.",
                self.companion_hash,
                self.bind_address,
                self.port,
            )
        if self.sqlite_handler:
            add_observer = getattr(self.bridge, "add_observer", None)
            if callable(add_observer):
                add_observer("contact_committed", self._on_contact_committed)
            if getattr(self.bridge, "manages_inbound_history", False) is not True:
                # Compatibility fallback for a non-repeater Core bridge. The
                # repeater bridge owns durability before transport callbacks,
                # including when Frame is disabled.
                self.bridge.on_message_event(self._on_message_event)
                self.bridge.on_channel_message_event(self._on_channel_message_event)
                self.bridge.on_channel_data_event(self._on_channel_data_event)
            if getattr(self.bridge, "manages_contact_history", False) is not True:
                # Compatibility fallback for bridges that still delegate
                # contact durability to the Frame server.
                self.bridge.on_advert_received(self._on_advert_received)
                self.bridge.on_contact_path_updated(self._on_contact_path_updated)
                self.bridge.on_contact_deleted(self._on_contact_deleted)
        await super().start()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Own one client session without crossing command response queues.

        This follows the upstream one-client eviction flow, adding only a
        short gate around client replacement and command execution.
        """
        session = object()
        task = asyncio.current_task()
        local_write_queue = None
        local_writer_task = None

        async with self._session_guard():
            if getattr(self, "_frame_stopping", False):
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception as exc:
                    logger.debug(
                        "Companion %s: closing rejected client session failed: %s",
                        self.companion_hash,
                        exc,
                    )
                return
            if self._client_writer:
                await super()._evict_existing_client()

            self._client_reader = reader
            self._client_writer = writer
            self._configure_socket(writer)
            local_write_queue = asyncio.Queue(
                maxsize=self._WRITE_QUEUE_MAXSIZE,
            )
            self._write_queue = local_write_queue
            self._setup_push_callbacks()
            self._active_client_session = session
            if task is not None:
                self._client_sessions[task] = session
            logger.info("Companion client connected (port=%s)", self.port)

            local_writer_task = asyncio.create_task(
                self._writer_loop(writer),
            )
            self._writer_task = local_writer_task

        disconnect_reason = None
        try:
            disconnect_reason = await self._read_client_frames(
                reader,
                local_writer_task,
            )
        except asyncio.IncompleteReadError:
            disconnect_reason = "incomplete_read"
        except (ConnectionResetError, BrokenPipeError) as exc:
            disconnect_reason = type(exc).__name__
        except Exception as exc:
            disconnect_reason = f"other: {type(exc).__name__}: {exc}"
            logger.error("Client handler error: %s", exc, exc_info=True)
        finally:
            async with self._session_guard():
                await self._cleanup_client(
                    writer,
                    local_write_queue,
                    local_writer_task,
                    disconnect_reason,
                )
                if task is not None:
                    self._client_sessions.pop(task, None)
                self._retire_session_claims(session)
                if self._active_client_session is session:
                    self._active_client_session = None

    async def _on_advert_received(self, contact) -> None:
        """Inbound contact persistence is owned by the bridge's atomic diff."""
        if getattr(self.bridge, "manages_contact_history", False) is not True:
            await super()._on_advert_received(contact)

    async def _on_node_discovered(self, contact_or_data) -> None:
        """Delay stored-contact push until the bridge confirms its commit."""
        public_key = getattr(contact_or_data, "public_key", None)
        if public_key is None and isinstance(contact_or_data, dict):
            public_key = contact_or_data.get("public_key") or contact_or_data.get("pubkey")
        if isinstance(public_key, str):
            try:
                public_key = bytes.fromhex(public_key)
            except ValueError:
                public_key = None
        if (
            getattr(self.bridge, "_contact_commit_pending", False)
            and isinstance(public_key, bytes)
            and self.bridge.contacts.get_by_key(public_key) is not None
        ):
            return
        await super()._on_node_discovered(contact_or_data)

    async def _on_contact_path_updated(self, contact) -> None:
        """Delay PATH push until its contact diff is durable."""
        if getattr(self.bridge, "_contact_commit_pending", False):
            return
        await super()._on_contact_path_updated(contact)

    async def _on_contact_committed(self, change: str, contact_dict: dict) -> None:
        """Publish one Frame cue after the bridge/storage transaction commits."""
        public_key = contact_dict.get("pubkey", b"")
        if change == "remove":
            super()._on_contact_deleted(public_key)
            return
        contact = self.bridge.contacts.get_by_key(public_key)
        if contact is None:
            return
        if change == "path":
            self._enqueue_frame(
                bytes([PUSH_CODE_PATH_UPDATED])
                + public_key[:PUB_KEY_SIZE]
            )
            return
        await super()._on_node_discovered(contact)

    def _on_send_confirmed(self, crc, trip_ms=0):
        """Push an ACK only to the Frame session that originated that exact send."""
        try:
            key = int(crc) & 0xFFFFFFFF
        except (TypeError, ValueError):
            return
        owners = self._response_session_map("ack")
        early_ack = key not in owners
        if early_ack:
            inflight_owner = getattr(self, "_inflight_ack_session", None)
            if inflight_owner is not getattr(
                self,
                "_active_client_session",
                None,
            ):
                logger.debug(
                    "Companion %s: dropping unowned ACK 0x%08X",
                    self.companion_hash,
                    key,
                )
                return
            self._claim_response_session("ack", key, inflight_owner)
        owner = self._pop_active_response_session("ack", key)
        if owner is None:
            return
        if early_ack:
            early = getattr(self, "_early_ack_sessions", None)
            if early is None:
                early = OrderedDict()
                self._early_ack_sessions = early
            early.pop(key, None)
            early[key] = owner
            while len(early) > MAX_PENDING_ACK_CRCS:
                early.popitem(last=False)

        session_token = _frame_command_session.set(owner)
        try:
            super()._on_send_confirmed(crc, trip_ms)
        finally:
            _frame_command_session.reset(session_token)

    def _on_binary_response(
        self,
        tag_bytes,
        response_data,
        parsed=None,
        request_type=None,
    ):
        """Push a binary response only to the session that owns its request tag."""
        if isinstance(tag_bytes, bytes):
            if len(tag_bytes) < 4:
                return
            tag = int.from_bytes(tag_bytes[:4], "little")
        else:
            try:
                tag = int(tag_bytes)
            except (TypeError, ValueError):
                return
        owner = self._pop_active_response_session("binary", tag)
        if owner is None:
            getattr(self, "_companion_binary_tags", set()).discard(tag)
            return
        session_token = _frame_command_session.set(owner)
        try:
            super()._on_binary_response(
                tag_bytes,
                response_data,
                parsed,
                request_type,
            )
        finally:
            _frame_command_session.reset(session_token)

    def _on_path_discovery_response(
        self,
        tag_bytes,
        contact_pubkey,
        out_len_byte,
        out_path,
        in_len_byte,
        in_path,
    ):
        """Push a path response only to the session that requested its tag."""
        if isinstance(tag_bytes, bytes):
            if len(tag_bytes) < 4:
                return
            tag = int.from_bytes(tag_bytes[:4], "little")
        else:
            try:
                tag = int(tag_bytes)
            except (TypeError, ValueError):
                return
        owner = self._pop_active_response_session("path", tag)
        if owner is None:
            return
        session_token = _frame_command_session.set(owner)
        try:
            super()._on_path_discovery_response(
                tag_bytes,
                contact_pubkey,
                out_len_byte,
                out_path,
                in_len_byte,
                in_path,
            )
        finally:
            _frame_command_session.reset(session_token)

    def push_trace_data(
        self,
        path_len: int,
        flags: int,
        tag: int,
        auth_code: int,
        path_hashes: bytes,
        path_snrs: bytes,
        final_snr_byte: int,
    ) -> None:
        """Push a trace completion only to the session that sent its exact tag."""
        owner = self._pop_active_response_session("trace", tag)
        if owner is None:
            return
        session_token = _frame_command_session.set(owner)
        try:
            super().push_trace_data(
                path_len,
                flags,
                tag,
                auth_code,
                path_hashes,
                path_snrs,
                final_snr_byte,
            )
        finally:
            _frame_command_session.reset(session_token)

    async def push_control_data(
        self,
        snr: float,
        rssi: int,
        path_len: int,
        path_bytes: bytes,
        payload: bytes,
    ) -> None:
        """Push discovery responses only to the session owning their exact tag."""
        if len(payload) >= 6 and (payload[0] & 0xF0) == 0x90:
            tag = int.from_bytes(payload[2:6], "little")
            # A discovery broadcast can receive many node responses during the
            # same bounded window, all sharing its request tag.
            owner = self._pop_active_response_session(
                "control",
                tag,
                consume=False,
            )
            if owner is None:
                return
            session_token = _frame_command_session.set(owner)
            try:
                await super().push_control_data(
                    snr,
                    rssi,
                    path_len,
                    path_bytes,
                    payload,
                )
            finally:
                _frame_command_session.reset(session_token)
            return
        await super().push_control_data(
            snr,
            rssi,
            path_len,
            path_bytes,
            payload,
        )

    # -----------------------------------------------------------------
    # Persistence hook overrides
    # -----------------------------------------------------------------

    async def _persist_companion_message(self, msg_dict: dict, queue_entry=None) -> None:
        """Store durable history and remove its exact in-memory queue entry.

        ``offline_queue_size`` limits only pending delivery to a frame client;
        zero no longer disables REST history. The journal helper commits the
        message row and its sync event together, then wakes live listeners.

        ``queue_entry`` is supplied by openhop-core and is removed by identity.
        Never use ``pop_last()`` here: another receive can append while the
        database write is in flight, making "last" a different message.
        """
        if getattr(self.bridge, "manages_inbound_history", False) is True:
            return
        await persist_inbound_message(
            bridge=self.bridge,
            sqlite_handler=self.sqlite_handler,
            companion_hash=self.companion_hash,
            msg_dict=msg_dict,
            queue_entry=queue_entry,
            journal=self.journal,
            tracker=getattr(self, "tracker", None),
        )

    async def _on_message_event(self, event) -> None:
        """Persist direct-message history even when the frame queue rejected it."""
        if not event.queued:
            await self._persist_companion_message(
                {
                    "sender_key": event.sender_key,
                    "text": event.text,
                    "timestamp": event.timestamp,
                    "txt_type": event.txt_type,
                    "is_channel": False,
                    "channel_idx": 0,
                    "path_len": event.path_len,
                    "packet_hash": event.packet_hash,
                    "snr": event.snr,
                    "rssi": event.rssi,
                    "sender_prefix": event.sender_prefix,
                },
                None,
            )
        await super()._on_message_event(event)

    async def _on_channel_message_event(self, event) -> None:
        """Persist channel-text history even when the frame queue rejected it."""
        if not event.queued:
            await self._persist_companion_message(
                {
                    "sender_key": b"",
                    "text": event.text,
                    "timestamp": event.timestamp,
                    "txt_type": 0,
                    "is_channel": True,
                    "channel_idx": event.channel_idx,
                    "path_len": event.path_len,
                    "packet_hash": event.packet_hash,
                    "snr": event.snr,
                    "rssi": event.rssi,
                },
                None,
            )
        await super()._on_channel_message_event(event)

    async def _on_channel_data_event(self, event) -> None:
        """Persist channel-data history even when the frame queue rejected it."""
        if not event.queued:
            await self._persist_companion_message(
                {
                    "sender_key": b"",
                    "text": "",
                    "timestamp": 0,
                    "txt_type": 0,
                    "is_channel": True,
                    "channel_idx": event.channel_idx,
                    "path_len": event.path_len,
                    "packet_hash": event.packet_hash,
                    "snr": event.snr,
                    "rssi": event.rssi,
                    "channel_data_type": event.data_type,
                    "channel_data_payload": bytes(event.payload or b""),
                },
                None,
            )
        await super()._on_channel_data_event(event)

    def _remove_queue_entry(self, queue_entry) -> None:
        """Remove one persisted entry without disturbing concurrent receives."""
        remove_queue_entry(self.bridge, self.companion_hash, queue_entry)

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
        if self.sqlite_handler is not None:
            msg = await asyncio.to_thread(self._sync_next_from_persistence)
        else:
            msg = self.bridge.sync_next_message()
        if msg is None:
            self._write_frame(bytes([RESP_CODE_NO_MORE_MESSAGES]))
            return
        self._write_frame(self._build_message_frame(msg))

    async def _cmd_get_contacts(self, data: bytes) -> None:
        async with self._state_guard():
            await super()._cmd_get_contacts(data)

    async def _cmd_get_contact_by_key(self, data: bytes) -> None:
        async with self._state_guard():
            await super()._cmd_get_contact_by_key(data)

    async def _cmd_get_channel(self, data: bytes) -> None:
        async with self._state_guard():
            await super()._cmd_get_channel(data)

    async def _cmd_get_advert_path(self, data: bytes) -> None:
        async with self._state_guard():
            await super()._cmd_get_advert_path(data)

    async def _cmd_export_contact(self, data: bytes) -> None:
        async with self._state_guard():
            await super()._cmd_export_contact(data)

    async def _cmd_share_contact(self, data: bytes) -> None:
        await self._await_committed_state()
        await super()._cmd_share_contact(data)

    async def _cmd_send_txt_msg(self, data: bytes) -> None:
        await self._await_committed_state()
        owner = self._current_command_session()
        previous_owner = getattr(self, "_inflight_ack_session", None)
        self._inflight_ack_session = owner
        kind_token = _sent_response_kind.set("ack")
        try:
            await super()._cmd_send_txt_msg(data)
        finally:
            _sent_response_kind.reset(kind_token)
            self._inflight_ack_session = previous_owner
            early = getattr(self, "_early_ack_sessions", {})
            for crc, session in tuple(early.items()):
                if session is owner:
                    early.pop(crc, None)

    async def _cmd_send_channel_txt_msg(self, data: bytes) -> None:
        await self._await_committed_state()
        await super()._cmd_send_channel_txt_msg(data)

    async def _cmd_send_channel_data(self, data: bytes) -> None:
        await self._await_committed_state()
        await super()._cmd_send_channel_data(data)

    async def _cmd_send_binary_req(self, data: bytes) -> None:
        await self._await_committed_state()
        kind_token = _sent_response_kind.set("binary")
        try:
            await super()._cmd_send_binary_req(data)
        finally:
            _sent_response_kind.reset(kind_token)

    async def _cmd_send_anon_req(self, data: bytes) -> None:
        await self._await_committed_state()
        kind_token = _sent_response_kind.set("binary")
        try:
            await super()._cmd_send_anon_req(data)
        finally:
            _sent_response_kind.reset(kind_token)

    async def _cmd_send_path_discovery_req(self, data: bytes) -> None:
        await self._await_committed_state()
        kind_token = _sent_response_kind.set("path")
        try:
            await super()._cmd_send_path_discovery_req(data)
        finally:
            _sent_response_kind.reset(kind_token)

    async def _cmd_send_trace_path(self, data: bytes) -> None:
        await self._await_committed_state()
        if len(data) < 10:
            self._write_err(ERR_CODE_ILLEGAL_ARG)
            return
        tag = struct.unpack_from("<I", data, 0)[0]
        auth_code = struct.unpack_from("<I", data, 4)[0]
        flags = data[8]
        path_bytes = data[9:]
        hash_width = PathUtils.trace_payload_hash_width(flags)
        if (
            len(path_bytes) % hash_width != 0
            or len(path_bytes) // hash_width > MAX_PATH_SIZE
        ):
            self._write_err(ERR_CODE_ILLEGAL_ARG)
            return
        send_raw = getattr(self.bridge, "send_trace_path_raw", None)
        if not send_raw:
            self._write_err(ERR_CODE_UNSUPPORTED_CMD)
            return

        owner = self._current_command_session()
        self._claim_response_session("trace", tag, owner)
        if self._host_owns_response_tag("trace", tag):
            self._release_response_session("trace", tag, owner)
            logger.warning(
                "Companion %s: refusing Frame trace tag 0x%08X already "
                "owned by another shared-radio client",
                self.companion_hash,
                tag,
            )
            self._write_err(ERR_CODE_TABLE_FULL)
            return
        try:
            result = await send_raw(tag, auth_code, flags, path_bytes)
        except Exception as exc:
            self._release_response_session("trace", tag, owner)
            logger.error("send_trace_path error: %s", exc, exc_info=True)
            self._write_err(ERR_CODE_ILLEGAL_ARG)
            return
        if not result.success:
            self._release_response_session("trace", tag, owner)
            self._write_err(ERR_CODE_TABLE_FULL)
            return

        kind_token = _sent_response_kind.set("trace")
        try:
            self._write_sent_response(result.is_flood, tag, result.timeout_ms)
        finally:
            _sent_response_kind.reset(kind_token)

    async def _cmd_send_control_data(self, data: bytes) -> None:
        """Send discovery without touching the Repeater API's shared callback slot."""
        await self._await_committed_state()
        if len(data) < 1 or (data[0] & 0x80) == 0:
            self._write_err(ERR_CODE_UNSUPPORTED_CMD)
            return
        discovery_tag = None
        discovery_owner = None
        if len(data) >= 6 and (data[0] & 0xF0) == 0x80:
            discovery_tag = struct.unpack("<I", data[2:6])[0]
            discovery_owner = self._current_command_session()
            self._claim_response_session(
                "control",
                discovery_tag,
                discovery_owner,
            )
            if self._host_owns_response_tag("control", discovery_tag):
                self._release_response_session(
                    "control",
                    discovery_tag,
                    discovery_owner,
                )
                logger.warning(
                    "Companion %s: refusing Frame discovery tag 0x%08X already "
                    "owned by another shared-radio client",
                    self.companion_hash,
                    discovery_tag,
                )
                self._write_err(ERR_CODE_TABLE_FULL)
                return
        send_control = getattr(self.bridge, "send_control_data", None)
        if not send_control:
            if discovery_tag is not None:
                self._release_response_session(
                    "control",
                    discovery_tag,
                    discovery_owner,
                )
            self._write_err(ERR_CODE_UNSUPPORTED_CMD)
            return
        try:
            sent = await send_control(data)
        except Exception as exc:
            if discovery_tag is not None:
                self._release_response_session(
                    "control",
                    discovery_tag,
                    discovery_owner,
                )
            logger.error("send_control_data error: %s", exc, exc_info=True)
            self._write_err(ERR_CODE_ILLEGAL_ARG)
            return
        if not sent:
            if discovery_tag is not None:
                self._release_response_session(
                    "control",
                    discovery_tag,
                    discovery_owner,
                )
            self._write_err(ERR_CODE_TABLE_FULL)
            return
        if discovery_tag is not None:
            # Refresh the multi-response window from successful transmission,
            # not from the earlier collision-safe tentative reservation.
            self._claim_response_session(
                "control",
                discovery_tag,
                discovery_owner,
            )
        self._write_ok()

    async def _cmd_send_login(self, data: bytes) -> None:
        await self._await_committed_state()
        await super()._cmd_send_login(data)

    async def _cmd_send_status_req(self, data: bytes) -> None:
        await self._await_committed_state()
        await super()._cmd_send_status_req(data)

    async def _cmd_send_telemetry_req(self, data: bytes) -> None:
        await self._await_committed_state()
        await super()._cmd_send_telemetry_req(data)

    async def _cmd_logout(self, data: bytes) -> None:
        await self._await_committed_state()
        await super()._cmd_logout(data)

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
            "gps_lat": c.gps_lat if c.gps_lat is not None else 0.0,
            "gps_lon": c.gps_lon if c.gps_lon is not None else 0.0,
            "sync_since": c.sync_since,
        }

    def _contact_state(self, public_key: bytes) -> Optional[dict]:
        """Return one contact as a persistence-safe dict."""
        get_contact = getattr(self.bridge, "get_contact_by_key", None)
        contact = get_contact(public_key) if callable(get_contact) else None
        return None if contact is None else self._contact_to_dict(contact)

    def _restore_contact(self, public_key: bytes, before_contact) -> None:
        """Restore one exact contact snapshot after a pre-commit failure."""
        current = self.bridge.get_contact_by_key(public_key)
        if before_contact is None:
            if current is not None:
                self.bridge.contacts.remove(public_key)
        elif current is None:
            self.bridge.contacts.add(before_contact)
        else:
            self.bridge.contacts.update(before_contact)

    def _restore_channel(self, idx: int, before_channel) -> None:
        """Restore one exact channel snapshot after a pre-commit failure."""
        if before_channel is None:
            self.bridge.channels.remove(idx)
        else:
            self.bridge.channels.set(idx, before_channel)

    async def _notify_contact_change(
        self,
        before: Optional[dict],
        public_key: bytes,
        change: Optional[str] = None,
    ) -> None:
        """Emit one semantic event for a frame-client contact mutation."""
        after = self._contact_state(public_key)
        if after == before:
            return
        notify = getattr(self.bridge, "notify_observers", None)
        if not callable(notify):
            return
        if change is None:
            change = (
                "remove"
                if after is None
                else ("new" if before is None else "update")
            )
        await notify("contact_changed", change, after if after is not None else before)

    async def _cmd_add_update_contact(self, data: bytes) -> None:
        async with self._state_guard():
            await self._cmd_add_update_contact_durable(data)

    async def _cmd_add_update_contact_durable(self, data: bytes) -> None:
        """Apply upstream parsing, then acknowledge only after durable commit."""
        public_key = data[:32] if len(data) >= 32 else b""
        before = self._contact_state(public_key) if public_key else None
        before_contact = (
            copy.deepcopy(self.bridge.get_contact_by_key(public_key))
            if public_key
            else None
        )
        self._begin_durable_command()
        self._contact_command_key = public_key or None
        self._contact_command_before = before
        try:
            try:
                await super()._cmd_add_update_contact(data)
            finally:
                self._contact_command_key = None
                self._contact_command_before = None
        except BaseException:
            if (
                public_key
                and not getattr(self, "_command_persistence_committed", False)
            ):
                self._restore_contact(public_key, before_contact)
            self._clear_durable_command()
            raise
        if (
            getattr(self, "_command_persistence_error", None) is not None
            and public_key
        ):
            self._restore_contact(public_key, before_contact)
        committed = self._finish_durable_command()
        if committed and public_key:
            await self._notify_contact_change(before, public_key)

    async def _cmd_remove_contact(self, data: bytes) -> None:
        async with self._state_guard():
            await self._cmd_remove_contact_durable(data)

    async def _cmd_remove_contact_durable(self, data: bytes) -> None:
        """Apply upstream parsing, then acknowledge only after durable commit."""
        public_key = data[:32] if len(data) >= 32 else b""
        before = self._contact_state(public_key) if public_key else None
        before_contact = (
            copy.deepcopy(self.bridge.get_contact_by_key(public_key))
            if public_key
            else None
        )
        self._begin_durable_command()
        self._contact_command_key = public_key or None
        self._contact_command_before = before
        try:
            try:
                await super()._cmd_remove_contact(data)
            finally:
                self._contact_command_key = None
                self._contact_command_before = None
        except BaseException:
            if (
                public_key
                and not getattr(self, "_command_persistence_committed", False)
            ):
                self._restore_contact(public_key, before_contact)
            self._clear_durable_command()
            raise
        if (
            getattr(self, "_command_persistence_error", None) is not None
            and public_key
        ):
            self._restore_contact(public_key, before_contact)
        committed = self._finish_durable_command()
        if committed and public_key:
            await self._notify_contact_change(before, public_key)

    async def _cmd_reset_path(self, data: bytes) -> None:
        async with self._state_guard():
            await self._cmd_reset_path_durable(data)

    async def _cmd_reset_path_durable(self, data: bytes) -> None:
        """Reset a path and acknowledge only after its durable commit."""
        public_key = data[:32] if len(data) >= 32 else b""
        before = self._contact_state(public_key) if public_key else None
        before_contact = (
            copy.deepcopy(self.bridge.get_contact_by_key(public_key))
            if public_key
            else None
        )
        self._begin_durable_command()
        try:
            await super()._cmd_reset_path(data)
            response = getattr(self, "_deferred_command_response", None)
            if public_key and response == ("ok", None):
                after = self._contact_state(public_key)
                if after != before and after is not None:
                    try:
                        if self.journal is not None:
                            await self._command_persist(
                                self.journal.store_contact,
                                after,
                                "path",
                            )
                        elif self.sqlite_handler is not None:
                            await self._command_persist(
                                self.sqlite_handler.companion_upsert_contact,
                                self.companion_hash,
                                after,
                            )
                    except Exception as e:
                        logger.warning(
                            "Save contact after path reset failed for %s: %s",
                            self.companion_hash,
                            e,
                        )
        except BaseException:
            if (
                public_key
                and not getattr(self, "_command_persistence_committed", False)
            ):
                self._restore_contact(public_key, before_contact)
            self._clear_durable_command()
            raise
        if (
            getattr(self, "_command_persistence_error", None) is not None
            and public_key
        ):
            self._restore_contact(public_key, before_contact)
        committed = self._finish_durable_command()
        if committed and public_key:
            await self._notify_contact_change(before, public_key, "path")

    async def _persist_contact(self, contact) -> None:
        """Upsert a single contact to SQLite (non-blocking)."""
        if not self.sqlite_handler:
            return
        contact_dict = self._contact_to_dict(contact)
        if self.journal is not None:
            await asyncio.to_thread(self.journal.store_contact, contact_dict)
        else:
            await asyncio.to_thread(
                self.sqlite_handler.companion_upsert_contact,
                self.companion_hash,
                contact_dict,
            )

    async def _on_contact_deleted(self, contact_or_key) -> None:
        """Delay automatic removal push until its contact diff is durable."""
        public_key = getattr(contact_or_key, "public_key", contact_or_key)
        if isinstance(public_key, bytearray):
            public_key = bytes(public_key)
        if getattr(self.bridge, "_contact_commit_pending", False):
            return
        if (
            getattr(self.bridge, "manages_contact_history", False) is not True
            and isinstance(public_key, bytes)
            and len(public_key) >= 32
        ):
            public_key = public_key[:32]
            try:
                if self.journal is not None:
                    await asyncio.to_thread(self.journal.remove_contact, public_key)
                elif self.sqlite_handler is not None:
                    await asyncio.to_thread(
                        self.sqlite_handler.companion_delete_contact,
                        self.companion_hash,
                        public_key,
                    )
            except Exception as e:
                logger.warning(
                    "Persist automatic contact removal failed for %s: %s",
                    self.companion_hash,
                    e,
                )
        super()._on_contact_deleted(public_key)

    async def _save_contacts(self) -> None:
        """Persist all contacts to SQLite (non-blocking).

        During a frame command, persist only that contact through the atomic
        journal helper. The stop-time bulk save remains unjournaled, so an
        unchanged restart does not replay the entire contact book.
        """
        if not self.sqlite_handler:
            return
        command_key = getattr(self, "_contact_command_key", None)
        if command_key is not None:
            contact = self._contact_state(command_key)
            before = getattr(self, "_contact_command_before", None)
            if contact == before:
                return
            if self.journal is not None and contact is None:
                await self._command_persist(self.journal.remove_contact, command_key)
            elif self.journal is not None:
                await self._command_persist(
                    self.journal.store_contact,
                    contact,
                    "new" if before is None else "update",
                )
            elif contact is None:
                await self._command_persist(
                    self.sqlite_handler.companion_delete_contact,
                    self.companion_hash,
                    command_key,
                )
            else:
                await self._command_persist(
                    self.sqlite_handler.companion_upsert_contact,
                    self.companion_hash,
                    contact,
                )
            return
        contacts = self.bridge.get_contacts()
        dicts = [self._contact_to_dict(c) for c in contacts]
        await asyncio.to_thread(
            self.sqlite_handler.companion_save_contacts,
            self.companion_hash,
            dicts,
        )

    def _channel_record(self, idx) -> Optional[dict]:
        """Persistence view of one channel slot, including its private secret."""
        if idx is None:
            return None
        channel = self.bridge.get_channel(idx)
        if channel is None:
            return None
        secret = bytes(channel.secret or b"")
        # Match openhop_core's set_channel representation so an equivalent
        # 16-byte MeshCore secret does not look changed after core pads it.
        secret = secret[:32].ljust(32, b"\x00")
        return {
            "index": int(idx),
            "name": channel.name,
            "secret": secret,
        }

    async def _cmd_set_channel(self, data: bytes) -> None:
        async with self._state_guard():
            await self._cmd_set_channel_durable(data)

    async def _cmd_set_channel_durable(self, data: bytes) -> None:
        """Apply upstream parsing, then acknowledge only after durable commit."""
        idx = data[0] if data else None
        before_channel = (
            copy.deepcopy(self.bridge.get_channel(idx)) if idx is not None else None
        )
        self._begin_durable_command()
        self._channel_command_index = idx
        self._channel_command_before = self._channel_record(idx)
        try:
            try:
                await super()._cmd_set_channel(data)
            finally:
                self._channel_command_index = None
                self._channel_command_before = None
        except BaseException:
            if (
                idx is not None
                and not getattr(self, "_command_persistence_committed", False)
            ):
                self._restore_channel(idx, before_channel)
            self._clear_durable_command()
            raise
        if (
            getattr(self, "_command_persistence_error", None) is not None
            and idx is not None
        ):
            self._restore_channel(idx, before_channel)
        self._finish_durable_command()

    async def _save_channels(self) -> None:
        """Persist channels to SQLite (non-blocking).

        During a frame command, persist only that channel through the atomic
        journal helper. The stop-time bulk save remains unjournaled, so an
        unchanged restart does not replay the channel table.
        """
        if not self.sqlite_handler:
            return
        command_idx = getattr(self, "_channel_command_index", None)
        if command_idx is not None:
            channel = self.bridge.get_channel(command_idx)
            # MeshCore treats an empty channel name as clearing the slot.
            if channel is not None and not channel.name:
                self.bridge.channels.remove(command_idx)
                channel = None
            after = self._channel_record(command_idx)
            if after == getattr(self, "_channel_command_before", None):
                return
            if self.journal is not None and channel is None:
                await self._command_persist(
                    self.journal.store_channel,
                    command_idx,
                    None,
                    None,
                )
            elif self.journal is not None:
                await self._command_persist(
                    self.journal.store_channel,
                    command_idx,
                    after["name"],
                    after["secret"],
                )
            else:
                # Journal-free embeddings retain the legacy whole-table
                # persistence hook; production uses the atomic journal path.
                channels = []
                max_ch = getattr(
                    getattr(self.bridge, "channels", None),
                    "max_channels",
                    40,
                )
                for idx in range(max_ch):
                    current = self.bridge.get_channel(idx)
                    if current is not None:
                        channels.append(
                            {
                                "channel_idx": idx,
                                "name": current.name,
                                "secret": current.secret,
                            }
                        )
                await self._command_persist(
                    self.sqlite_handler.companion_save_channels,
                    self.companion_hash,
                    channels,
                )
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
        """Stop without an unjournaled whole-table rewrite.

        Every supported mutation is committed at the point it succeeds, so a
        bulk save here would only race those atomic writes during hot removal.
        """
        self._frame_stopping = True
        # Stop accepting clients immediately. An already accepted handler sees
        # _frame_stopping under the session lock and closes without taking
        # ownership.
        server = getattr(self, "_server", None)
        if server is not None:
            server.close()
            await server.wait_closed()
            if self._server is server:
                self._server = None
        remove_observer = getattr(self.bridge, "remove_observer", None)
        if callable(remove_observer):
            remove_observer("contact_committed", self._on_contact_committed)
        # Let the command that already owns this session finish. Queued commands
        # observe _frame_stopping and are ignored before touching bridge state.
        async with self._session_guard():
            await super().stop()
        remove_callbacks = getattr(
            self.bridge,
            "remove_frame_server_callbacks",
            None,
        )
        if callable(remove_callbacks):
            remove_callbacks(self)
