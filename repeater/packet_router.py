import asyncio
import logging
import time

from openhop_core.node.handlers.ack import AckHandler
from openhop_core.node.handlers.advert import AdvertHandler
from openhop_core.node.handlers.control import ControlHandler
from openhop_core.node.handlers.login_response import LoginResponseHandler
from openhop_core.node.handlers.login_server import LoginServerHandler
from openhop_core.node.handlers.multipart import MultipartAckHandler
from openhop_core.node.handlers.path import PathHandler
from openhop_core.node.handlers.protocol_request import ProtocolRequestHandler
from openhop_core.node.handlers.protocol_response import ProtocolResponseHandler
from openhop_core.node.handlers.text import TextMessageHandler
from openhop_core.node.handlers.trace import TraceHandler
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_GRP_DATA,
    PAYLOAD_TYPE_GRP_TXT,
    PH_ROUTE_MASK,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_TRANSPORT_DIRECT,
)

from repeater.engine import DropReason
from repeater.companion.correlation import injected_tx_outcome
from repeater.policy_engine import PolicyDecision, PolicyEngine

logger = logging.getLogger("PacketRouter")

# Deliver PATH and protocol-response (PATH) to companion at most once per logical packet
# so the client is not spammed with duplicate telemetry when the mesh delivers multiple copies.
_COMPANION_DEDUPE_TTL_SEC = 60.0

# Normal policy/forwarding drop outcomes (logged at debug, not warning). The engine
# is the single source of truth via DropReason; string-form acceptance is retained
# because records and the recent_packets fallback still carry detailed text variants
# (e.g. "Max flood hops limit reached (5/5)").
_EXPECTED_DROP_REASONS = frozenset(DropReason)


def _companion_dedup_key(packet) -> str | None:
    """Return a stable key for companion delivery deduplication, or None if not available."""
    try:
        return packet.calculate_packet_hash().hex().upper()
    except Exception:
        return None


def _is_direct_final_hop(packet) -> bool:
    """True if packet is DIRECT (or TRANSPORT_DIRECT) with empty path — we're the final destination."""
    route = getattr(packet, "header", 0) & PH_ROUTE_MASK
    if route != ROUTE_TYPE_DIRECT and route != ROUTE_TYPE_TRANSPORT_DIRECT:
        return False
    path = getattr(packet, "path", None)
    return not path or len(path) == 0


def _is_direct_intermediate_hop(packet) -> bool:
    """True for a direct packet that still has one or more routing hops."""
    route = getattr(packet, "header", 0) & PH_ROUTE_MASK
    return route in (ROUTE_TYPE_DIRECT, ROUTE_TYPE_TRANSPORT_DIRECT) and not _is_direct_final_hop(
        packet
    )


def _is_expected_drop_reason(reason) -> bool:
    # Membership is the fast path for a DropReason member or its exact text; the
    # startswith scan covers detailed variants that embed a suffix.
    if reason in _EXPECTED_DROP_REASONS:
        return True
    if not isinstance(reason, str) or not reason:
        return False
    return any(reason.startswith(expected) for expected in _EXPECTED_DROP_REASONS)


def _drop_reason_from_recent_packets(handler, packet) -> str | None:
    """Best-effort drop reason lookup from handler recent packet records."""
    recent_packets = getattr(handler, "recent_packets", None)
    if not recent_packets:
        return None
    try:
        packet_hash = packet.calculate_packet_hash().hex().upper()[:16]
    except Exception:
        return None
    for record in reversed(list(recent_packets)):
        if not isinstance(record, dict):
            continue
        if record.get("packet_hash") != packet_hash:
            continue
        reason = record.get("drop_reason")
        if isinstance(reason, str) and reason:
            return reason
    return None


