"""Neighbour scope-query helper for openHop Repeater.

Client side of the anonymous *regions* request. The server side already lives in
:class:`~openhop_core.node.handlers.anon_request.AnonRequestHandler` (wired up by
:mod:`repeater.handler_helpers.login`); this module asks the question instead of
answering it, so the MQTT ``neighbors`` topic can report which region scopes each
zero-hop neighbour serves.

Wire format mirrors firmware ``MyMesh::sendAnonRegionsReq``: a PAYLOAD_TYPE_ANON_REQ
whose plaintext is ``tag(4) + ANON_REQ_TYPE_REGIONS + 0x00``, where the trailing
``0x00`` asks for a zero-hop reply path. The reply is a PAYLOAD_TYPE_RESPONSE
datagram (``dest_hash(1) + src_hash(1) + cipher``) whose plaintext is
``tag(4) + clock(4) + comma_separated_scope_names``.

Two firmware behaviours are load-bearing and are reproduced here:

* The request must be **route-direct** — ``AnonRequestHandler`` (and the firmware
  regions handler it mirrors) ignores flooded discovery sub-types outright.
* Queries are issued **strictly one at a time**. Firing them as a burst makes every
  responder answer into the same window and the replies collide; the firmware fixed
  this by walking the snapshot one entry at a time and only arming the response
  deadline once the request has actually transmitted (MeshCore ``aba571ed``).
  ``router.inject_packet`` already resolves at that exact point — it awaits
  ``dispatcher.send_packet`` under the engine TX lock — so the firmware's
  QUEUED/PENDING state machine collapses into a sequential ``await`` here.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from openhop_core.protocol import CryptoUtils, Identity
from openhop_core.protocol.constants import (
    ANON_REQ_TYPE_REGIONS,
    MAX_PACKET_PAYLOAD,
    PAYLOAD_TYPE_RESPONSE,
)

logger = logging.getLogger("NeighborScopes")

# Per-neighbour outcome, published verbatim in the MQTT neighbors payload.
STATUS_RESPONDED = "responded"
STATUS_TIMEOUT = "timeout"
STATUS_SEND_FAILED = "send_failed"

# Responder-side reply delay to budget for. Firmware uses SERVER_RESPONSE_DELAY
# (500 ms); openhop_core's AnonRequestHandler uses 300 ms. Budget the larger so a
# firmware neighbour is not written off early.
SERVER_RESPONSE_DELAY_MS = 500.0

# Slack for scheduler jitter, matching the firmware's own 360 ms constant.
RESPONSE_TIMEOUT_SLACK_MS = 360.0

# Bounds for the auto-sized response timeout. The floor keeps a fast radio config
# from producing an unreachably tight window; the ceiling keeps a slow one (SF12,
# narrow bandwidth) from stalling a whole sweep on one dead neighbour.
MIN_RESPONSE_TIMEOUT_SEC = 5.0
MAX_RESPONSE_TIMEOUT_SEC = 120.0

# Used when no AirtimeManager is available to size the window from radio params.
FALLBACK_RESPONSE_TIMEOUT_SEC = 30.0

# Whole-sweep budget, and how long a duty-cycle backlog may be before the sweep
# gives up rather than queueing behind forwarding traffic for minutes.
DEFAULT_MAX_SWEEP_SECONDS = 900.0
DEFAULT_DUTY_CYCLE_ABORT_SECONDS = 30.0


def neighbors_config_block(config: Optional[dict]) -> dict:
    """Return ``mqtt_brokers.neighbors`` as a mapping, or ``{}`` when it is not one.

    ``neighbors`` names a settings block under ``mqtt_brokers`` but a plain
    boolean on each broker entry, and ``config.yaml.example`` documents both, so
    ``mqtt_brokers.neighbors: true`` is an easy hand-edit to make. A truthy
    non-mapping used to reach ``.get()`` directly, which raised AttributeError
    inside :meth:`NeighborScopeHelper.refresh_config` — and because that helper is
    built during daemon init, it took the whole daemon down on startup. Ignore the
    value instead; saving from the API rewrites it as a proper block.
    """
    if not isinstance(config, dict):
        return {}
    brokers_cfg = config.get("mqtt_brokers", {})
    if not isinstance(brokers_cfg, dict):
        return {}
    block = brokers_cfg.get("neighbors", {})
    if not isinstance(block, dict):
        logger.debug(
            "Ignoring mqtt_brokers.neighbors: expected a settings block, got %s",
            type(block).__name__,
        )
        return {}
    return block


@dataclass(frozen=True)
class NeighborSnapshot:
    """One neighbour, frozen at sweep start.

    Firmware originally indexed into the live ``neighbours[]`` table and had to
    stop doing that (``aba571ed``) because an advert arriving mid-pass could
    reshuffle it. Same reasoning here: the sqlite neighbour table keeps changing
    while a multi-minute sweep runs, so what gets published is decided up front.
    """

    pubkey: str
    last_seen: float = 0.0
    snr: float = 0.0

    @property
    def pubkey_bytes(self) -> bytes:
        return bytes.fromhex(self.pubkey)


@dataclass
class ScopeResult:
    status: str
    scopes: str = ""
    # Whether the request actually reached the air. Feeds the payload's
    # ``queried_neighbors``, which firmware increments in ``logTx`` when a QUEUED
    # entry becomes PENDING. It is not derivable from ``status``: a target the
    # sweep never reached is reported as ``timeout`` exactly like one that was
    # asked and stayed silent.
    transmitted: bool = False


@dataclass
class _PendingQuery:
    pubkey: str
    tag: int
    src_hash: int
    future: "asyncio.Future[str]" = field(repr=False, default=None)


class _ScopeTarget:
    """Minimal contact stand-in for ``PacketBuilder.create_anon_request``.

    ``out_path_len = 0`` (rather than -1) selects direct routing with an empty
    path, i.e. the zero-hop request the responder's route-direct gate requires.
    """

    def __init__(self, pubkey_hex: str):
        self.public_key = pubkey_hex
        self.out_path_len = 0
        self.out_path = b""


class NeighborScopeHelper:
    """Issues anon-regions queries and matches their responses."""

    def __init__(
        self,
        local_identity,
        packet_injector: Optional[Callable] = None,
        airtime_manager=None,
        config: Optional[dict] = None,
    ):
        self.local_identity = local_identity
        self.packet_injector = packet_injector
        self.airtime_manager = airtime_manager
        # Held by reference so a live config update is visible; re-read at the
        # start of every sweep rather than cached at construction.
        self.config = config if config is not None else {}

        self._pending: Optional[_PendingQuery] = None
        self._sweep_lock = asyncio.Lock()

        self._response_timeout_override = 0.0
        self._max_sweep_seconds = DEFAULT_MAX_SWEEP_SECONDS
        self._duty_cycle_abort_seconds = DEFAULT_DUTY_CYCLE_ABORT_SECONDS
        self._direct_tx_delay_factor = 0.5
        self.refresh_config(self.config)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def refresh_config(self, config: Optional[dict] = None) -> None:
        """Re-read the tunables. Called at construction and before each sweep."""
        if config is None:
            config = self.config
        else:
            self.config = config
        neighbors_cfg = neighbors_config_block(config)

        self._response_timeout_override = _as_float(
            neighbors_cfg.get("scope_response_timeout_seconds", 0), 0.0
        )
        self._max_sweep_seconds = max(
            1.0, _as_float(neighbors_cfg.get("max_sweep_seconds"), DEFAULT_MAX_SWEEP_SECONDS)
        )
        self._duty_cycle_abort_seconds = max(
            0.0,
            _as_float(
                neighbors_cfg.get("duty_cycle_abort_seconds"), DEFAULT_DUTY_CYCLE_ABORT_SECONDS
            ),
        )
        delays_cfg = config.get("delays", {}) if isinstance(config, dict) else {}
        self._direct_tx_delay_factor = _as_float(
            delays_cfg.get("direct_tx_delay_factor") if isinstance(delays_cfg, dict) else None,
            0.5,
        )

    def response_timeout(self) -> float:
        """How long to wait for a reply once the request is on air.

        Mirrors firmware ``neighborDiscoverQueryTimeoutMs()``: the responder's
        fixed reply delay, plus however long it may defer the transmission, plus
        airtime for one packet ahead of the response and the response itself.
        The firmware term there is ``getCADFailMaxDuration()``; openhop's
        equivalent deferral is the engine's random direct-TX window,
        ``[0, 5 * airtime * direct_tx_delay_factor]``.
        """
        if self._response_timeout_override > 0:
            return self._response_timeout_override

        airtime_ms = self._estimate_response_airtime_ms()
        if airtime_ms <= 0:
            return FALLBACK_RESPONSE_TIMEOUT_SEC

        deferral_ms = 5.0 * airtime_ms * max(0.0, self._direct_tx_delay_factor)
        total_ms = (
            SERVER_RESPONSE_DELAY_MS + deferral_ms + (2.0 * airtime_ms) + RESPONSE_TIMEOUT_SLACK_MS
        )
        return min(MAX_RESPONSE_TIMEOUT_SEC, max(MIN_RESPONSE_TIMEOUT_SEC, total_ms / 1000.0))

    def _estimate_response_airtime_ms(self) -> float:
        if not self.airtime_manager:
            return 0.0
        try:
            # Worst-case response size, matching the firmware's
            # getEstAirtimeFor(MAX_PACKET_PAYLOAD + 2).
            return float(self.airtime_manager.calculate_airtime(MAX_PACKET_PAYLOAD + 2))
        except Exception as e:
            logger.debug(f"Could not estimate response airtime: {e}")
            return 0.0

    # ------------------------------------------------------------------
    # Sweep
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._sweep_lock.locked()

    async def sweep(self, targets: List[NeighborSnapshot]) -> Dict[str, ScopeResult]:
        """Query every target's scopes, one request in flight at a time.

        Returns a result per target keyed by lowercase pubkey hex. Targets that
        are never reached (sweep budget or duty-cycle backlog) come back as
        ``timeout``, matching the firmware's treatment of unsent entries.
        """
        results: Dict[str, ScopeResult] = {}
        if not targets:
            return results

        if self._sweep_lock.locked():
            raise RuntimeError("neighbor scope sweep already active")

        async with self._sweep_lock:
            # Pick up live config edits (the mesh CLI can change
            # delays.direct_tx_delay_factor, which sizes the response window).
            self.refresh_config(self.config)
            deadline = time.monotonic() + self._max_sweep_seconds
            timeout = self.response_timeout()
            logger.info(
                "Scope sweep starting: %d neighbour(s), %.1fs response window, %.0fs sweep budget",
                len(targets),
                timeout,
                self._max_sweep_seconds,
            )

            abandoned = False
            for target in targets:
                if abandoned or time.monotonic() >= deadline:
                    if not abandoned:
                        logger.warning(
                            "Scope sweep budget exhausted; %s and later marked timeout",
                            target.pubkey[:8],
                        )
                        abandoned = True
                    results[target.pubkey] = ScopeResult(STATUS_TIMEOUT)
                    continue

                result = await self._query_one(target, timeout)
                results[target.pubkey] = result

                if result.status == STATUS_SEND_FAILED and self._duty_cycle_backlogged():
                    # The airtime budget is gone; the rest of the sweep would sit
                    # behind forwarding traffic. Publish what resolved instead.
                    logger.warning("Scope sweep abandoned: duty-cycle budget exhausted")
                    abandoned = True

            responded = sum(1 for r in results.values() if r.status == STATUS_RESPONDED)
            logger.info("Scope sweep complete: %d/%d responded", responded, len(results))
            return results

    async def _query_one(self, target: NeighborSnapshot, timeout: float) -> ScopeResult:
        if not self.packet_injector:
            logger.warning("No packet injector available - cannot query neighbour scopes")
            return ScopeResult(STATUS_SEND_FAILED)

        try:
            packet, tag = self._build_request(target)
        except Exception as e:
            logger.warning(f"Could not build scope request for {target.pubkey[:8]}: {e}")
            return ScopeResult(STATUS_SEND_FAILED)

        if not self._duty_cycle_allows(packet):
            logger.warning(
                "Skipping scope query for %s: duty-cycle budget exhausted", target.pubkey[:8]
            )
            return ScopeResult(STATUS_SEND_FAILED)

        loop = asyncio.get_running_loop()
        pending = _PendingQuery(
            pubkey=target.pubkey,
            tag=tag,
            src_hash=target.pubkey_bytes[0],
            future=loop.create_future(),
        )
        self._pending = pending

        # One finally for the whole method: the injector await spends most of its
        # wall time in the engine TX path, so a shutdown cancel lands there. A
        # CancelledError escaping with _pending still set would leave a dead query
        # matching (and hiding from the companion bridges) every later RESPONSE.
        try:
            try:
                # Resolves only once the packet is actually on air (or has failed):
                # the engine awaits dispatcher.send_packet under its TX lock and
                # defers local TX until the duty cycle allows. This is the firmware's
                # logTx / logTxFail boundary, which is where the response deadline is
                # armed -- hence the wait_for below, and not a moment earlier.
                sent = await self.packet_injector(packet, wait_for_ack=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Scope request send failed for {target.pubkey[:8]}: {e}")
                return ScopeResult(STATUS_SEND_FAILED)

            if not sent:
                logger.debug(f"Scope request not transmitted for {target.pubkey[:8]}")
                return ScopeResult(STATUS_SEND_FAILED)

            # Past this point the request is on air, so every outcome counts as
            # queried regardless of whether the neighbour answers.
            try:
                scopes = await asyncio.wait_for(pending.future, timeout)
            except asyncio.TimeoutError:
                logger.debug(f"Scope query timed out for {target.pubkey[:8]}")
                return ScopeResult(STATUS_TIMEOUT, transmitted=True)
            logger.debug(f"Scope response from {target.pubkey[:8]}: '{scopes}'")
            return ScopeResult(STATUS_RESPONDED, scopes, transmitted=True)
        finally:
            self._pending = None

    def _build_request(self, target: NeighborSnapshot):
        from openhop_core.protocol.packet_builder import PacketBuilder

        # tag(4) is prepended by create_anon_request as the request timestamp;
        # ANON_REQ_TYPE_REGIONS selects the scopes reply and 0x00 asks for a
        # zero-hop reply path (firmware inner[4], inner[5]).
        return PacketBuilder.create_anon_request(
            _ScopeTarget(target.pubkey),
            self.local_identity,
            req_data=bytes([ANON_REQ_TYPE_REGIONS, 0x00]),
        )

    # ------------------------------------------------------------------
    # Duty cycle
    # ------------------------------------------------------------------
    def _duty_cycle_allows(self, packet) -> bool:
        """Pre-flight the airtime budget.

        Stands in for the firmware's ``getFreeCount() >= 5`` packet-pool guard:
        different scarce resource, same job of refusing to enqueue a request the
        transmit path cannot honour. A short backlog is fine — the engine defers
        local TX on its own — so only a long one is treated as a failure.
        """
        if not self.airtime_manager:
            return True
        try:
            airtime_ms = self.airtime_manager.calculate_airtime(packet.get_raw_length())
            can_tx, wait_time = self.airtime_manager.can_transmit(airtime_ms)
        except Exception as e:
            logger.debug(f"Duty-cycle pre-flight failed, allowing send: {e}")
            return True
        return can_tx or wait_time <= self._duty_cycle_abort_seconds

    def _duty_cycle_backlogged(self) -> bool:
        if not self.airtime_manager:
            return False
        try:
            airtime_ms = self.airtime_manager.calculate_airtime(MAX_PACKET_PAYLOAD)
            can_tx, wait_time = self.airtime_manager.can_transmit(airtime_ms)
        except Exception:
            return False
        return not can_tx and wait_time > self._duty_cycle_abort_seconds

    # ------------------------------------------------------------------
    # Response matching
    # ------------------------------------------------------------------
    async def process_response_packet(self, packet) -> bool:
        """Consume a PAYLOAD_TYPE_RESPONSE that answers the in-flight query.

        Returns True only when the packet decrypts under the pending neighbour's
        shared secret *and* echoes its tag, so an unrelated response still falls
        through to the companion bridges. Like firmware
        ``handleNeighborDiscoverResponse``, a reply that arrives after its entry
        has timed out is not accepted.
        """
        pending = self._pending
        if pending is None or pending.future is None or pending.future.done():
            return False

        try:
            if packet.get_payload_type() != PAYLOAD_TYPE_RESPONSE:
                return False

            payload = bytes(getattr(packet, "payload", b"") or b"")
            if len(payload) < 3:
                return False

            our_hash = self.local_identity.get_public_key()[0]
            if payload[0] != our_hash or payload[1] != pending.src_hash:
                return False

            peer = Identity(bytes.fromhex(pending.pubkey))
            shared_secret = peer.calc_shared_secret(self.local_identity.get_private_key())
            plaintext = CryptoUtils.mac_then_decrypt(shared_secret[:16], shared_secret, payload[2:])
            if not plaintext or len(plaintext) < 8:
                return False

            # plaintext: tag(4) + responder clock(4) + comma-separated scope names
            if int.from_bytes(plaintext[:4], "little") != pending.tag:
                return False

            # The responder builds this field as a C string and the block cipher
            # zero-pads the tail, so stop at the first NUL exactly as the firmware
            # reader does -- rstrip alone would let "DEN\x00junk" through.
            raw_scopes = bytes(plaintext[8:]).split(b"\x00", 1)[0]
            scopes = raw_scopes.decode("utf-8", errors="replace").strip()
            if not pending.future.done():
                pending.future.set_result(scopes)
            return True

        except Exception as e:
            logger.debug(f"Error matching scope response: {e}")
            return False


def _as_float(value, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
