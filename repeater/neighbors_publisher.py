"""Periodic neighbours publication for the MQTT ``neighbors`` topic.

Python port of the firmware's two-stage neighbours cycle
(MeshCore ``simple_repeater/MyMesh.cpp``, ``WITH_MQTT_NEIGHBORS``):

* **Stage 1** — a zero-hop node-discovery broadcast refreshes the neighbour table.
  Firmware calls ``sendNodeDiscoverReq()`` and waits out its collection window;
  here that is a :class:`~repeater.handler_helpers.discovery.DiscoveryHelper`
  session, whose results are persisted by the same enricher the ``discover.neighbors``
  CLI command uses.
* **Stage 2** — one anon-regions scope query per neighbour, issued serially by
  :class:`~repeater.handler_helpers.neighbor_scopes.NeighborScopeHelper`.
* **Publish** — the assembled table goes to every enabled broker that opted in
  with ``neighbors: true``.

The published table is the whole zero-hop neighbour snapshot, including entries
that did not answer the scope query (``timeout`` / ``send_failed``), matching the
firmware payload. Unlike the firmware there is no 10 KB cap: the ESP32 needed a
fixed PSRAM buffer, a Linux host does not.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from repeater.handler_helpers.neighbor_scopes import (
    STATUS_RESPONDED,
    STATUS_TIMEOUT,
    NeighborSnapshot,
    ScopeResult,
)

logger = logging.getLogger("NeighborsPublisher")

# Firmware parity: mqtt.neighbors.interval accepts 12-336 hours, default 24, and
# rejects out-of-range values rather than clamping them.
MIN_INTERVAL_HOURS = 12
MAX_INTERVAL_HOURS = 336
DEFAULT_INTERVAL_HOURS = 24

# Firmware waits out the 60 s node-discover collection window before querying scopes.
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 60.0

# Firmware bounds the table by MAX_NEIGHBOURS; this is the openhop equivalent so a
# large advert history cannot turn into an unbounded sweep.
DEFAULT_MAX_NEIGHBORS = 32

# Zero-hop rows older than this are treated as gone and left out of the pass.
DEFAULT_MAX_NEIGHBOR_AGE_SECONDS = 86400.0

# How often the loop wakes to re-evaluate its schedule.
_TICK_SECONDS = 30.0

# Delay before retrying a cycle that failed or published nothing, instead of
# waiting out the full interval.
RETRY_DELAY_SECONDS = 900.0

# Phases reported by status(), mirroring the firmware's NeighborsPhase.
PHASE_DISABLED = "disabled"
PHASE_SCHEDULED = "scheduled"
PHASE_ACTIVE = "active"
PHASE_DUE = "due"


def build_neighbors_payload(
    *,
    origin: str,
    origin_id: str,
    self_scopes: str,
    entries: List[dict],
    timestamp: Optional[str] = None,
) -> dict:
    """Assemble the ``neighbors`` topic payload.

    ``entries`` are ordered most- to least-useful (most recently heard first, then
    stronger SNR, then pubkey) exactly as the firmware orders them. The firmware
    needs that order so it can drop the tail when its fixed buffer fills; we keep
    it because it is the documented shape of the topic and it puts the useful rows
    first for consumers.
    """
    ordered = sorted(
        entries,
        key=lambda e: (e.get("heard_secs_ago", 0), -float(e.get("snr", 0.0)), e.get("pubkey", "")),
    )
    return {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "origin_id": origin_id,
        "self": {"scopes": self_scopes or ""},
        "neighbors": ordered,
    }


class NeighborsPublisher:
    """Owns the periodic neighbours cycle and its manual trigger."""

    def __init__(
        self,
        config: dict,
        *,
        local_identity=None,
        discovery_helper=None,
        scope_helper=None,
        mqtt_handler_provider: Optional[Callable[[], Any]] = None,
        storage_provider: Optional[Callable[[], Any]] = None,
        self_scopes_fn: Optional[Callable[[], str]] = None,
    ):
        self.config = config
        self.local_identity = local_identity
        self.discovery_helper = discovery_helper
        self.scope_helper = scope_helper
        self._mqtt_handler_provider = mqtt_handler_provider
        self._storage_provider = storage_provider
        self._self_scopes_fn = self_scopes_fn

        self._task: Optional[asyncio.Task] = None
        self._manual_task: Optional[asyncio.Task] = None
        self._running = False
        self._active = False
        # Discovery responses collected during the current cycle, keyed by pubkey.
        self._discovery_seen: Dict[str, dict] = {}
        self._next_publish_at: Optional[float] = None
        self._last_result: Optional[str] = None
        self._last_publish_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Config accessors (re-read every cycle so live edits take effect)
    # ------------------------------------------------------------------
    @property
    def _neighbors_config(self) -> dict:
        return (self.config.get("mqtt_brokers", {}) or {}).get("neighbors", {}) or {}

    @property
    def master_enabled(self) -> bool:
        """Feature kill switch. Defaults on; the per-broker flags are the control."""
        return bool(self._neighbors_config.get("enabled", True))

    @property
    def interval_seconds(self) -> float:
        hours = self._neighbors_config.get("interval_hours", DEFAULT_INTERVAL_HOURS)
        return normalize_interval_hours(hours) * 3600.0

    @property
    def discovery_timeout(self) -> float:
        try:
            value = float(
                self._neighbors_config.get(
                    "discovery_timeout_seconds", DEFAULT_DISCOVERY_TIMEOUT_SECONDS
                )
            )
        except (TypeError, ValueError):
            return DEFAULT_DISCOVERY_TIMEOUT_SECONDS
        return max(1.0, value)

    @property
    def max_neighbors(self) -> int:
        try:
            value = int(self._neighbors_config.get("max_neighbors", DEFAULT_MAX_NEIGHBORS))
        except (TypeError, ValueError):
            return DEFAULT_MAX_NEIGHBORS
        return max(1, value)

    @property
    def max_neighbor_age(self) -> float:
        try:
            value = float(
                self._neighbors_config.get(
                    "max_neighbor_age_seconds", DEFAULT_MAX_NEIGHBOR_AGE_SECONDS
                )
            )
        except (TypeError, ValueError):
            return DEFAULT_MAX_NEIGHBOR_AGE_SECONDS
        return max(1.0, value)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="neighbors-publisher")
        logger.info("Neighbors publisher started (interval %.1fh)", self.interval_seconds / 3600.0)

    def trigger_cycle(self) -> bool:
        """Start a manual cycle, tracked so shutdown can cancel it.

        A cycle runs for minutes (a discovery window plus one serialized scope
        query per neighbour), so an untracked task would keep transmitting while
        the daemon tears down. Returns False when a cycle is already running.
        """
        if self._active or (self._manual_task is not None and not self._manual_task.done()):
            return False
        self._manual_task = asyncio.create_task(
            self.run_cycle(trigger="manual"), name="neighbors-manual-cycle"
        )
        self._manual_task.add_done_callback(self._on_manual_task_done)
        return True

    def _on_manual_task_done(self, task: asyncio.Task) -> None:
        if self._manual_task is task:
            self._manual_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Manual neighbors cycle failed: {e}", exc_info=True)

    async def stop(self) -> None:
        self._running = False
        tasks = [t for t in (self._task, self._manual_task) if t and not t.done()]
        self._task = None
        self._manual_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Neighbors publisher shutdown error ignored: {e}")
        logger.info("Neighbors publisher stopped")

    def status(self) -> dict:
        if not self.enabled():
            phase = PHASE_DISABLED
        elif self._active:
            phase = PHASE_ACTIVE
        elif self._next_publish_at is None or time.monotonic() >= self._next_publish_at:
            phase = PHASE_DUE
        else:
            phase = PHASE_SCHEDULED

        secs_until_next = None
        if phase == PHASE_SCHEDULED and self._next_publish_at is not None:
            secs_until_next = max(0, int(self._next_publish_at - time.monotonic()))

        return {
            "phase": phase,
            "secs_until_next": secs_until_next,
            "last_result": self._last_result,
            "last_publish_at": self._last_publish_at,
            "interval_hours": self.interval_seconds / 3600.0,
        }

    def enabled(self) -> bool:
        """True when the master switch is on and some broker opted in."""
        if not self.master_enabled:
            return False
        handler = self._mqtt_handler()
        return bool(handler and handler.has_neighbors_brokers())

    def _mqtt_handler(self):
        if not self._mqtt_handler_provider:
            return None
        try:
            return self._mqtt_handler_provider()
        except Exception as e:
            logger.debug(f"MQTT handler unavailable: {e}")
            return None

    def _storage(self):
        if not self._storage_provider:
            return None
        try:
            return self._storage_provider()
        except Exception as e:
            logger.debug(f"Storage unavailable: {e}")
            return None

    def _local_pubkey_hex(self) -> str:
        if not self.local_identity:
            return ""
        try:
            return self.local_identity.get_public_key().hex().lower()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def _run_loop(self) -> None:
        try:
            while self._running:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Neighbors publisher cycle failed: {e}", exc_info=True)
                    self._last_result = f"error: {e}"
                    self._reschedule(retry=True)
                await asyncio.sleep(_TICK_SECONDS)
        except asyncio.CancelledError:
            logger.debug("Neighbors publisher loop cancelled")
            raise

    async def _tick(self) -> None:
        if not self.enabled():
            # Drop the schedule so re-enabling runs a pass promptly, matching the
            # firmware's next_neighbors_publish = 0 reset.
            self._next_publish_at = None
            return

        handler = self._mqtt_handler()
        if not handler or not handler.has_connected_neighbors_brokers():
            logger.debug("Neighbors publish deferred: no connected opted-in broker")
            return

        now = time.monotonic()
        if self._next_publish_at is not None and now < self._next_publish_at:
            return

        await self.run_cycle(trigger="periodic")

    def _reschedule(self, *, retry: bool = False) -> None:
        """Arm the next cycle.

        A cycle that produced no publish retries on a short delay instead of
        burning the whole interval: the usual cause is a broker that was briefly
        unreachable or rejected the payload, and waiting a day to find out
        otherwise is not useful.
        """
        delay = min(RETRY_DELAY_SECONDS, self.interval_seconds) if retry else self.interval_seconds
        self._next_publish_at = time.monotonic() + delay

    # ------------------------------------------------------------------
    # Cycle
    # ------------------------------------------------------------------
    async def run_cycle(self, trigger: str = "manual") -> dict:
        """Run one full discovery + scopes + publish cycle."""
        if self._active:
            return {"success": False, "error": "neighbors cycle already active"}

        self._active = True
        started = time.monotonic()
        self._discovery_seen = {}
        published = False
        try:
            await self._refresh_neighbor_table()
            targets = self._snapshot_neighbors()

            scope_results: Dict[str, ScopeResult] = {}
            if targets and self.scope_helper:
                scope_results = await self.scope_helper.sweep(targets)
            elif targets:
                logger.warning("No scope helper available; publishing without scopes")

            payload = self._build_payload(targets, scope_results)
            published = self._publish(payload)

            responded = sum(1 for r in scope_results.values() if r.status == STATUS_RESPONDED)
            self._last_result = (
                f"ok ({len(targets)} neighbours, {responded} with scopes)"
                if published
                else "publish failed (broker unreachable or rejected the payload)"
            )
            self._last_publish_at = time.time()
            logger.info(
                "Neighbors %s cycle finished in %.1fs: %d neighbour(s), %d with scopes, "
                "published=%s",
                trigger,
                time.monotonic() - started,
                len(targets),
                responded,
                published,
            )
            return {
                "success": True,
                "neighbors": len(targets),
                "responded": responded,
                "published": published,
            }
        finally:
            self._active = False
            self._reschedule(retry=not published)

    async def _refresh_neighbor_table(self) -> None:
        """Stage 1: zero-hop node discovery, awaited to completion.

        ``prefix_only=False`` is required, not cosmetic: the scope query needs the
        neighbour's full 32-byte public key to derive a shared secret.
        """
        if not self.discovery_helper:
            logger.debug("No discovery helper; using the stored neighbour table as-is")
            return

        try:
            self.discovery_helper.cleanup_sessions()
            session = self.discovery_helper.create_session(
                timeout=self.discovery_timeout,
                filter_mask=(1 << 2),  # repeaters
                since=0,
                prefix_only=False,
                result_enricher=self._enrich_discovery_result,
            )
            await self.discovery_helper.execute_session(session["session_id"])
        except Exception as e:
            logger.warning(f"Neighbour table refresh failed, using stored table: {e}")

    def _enrich_discovery_result(self, result: dict) -> dict:
        """Record each discovery response for this cycle, and persist it.

        This is what makes stage 1 matter: without it the snapshot would report
        whatever ``last_seen``/``snr`` the advert history happened to hold, rather
        than what answered just now. Mirrors firmware ``putNeighbour()`` on a
        discovery response.

        The in-memory copy is not redundant with the write. ``get_neighbors()``
        serves a 60 s cache that ``store_advert()`` does not invalidate, so a
        neighbour that answered seconds ago can be missing from, or stale in, the
        table read that follows. The snapshot merges this dict over that read.
        """
        pubkey = str(result.get("pub_key") or "").strip().lower()
        if not pubkey or len(pubkey) != 64:
            return result

        local_pubkey = self._local_pubkey_hex()
        if local_pubkey and pubkey == local_pubkey:
            return result

        node_type_raw = int(result.get("node_type", 0) or 0)
        snr_raw = result.get("response_snr", result.get("snr"))
        if node_type_raw == 2:
            self._discovery_seen[pubkey] = {
                "last_seen": time.time(),
                "snr": float(snr_raw) if snr_raw is not None else 0.0,
            }

        storage = self._storage()
        record_advert = getattr(storage, "record_advert", None) if storage else None
        if not callable(record_advert):
            return result

        node_type = int(result.get("node_type", 0) or 0)
        rssi = result.get("rssi")
        snr = result.get("response_snr", result.get("snr"))
        try:
            record_advert(
                {
                    "timestamp": time.time(),
                    "pubkey": pubkey,
                    "node_name": result.get("node_name"),
                    "is_repeater": node_type == 2,
                    "route_type": 2,
                    "contact_type": {1: "Chat Node", 2: "Repeater", 3: "Room Server"}.get(
                        node_type, "Unknown"
                    ),
                    "latitude": None,
                    "longitude": None,
                    "rssi": int(rssi) if rssi is not None else None,
                    "snr": float(snr) if snr is not None else None,
                    "is_new_neighbor": True,
                    "zero_hop": True,
                }
            )
        except Exception as e:
            logger.debug(f"Could not persist discovery result for {pubkey[:8]}: {e}")

        return result

    def _snapshot_neighbors(self) -> List[NeighborSnapshot]:
        """Freeze the zero-hop repeater table for this pass.

        Taken once, up front: the sweep can run for minutes and the table keeps
        changing underneath it (firmware hit the same problem and stopped indexing
        into its live ``neighbours[]`` in ``aba571ed``).

        Sources are the stored zero-hop repeater table (which also carries
        neighbours heard via advert but silent during discovery, as the firmware's
        table does) merged with this cycle's discovery responses, which win on
        ``last_seen``/``snr`` because they are first-hand and the table read is
        served from a cache the advert write does not invalidate.
        """
        storage = self._storage()
        neighbors = {}
        if storage:
            try:
                neighbors = storage.get_neighbors() or {}
            except Exception as e:
                logger.warning(f"Could not read neighbour table: {e}")

        local_pubkey = self._local_pubkey_hex()
        now = time.time()
        max_age = self.max_neighbor_age

        merged: Dict[str, dict] = {}
        for pubkey, info in neighbors.items():
            if not info.get("is_repeater") or not info.get("zero_hop"):
                continue
            key = str(pubkey or "").lower()
            if len(key) != 64:
                # Scope queries need the full key for ECDH; a prefix cannot be used.
                continue
            last_seen = float(info.get("last_seen") or 0.0)
            if last_seen <= 0 or (now - last_seen) > max_age:
                continue
            merged[key] = {"last_seen": last_seen, "snr": float(info.get("snr") or 0.0)}

        for key, seen in (self._discovery_seen or {}).items():
            merged[key] = dict(seen)

        snapshots = [
            NeighborSnapshot(pubkey=key, last_seen=info["last_seen"], snr=info["snr"])
            for key, info in merged.items()
            if not (local_pubkey and key == local_pubkey)
        ]

        # Freshest first, then strongest, then pubkey for a deterministic order --
        # the same ordering the firmware applies before it starts querying.
        snapshots.sort(key=lambda s: (-s.last_seen, -s.snr, s.pubkey))
        if len(snapshots) > self.max_neighbors:
            logger.info(
                "Neighbour table has %d entries; querying the freshest %d",
                len(snapshots),
                self.max_neighbors,
            )
            snapshots = snapshots[: self.max_neighbors]
        return snapshots

    def _build_payload(
        self, targets: List[NeighborSnapshot], scope_results: Dict[str, ScopeResult]
    ) -> dict:
        now = time.time()
        entries = []
        for target in targets:
            result = scope_results.get(target.pubkey) or ScopeResult(STATUS_TIMEOUT)
            heard_secs_ago = int(max(0.0, now - target.last_seen)) if target.last_seen else 0
            entries.append(
                {
                    "pubkey": target.pubkey,
                    "snr": round(target.snr, 2),
                    "heard_secs_ago": heard_secs_ago,
                    "scopes": result.scopes or "",
                    "status": result.status,
                }
            )

        handler = self._mqtt_handler()
        origin = getattr(handler, "node_name", "") if handler else ""
        origin_id = getattr(handler, "public_key", "") if handler else ""
        if not origin:
            origin = self.config.get("repeater", {}).get("node_name", "openHop-Repeater")
        if not origin_id and self.local_identity:
            try:
                origin_id = self.local_identity.get_public_key().hex().upper()
            except Exception:
                origin_id = ""

        return build_neighbors_payload(
            origin=origin,
            origin_id=origin_id,
            self_scopes=self._self_scopes(),
            entries=entries,
        )

    def _self_scopes(self) -> str:
        if not self._self_scopes_fn:
            return ""
        try:
            return self._self_scopes_fn() or ""
        except Exception as e:
            logger.debug(f"Could not read local scopes: {e}")
            return ""

    def _publish(self, payload: dict) -> bool:
        handler = self._mqtt_handler()
        if not handler:
            return False
        try:
            results = handler.publish_neighbors(payload)
        except Exception as e:
            logger.error(f"Neighbors publish failed: {e}")
            return False
        return bool(results)


def normalize_interval_hours(value) -> float:
    """Validate an interval in hours, falling back to the default when invalid.

    Firmware rejects out-of-range values instead of clamping them; the API layer
    surfaces that rejection to the user, and this fallback keeps a hand-edited
    config.yaml from producing a nonsense schedule.
    """
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_INTERVAL_HOURS)
    if hours < MIN_INTERVAL_HOURS or hours > MAX_INTERVAL_HOURS:
        logger.warning(
            "mqtt_brokers.neighbors.interval_hours=%s outside %d-%d; using %d",
            value,
            MIN_INTERVAL_HOURS,
            MAX_INTERVAL_HOURS,
            DEFAULT_INTERVAL_HOURS,
        )
        return float(DEFAULT_INTERVAL_HOURS)
    return hours