class PacketRouter:
    def __init__(self, daemon_instance):
        self.daemon = daemon_instance
        self.queue = asyncio.Queue(maxsize=500)
        self.running = False
        self.router_task = None
        # Serialize injects so one local TX completes before the next is processed
        self._inject_lock = asyncio.Lock()
        # Hash -> expiry time; skip delivering same PATH/protocol-response to companions more than once
        self._companion_delivered = {}
        # Keep the check -> authenticated fan-out -> mark sequence atomic.
        # _route_packet tasks run concurrently, so a plain dict check on both
        # sides of an await can otherwise deliver the same response twice.
        self._companion_delivery_lock = asyncio.Lock()
        # Safety valve: cap the number of _route_packet tasks sleeping concurrently.
        # LoRa's airtime budget naturally limits throughput, but burst arrivals
        # (multi-hop amplification, collision retries) can stack many sleeping
        # delay tasks before the duty-cycle gate fires.  30 is very generous for
        # any realistic LoRa network but protects against pathological scenarios
        # (e.g. a busy bridge node during a mesh-wide flood) exhausting memory or
        # starving the event loop.
        self._in_flight: int = 0
        self._max_in_flight: int = 30
        # Live set of in-flight tasks — kept in sync with _in_flight via the
        # done-callback.  Used exclusively for shutdown drain; the integer
        # counter is used for the cap check (faster, single source of truth).
        self._route_tasks: set = set()
        # Total packets dropped because the cap was reached.  Exposed in logs
        # at shutdown so operators know whether the cap is actually firing.
        self._cap_drop_count: int = 0

    async def start(self):
        self.running = True
        self.router_task = asyncio.create_task(self._process_queue())
        logger.info("Packet router started")

    async def stop(self):
        self.running = False
        if self.router_task:
            self.router_task.cancel()
            try:
                await self.router_task
            except asyncio.CancelledError:
                pass

        # Drain in-flight tasks gracefully, then cancel any that outlast the
        # timeout.  This mirrors what the old _route_tasks set enabled and gives
        # in-progress packets a fair chance to finish (e.g. their TX delay sleep
        # + send) before the process exits.
        if self._route_tasks:
            pending_snapshot = set(self._route_tasks)
            logger.info(
                "Draining %d in-flight route task(s) (5 s timeout)...",
                len(pending_snapshot),
            )
            _, still_pending = await asyncio.wait(pending_snapshot, timeout=5.0)
            if still_pending:
                logger.warning(
                    "Cancelling %d route task(s) that did not finish within the shutdown timeout",
                    len(still_pending),
                )
                for task in still_pending:
                    task.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)

        if self._cap_drop_count:
            logger.warning(
                "In-flight cap dropped %d packet(s) during this session — "
                "consider raising _max_in_flight if this is frequent",
                self._cap_drop_count,
            )
        logger.info("Packet router stopped")

    def _on_route_done(self, task: asyncio.Task) -> None:
        """Done-callback for _route_packet tasks: decrement counter and surface errors."""
        self._in_flight -= 1
        self._route_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("_route_packet raised: %s", exc, exc_info=exc)

    def _was_delivered_to_companions(self, packet) -> bool:
        """Pure check: True if this PATH/protocol-response was already delivered (unexpired).

        Does not mutate delivery state, so callers can deliver first and only record
        the packet as delivered once a bridge has actually received it.
        """
        key = _companion_dedup_key(packet)
        if not key:
            return False
        expiry = self._companion_delivered.get(key)
        return expiry is not None and expiry > time.time()

    def _mark_delivered_to_companions(self, packet) -> None:
        """Record that this PATH/protocol-response reached companions.

        Called only after a bridge has received the packet, so a delivery that
        raised for every bridge is retried on the next copy instead of being
        suppressed for the full dedupe TTL.
        """
        key = _companion_dedup_key(packet)
        if not key:
            return
        now = time.time()
        # Prune expired entries only when the dict grows large, avoiding a full
        # dict comprehension on every packet.  200 entries × 60 s TTL means a
        # sweep only triggers after ~200 unique PATH packets with no expiry — far
        # more than any realistic companion session, and well below the 1000-entry
        # threshold that could accumulate over hours without pruning.
        if len(self._companion_delivered) > 200:
            self._companion_delivered = {
                k: v for k, v in self._companion_delivered.items() if v > now
            }
        self._companion_delivered[key] = now + _COMPANION_DEDUPE_TTL_SEC

    def _should_deliver_path_to_companions(self, packet) -> bool:
        """Atomic check-and-mark: True on the first of duplicate PATH/protocol-responses."""
        if self._was_delivered_to_companions(packet):
            return False
        self._mark_delivered_to_companions(packet)
        return True

    def _policy_companion_decision(self, packet, metadata: dict) -> PolicyDecision | None:
        """Return cached policy decision used to gate companion delivery.

        Stores the pre-check decision in shared metadata so the repeater engine
        can reuse it and avoid a second full policy evaluation pass.
        """
        handler = getattr(self.daemon, "repeater_handler", None)
        if not handler:
            return None
        policy_engine = getattr(handler, "policy_engine", None)
        if not isinstance(policy_engine, PolicyEngine) or not policy_engine.enabled:
            return None

        cached = metadata.get("_policy_precheck_decision")
        if isinstance(cached, PolicyDecision):
            return cached

        mode = self.daemon.config.get("repeater", {}).get("mode", "forward")
        route_type = getattr(packet, "header", 0) & PH_ROUTE_MASK
        policy_context = {
            "route_type": route_type,
            "payload_type": packet.get_payload_type()
            if hasattr(packet, "get_payload_type")
            else None,
            "payload_length": len(packet.payload or b""),
            "path_hash_size": packet.get_path_hash_size()
            if hasattr(packet, "get_path_hash_size")
            else None,
            "hop_count": packet.get_path_hash_count()
            if hasattr(packet, "get_path_hash_count")
            else None,
            "rssi": metadata.get("rssi", getattr(packet, "rssi", 0)),
            "snr": metadata.get("snr", getattr(packet, "snr", 0.0)),
            "local_transmission": False,
            "mode": mode,
        }
        decision = policy_engine.evaluate(packet, policy_context)
        metadata["_policy_precheck_decision"] = decision
        return decision

    def _policy_blocks_companion(self, packet, metadata: dict) -> bool:
        """Return True when policy action is drop, making companion suppression final."""
        decision = self._policy_companion_decision(packet, metadata)
        if not isinstance(decision, PolicyDecision):
            return False
        if decision.action == "drop":
            logger.debug(
                "Policy pre-check blocked companion delivery: rule %s action=drop",
                decision.rule_id,
            )
            return True
        return False

    def _companion_bridges_for_packet(self, packet, metadata: dict) -> dict:
        """Return companion bridges unless policy drop pre-check blocks delivery."""
        companion_bridges = getattr(self.daemon, "companion_bridges", {})
        if not companion_bridges:
            return {}
        if self._policy_blocks_companion(packet, metadata):
            return {}
        return dict(companion_bridges)

    async def _fan_out_to_bridges(self, packet, bridges, *, context: str) -> bool:
        """Offer packet to each bridge; True if any bridge authenticated it.

        Accepts a dict of bridges — pass a single-entry dict for targeted delivery
        to the bridge that owns ``dest_hash``. A bridge that raises is logged and
        skipped; ``result.authenticated`` is read directly (every bridge returns a
        HandlerResult) so a broken contract surfaces instead of being hidden.
        """
        authenticated = False
        for bridge in tuple(bridges.values()):
            try:
                result = await bridge.process_received_packet(packet)
            except Exception as e:
                logger.debug("Companion bridge %s error: %s", context, e)
                continue
            if result.authenticated is True:
                authenticated = True
        return authenticated

    async def _fan_out_to_bridges_once(
        self,
        packet,
        bridges,
        *,
        context: str,
    ) -> bool | None:
        """Deliver one deduplicated response after authentication.

        ``None`` means an earlier copy was already delivered. ``False`` means
        this attempt did not authenticate, so a waiting duplicate may retry.
        """
        async with self._companion_delivery_lock:
            if self._was_delivered_to_companions(packet):
                return None
            authenticated = await self._fan_out_to_bridges(
                packet,
                bridges,
                context=context,
            )
            if authenticated:
                self._mark_delivered_to_companions(packet)
            return authenticated

    async def _consume_via_local_candidates(
        self, packet, metadata: dict, dest_hash, helper, process_method_name: str
    ) -> bool:
        """Try every local candidate that shares ``dest_hash`` and report consumption.

        The on-air destination hash is only one byte, so several local identities
        can share it. The candidates are the companion bridge registered at
        ``dest_hash`` and the room-server / repeater identity registered in
        ``helper`` (login, text, or protocol-request) at the same hash. Both are
        offered the packet; the one whose key MAC-verifies it consumes it, the
        other fails HMAC and no-ops.

        Returns True only when at least one candidate authenticated (decrypted)
        the packet. Consuming solely on authenticated handling is what lets a
        one-byte prefix collision with a remote node — or a forged packet — fall
        through to the forwarding engine instead of being swallowed.
        """
        companion_bridges = self._companion_bridges_for_packet(packet, metadata)
        helper_handlers = getattr(helper, "handlers", {}) if helper else {}
        has_companion = dest_hash is not None and dest_hash in companion_bridges
        has_local_identity = dest_hash is not None and dest_hash in helper_handlers

        consumed = False
        if has_companion:
            bridge_result = await companion_bridges[dest_hash].process_received_packet(packet)
            if bridge_result.authenticated:
                consumed = True
        # Offer to the room-server / repeater identity when it shares the hash
        # (collision) or when no local companion claims it at all (normal
        # server-owned + remote-forward handling).
        if helper and (has_local_identity or not has_companion):
            if await getattr(helper, process_method_name)(packet):
                consumed = True
        return consumed

    def _record_for_ui(self, packet, metadata: dict) -> None:
        """Record an injection-only packet for the web UI (storage + recent_packets)."""
        handler = getattr(self.daemon, "repeater_handler", None)
        if handler and getattr(handler, "storage", None):
            try:
                handler.record_packet_only(packet, metadata)
            except Exception as e:
                logger.debug("Record for UI failed: %s", e)

    async def _register_ack_with_dispatcher(self, ack_crc: int, context: str) -> None:
        """Best-effort ACK CRC registration with the dispatcher waiter path."""
        dispatcher = getattr(self.daemon, "dispatcher", None)
        if dispatcher is None or not hasattr(dispatcher, "_register_ack_received"):
            return
        try:
            await dispatcher._register_ack_received(ack_crc)
        except Exception as e:
            logger.debug("Dispatcher %s registration error: %s", context, e)

    async def enqueue(self, packet):
        """Add packet to router queue."""
        if self.queue.full():
            logger.warning("Packet router queue full (%d), dropping oldest", self.queue.maxsize)
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await self.queue.put(packet)

    async def inject_packet(
        self,
        packet,
        wait_for_ack: bool = False,
        expected_crc=None,
        origin_hash=None,
        ack_timeout_s: float = 5.0,
    ):
        try:
            # RepeaterCompanionBridge uses this task-local marker to
            # distinguish local rejection from False after RF acceptance
            # (most notably a wait-for-ACK timeout). Packet is slotted, so an
            # ad-hoc packet attribute is not portable.
            tx_outcome = injected_tx_outcome.get()
            if tx_outcome is not None:
                tx_outcome["accepted"] = False
                tx_outcome["attempted"] = False
            metadata = {
                "rssi": getattr(packet, "rssi", 0),
                "snr": getattr(packet, "snr", 0.0),
                "timestamp": getattr(packet, "timestamp", 0),
            }

            # Serialize injects so one local TX completes before the next runs
            # (avoids duty-cycle or dispatcher races where a later packet goes out first)
            async with self._inject_lock:
                # Use local_transmission=True to bypass forwarding logic
                if tx_outcome is not None:
                    tx_outcome["attempted"] = True
                sent = await self.daemon.repeater_handler(packet, metadata, local_transmission=True)
                if not sent:
                    logger.warning("Injected packet failed local transmission")
                    return False
                if tx_outcome is not None:
                    tx_outcome["accepted"] = True

                # Mark so dequeue processing does not pass this local TX to the
                # engine a second time.
                packet._injected_for_tx = True

                # Keep local echo and enqueue in the same ordering boundary as
                # RF acceptance. A later send must not become observable before
                # an earlier on-air packet. ACK waiting remains outside the
                # lock so it never stalls unrelated radio use.
                push_rx = getattr(self.daemon, "_on_raw_rx_for_companions", None)
                if push_rx is not None:
                    try:
                        raw = packet.write_to()
                        await push_rx(
                            raw,
                            0,
                            0.0,
                            exclude_hash=origin_hash,
                        )
                        servers = getattr(
                            self.daemon,
                            "companion_frame_servers",
                            [],
                        )
                        pushed = sum(
                            1
                            for fs in servers
                            if getattr(fs, "companion_hash", None) != origin_hash
                        )
                        logger.debug(
                            "Echoed injected TX as raw RX (0x88) to %d "
                            "companion client(s) (%d bytes, origin=%s excluded)",
                            pushed,
                            len(raw),
                            origin_hash,
                        )
                    except Exception as e:
                        logger.debug(
                            "Failed to echo injected TX to companions: %s",
                            e,
                        )

                # Queue delivery is part of local completion: TXT_MSG routes to
                # its destination bridge; ACK reaches every interested bridge.
                await self.enqueue(packet)

            if wait_for_ack:
                ptype = getattr(packet, "get_payload_type", lambda: None)()
                if ptype not in {
                    AckHandler.payload_type(),
                    AdvertHandler.payload_type(),
                }:
                    dispatcher = getattr(self.daemon, "dispatcher", None)
                    if dispatcher and hasattr(dispatcher, "wait_for_ack"):
                        try:
                            wait_crc = (
                                expected_crc if expected_crc is not None else packet.get_crc()
                            )
                            wait_timeout = (
                                float(ack_timeout_s)
                                if isinstance(ack_timeout_s, (int, float)) and ack_timeout_s > 0
                                else 5.0
                            )
                            ack_ok = await dispatcher.wait_for_ack(wait_crc, timeout=wait_timeout)
                            if not ack_ok:
                                logger.warning(
                                    "Injected packet ACK timeout (crc=%08X, timeout=%.1fs)",
                                    wait_crc,
                                    wait_timeout,
                                )
                                return False
                        except Exception as e:
                            logger.warning("Injected packet ACK wait failed: %s", e)
                            return False

            packet_len = len(packet.payload) if packet.payload else 0
            logger.debug(
                f"Injected packet processed by engine as local transmission ({packet_len} bytes)"
            )
            # Log protocol REQ (e.g. status/telemetry) so we can confirm target node
            ptype = getattr(packet, "get_payload_type", lambda: None)()
            if (
                ptype == ProtocolRequestHandler.payload_type()
                and packet.payload
                and packet_len >= 1
            ):
                logger.info(
                    "Injected protocol REQ: dest=0x%02x, payload=%d bytes",
                    packet.payload[0],
                    packet_len,
                )
            return True

        except asyncio.CancelledError:
            # Cancellation can arrive after the physical TX call has begun but
            # before it reports whether the radio accepted the packet. Treat
            # that window as indeterminate so callers retain correlation and
            # do not safely retry a packet that may already be on air.
            tx_outcome = injected_tx_outcome.get()
            if (
                tx_outcome is not None
                and tx_outcome.get("attempted") is True
                and tx_outcome.get("accepted") is not True
            ):
                tx_outcome["uncertain"] = True
            raise
        except Exception as e:
            tx_outcome = injected_tx_outcome.get()
            if (
                tx_outcome is not None
                and tx_outcome.get("attempted") is True
                and tx_outcome.get("accepted") is not True
            ):
                tx_outcome["uncertain"] = True
            logger.error(f"Error injecting packet through engine: {e}")
            return False

    async def _process_queue(self):
        while self.running:
            try:
                packet = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                # Drop early if the in-flight cap is reached.  This is a last-resort
                # safety valve — under normal operation LoRa airtime and the duty-cycle
                # gate keep _in_flight well below _max_in_flight.
                if self._in_flight >= self._max_in_flight:
                    self._cap_drop_count += 1
                    logger.warning(
                        "In-flight task cap reached (%d/%d), dropping packet "
                        "(session total dropped: %d)",
                        self._in_flight,
                        self._max_in_flight,
                        self._cap_drop_count,
                    )
                    continue
                self._in_flight += 1
                task = asyncio.create_task(self._route_packet(packet))
                self._route_tasks.add(task)
                task.add_done_callback(self._on_route_done)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Router error: {e}", exc_info=True)

    async def _route_packet(self, packet):

        payload_type = packet.get_payload_type()
        processed_by_injection = False
        metadata = {
            "rssi": getattr(packet, "rssi", 0),
            "snr": getattr(packet, "snr", 0.0),
            "timestamp": getattr(packet, "timestamp", 0),
        }

        # MeshCore routes direct packets with remaining hops before normal
        # payload dispatch. Only TRACE, high-bit CONTROL, and early ACK handling
        # have special behavior at an intermediate hop.
        direct_intermediate = _is_direct_intermediate_hop(packet)
        if direct_intermediate:
            if payload_type == TraceHandler.payload_type():
                processed_by_injection = True
                if not getattr(packet, "_injected_for_tx", False) and self.daemon.trace_helper:
                    await self.daemon.trace_helper.process_trace_packet(packet)
            elif payload_type == ControlHandler.payload_type():
                if packet.payload and (packet.payload[0] & 0x80):
                    # Direct high-bit CONTROL is accepted only at zero hops.
                    processed_by_injection = True
            elif payload_type == AckHandler.payload_type():
                if len(getattr(packet, "payload", b"")) >= 4:
                    ack_crc = int.from_bytes(packet.payload[:4], "little")
                    await self._register_ack_with_dispatcher(ack_crc, "ACK")

        # Route to specific handlers for parsing only
        elif payload_type == TraceHandler.payload_type():
            # Locally injected TRACE requests are TX-only and re-enter the router so
            # companion delivery can still happen. They are not inbound RF responses,
            # so skip TraceHelper parsing to avoid matching pending ping tags against
            # zeroed local metadata.
            if getattr(packet, "_injected_for_tx", False):
                processed_by_injection = True
            elif self.daemon.trace_helper:
                await self.daemon.trace_helper.process_trace_packet(packet)
                # Skip engine processing for trace packets - they're handled by trace helper
                processed_by_injection = True
                # Do not call _record_for_ui: TraceHelper.log_trace_record already persists the
                # trace path from the payload. record_packet_only would treat packet.path (SNR bytes)
                # as routing hashes and log bogus duplicate rows.

        elif payload_type == ControlHandler.payload_type():
            # MeshCore never relays control packets: the direct high-bit discovery
            # subset is explicitly released (Mesh.cpp), and any other control
            # payload hits the switch default that does not flood-route unknown
            # types. Mark do-not-retransmit unconditionally so a repeater with
            # discovery disabled still does not forward control/discovery traffic
            # to the engine. Discovery responses are injected as their own TX, so
            # this does not suppress them.
            packet.mark_do_not_retransmit()
            # Process control/discovery packet
            if self.daemon.discovery_helper:
                await self.daemon.discovery_helper.control_handler(packet)
            # Deliver to companions via daemon (frame servers push PUSH_CODE_CONTROL_DATA 0x8E)
            deliver = getattr(self.daemon, "deliver_control_data", None)
            if deliver:
                snr = getattr(packet, "_snr", None) or getattr(packet, "snr", 0.0)
                rssi = getattr(packet, "_rssi", None) or getattr(packet, "rssi", 0)
                path_len = getattr(packet, "path_len", 0) or 0
                path_bytes = (
                    bytes(getattr(packet, "path", []))
                    if getattr(packet, "path", None) is not None
                    else b""
                )[:path_len]
                payload_bytes = bytes(packet.payload) if packet.payload else b""
                await deliver(snr, rssi, path_len, path_bytes, payload_bytes)

        elif payload_type == AdvertHandler.payload_type():
            # Process advertisement packet for neighbor tracking
            if self.daemon.advert_helper:
                rssi = getattr(packet, "rssi", 0)
                snr = getattr(packet, "snr", 0.0)
                await self.daemon.advert_helper.process_advert_packet(packet, rssi, snr)
            # Also feed adverts to companion bridges (for contact/path updates),
            # but keep policy drop final just like the other companion paths.
            companion_bridges = self._companion_bridges_for_packet(packet, metadata)
            await self._fan_out_to_bridges(packet, companion_bridges, context="advert")

        elif payload_type == LoginServerHandler.payload_type():
            # Route ANON_REQ/login to the local identity that owns it. The on-air
            # dest hash is only one byte, so a companion and a room-server identity
            # can share it; decryption is the only real disambiguator. Offer the
            # packet to every local candidate and consume only when one decrypts —
            # a remote (or forged) ANON_REQ that merely collides on the prefix is
            # then forwarded by the engine. Our own injected ANON_REQ is suppressed
            # by the engine's duplicate (mark_seen) check.
            dest_hash = packet.payload[0] if packet.payload else None
            if await self._consume_via_local_candidates(
                packet, metadata, dest_hash, self.daemon.login_helper, "process_login_packet"
            ):
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type == AckHandler.payload_type():
            # Ensure ACK CRC reaches dispatcher waiter path even when only router fallback is active.
            if len(getattr(packet, "payload", b"")) >= 4:
                ack_crc = int.from_bytes(packet.payload[:4], "little")
                await self._register_ack_with_dispatcher(ack_crc, "ACK")
            # ACK has no dest in payload (4-byte CRC only); deliver to all bridges so sender sees send_confirmed.
            # Do not set processed_by_injection so packet also reaches engine for DIRECT forwarding when we're a middle hop.
            companion_bridges = self._companion_bridges_for_packet(packet, metadata)
            await self._fan_out_to_bridges(packet, companion_bridges, context="ACK")

        elif payload_type == MultipartAckHandler.payload_type():
            # MULTIPART ACK wrapper: low nibble of first byte is embedded payload type.
            if (
                len(getattr(packet, "payload", b"")) >= 5
                and (packet.payload[0] & 0x0F) == AckHandler.payload_type()
            ):
                ack_crc = int.from_bytes(packet.payload[1:5], "little")
                await self._register_ack_with_dispatcher(ack_crc, "multi-ACK")

        elif payload_type == TextMessageHandler.payload_type():
            # Same one-byte dest-hash collision handling as the login path above:
            # a companion and a room-server text identity can share a hash, and
            # only decryption tells them apart. Offer to every local candidate and
            # consume only when one decrypts, so a companion never shadows a
            # room-server message and a prefix collision with a remote node still
            # forwards.
            dest_hash = packet.payload[0] if packet.payload else None
            if await self._consume_via_local_candidates(
                packet, metadata, dest_hash, self.daemon.text_helper, "process_text_packet"
            ):
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type == PathHandler.payload_type():
            # Always let PathHelper inspect/decrypt PATH first so out_path and bundled ACK state
            # are updated even when companion routing fan-out also happens for this packet.
            consumed = False
            if self.daemon.path_helper:
                try:
                    consumed = (await self.daemon.path_helper.process_path_packet(packet)) is True
                except Exception as e:
                    logger.debug(f"Path helper processing error: {e}")
            # The helper/bridge results decide ownership: a direct middle hop
            # that cannot authenticate remains eligible for engine forwarding,
            # while a local MAC-authenticated PATH is consumed below.
            dest_hash = packet.payload[0] if packet.payload else None
            companion_bridges = self._companion_bridges_for_packet(packet, metadata)
            if dest_hash is not None and dest_hash in companion_bridges:
                bridge_authenticated = await self._fan_out_to_bridges_once(
                    packet,
                    {dest_hash: companion_bridges[dest_hash]},
                    context="PATH",
                )
                if bridge_authenticated is None:
                    # A prior copy authenticated as local, so this duplicate
                    # is local too and must not fall through to forwarding.
                    consumed = True
                else:
                    consumed = bridge_authenticated or consumed
            elif companion_bridges:
                # Dest not in bridges: path-return with ephemeral dest (e.g. multi-hop login).
                # Deliver to all bridges; each will try to decrypt and ignore if not relevant.
                bridge_authenticated = await self._fan_out_to_bridges_once(
                    packet,
                    companion_bridges,
                    context="PATH",
                )
                if bridge_authenticated is None:
                    consumed = True
                else:
                    consumed = bridge_authenticated or consumed
                    logger.debug(
                        "PATH dest=0x%02x (anon) delivered to %d bridge(s) for matching",
                        dest_hash or 0,
                        len(companion_bridges),
                    )
            if consumed:
                # A local MAC-authenticated PATH belongs to this node. Do not
                # let the forwarding engine retransmit it, but retain it for UI.
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type == LoginResponseHandler.payload_type():
            # PAYLOAD_TYPE_RESPONSE (0x01): payload is dest_hash(1)+src_hash(1)+encrypted.
            # Deliver to the bridge that is the destination, or to all bridges when the
            # response is addressed to this repeater (path-based reply: firmware sends
            # to first hop instead of original requester).
            consumed = False
            dest_hash = packet.payload[0] if packet.payload and len(packet.payload) >= 1 else None
            companion_bridges = self._companion_bridges_for_packet(packet, metadata)
            local_hash = getattr(self.daemon, "local_hash", None)
            if dest_hash is not None and dest_hash in companion_bridges:
                consumed = await self._fan_out_to_bridges(
                    packet, {dest_hash: companion_bridges[dest_hash]}, context="RESPONSE"
                )
                logger.info("RESPONSE dest=0x%02x delivered to companion bridge", dest_hash)
            elif dest_hash == local_hash and companion_bridges:
                # Response addressed to this repeater (e.g. path-based reply to first hop)
                consumed = await self._fan_out_to_bridges(
                    packet, companion_bridges, context="RESPONSE"
                )
                logger.info(
                    "RESPONSE dest=0x%02x (local) delivered to %d companion bridge(s)",
                    dest_hash,
                    len(companion_bridges),
                )
            elif companion_bridges:
                # Dest not in bridges and not local: likely ANON_REQ response (dest = ephemeral
                # sender hash). Deliver to all bridges; each will try to decrypt and ignore if
                # not relevant (firmware-like behavior, works with multiple companion bridges).
                consumed = await self._fan_out_to_bridges(
                    packet, companion_bridges, context="RESPONSE"
                )
                logger.debug(
                    "RESPONSE dest=0x%02x (anon) delivered to %d bridge(s) for matching",
                    dest_hash or 0,
                    len(companion_bridges),
                )
            if consumed:
                processed_by_injection = True
                self._record_for_ui(packet, metadata)
            elif companion_bridges and _is_direct_final_hop(packet):
                # DIRECT with empty path is engine release hygiene: there is
                # no next hop, even when no local identity authenticated it.
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type == ProtocolResponseHandler.payload_type():
            # PAYLOAD_TYPE_PATH (0x08): protocol responses (telemetry, binary, etc.).
            # Deliver at most once per logical packet so the client is not spammed with duplicates,
            # but always deliver at a final hop (we are the destination). Do not set
            # processed_by_injection for a middle hop so the packet still reaches engine forwarding.
            companion_bridges = self._companion_bridges_for_packet(packet, metadata)
            final_hop = _is_direct_final_hop(packet)
            if companion_bridges:
                await self._fan_out_to_bridges_once(
                    packet,
                    companion_bridges,
                    context="RESPONSE",
                )
            if companion_bridges and final_hop:
                # DIRECT with empty path: we're the final hop, so consume after delivery.
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type == ProtocolRequestHandler.payload_type():
            # Same one-byte dest-hash collision handling as the login/text paths:
            # a companion and a server (ACL) identity can share a hash and only
            # decryption tells them apart. Offer to every local candidate and
            # consume only when one authenticates the REQ; otherwise leave it for
            # the engine.
            dest_hash = packet.payload[0] if packet.payload else None
            if await self._consume_via_local_candidates(
                packet,
                metadata,
                dest_hash,
                self.daemon.protocol_request_helper,
                "process_request_packet",
            ):
                processed_by_injection = True
                self._record_for_ui(packet, metadata)
            else:
                companion_bridges = self._companion_bridges_for_packet(packet, metadata)
                if companion_bridges and _is_direct_final_hop(packet):
                    # OpenHop release hygiene: an empty-path DIRECT has no next hop,
                    # so consume after offering it to bridges even without MAC ownership.
                    await self._fan_out_to_bridges(packet, companion_bridges, context="REQ")
                    processed_by_injection = True
                    self._record_for_ui(packet, metadata)

        elif payload_type == PAYLOAD_TYPE_GRP_TXT:
            # GRP_TXT: pass to all companions (they filter by channel); still forward.
            # Policy drop is final and blocks companion delivery.
            companion_bridges = self._companion_bridges_for_packet(packet, metadata)
            await self._fan_out_to_bridges(packet, companion_bridges, context="GRP_TXT")

        elif payload_type == PAYLOAD_TYPE_GRP_DATA:
            # MeshCore forwards direct packets with remaining hops before payload
            # handling. Otherwise, companions authenticate and filter channels.
            if not _is_direct_intermediate_hop(packet):
                companion_bridges = self._companion_bridges_for_packet(packet, metadata)
                await self._fan_out_to_bridges(packet, companion_bridges, context="GRP_DATA")

        # Only pass to repeater engine if not already processed by injection
        # Skip engine for packets we injected for TX (already sent; avoid double-send/double-count)
        if getattr(packet, "_injected_for_tx", False):
            processed_by_injection = True
        if self.daemon.repeater_handler and not processed_by_injection:
            sent = await self.daemon.repeater_handler(packet, metadata)
            if sent is False:
                drop_reason = metadata.get("_repeater_drop_reason")
                if not isinstance(drop_reason, str):
                    drop_reason = getattr(packet, "_repeater_drop_reason", None)
                if not isinstance(drop_reason, str):
                    drop_reason = _drop_reason_from_recent_packets(
                        self.daemon.repeater_handler, packet
                    )
                if _is_expected_drop_reason(drop_reason):
                    logger.debug(
                        "Inbound packet intentionally not transmitted by repeater handler "
                        "(type=%s, header=0x%02x, reason=%s)",
                        payload_type,
                        getattr(packet, "header", 0),
                        drop_reason,
                    )
                else:
                    logger.warning(
                        "Inbound packet not transmitted by repeater handler "
                        "(type=%s, header=0x%02x, reason=%s)",
                        payload_type,
                        getattr(packet, "header", 0),
                        drop_reason or "unknown",
                    )
