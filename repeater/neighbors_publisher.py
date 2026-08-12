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

from repeater.handler_helpers.discovery import persist_discovery_result
from repeater.handler_helpers.neighbor_scopes import (
    STATUS_RESPONDED,
    STATUS_SEND_FAILED,
    STATUS_TIMEOUT,
    NeighborSnapshot,
    ScopeResult,
    neighbors_config_block,
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

# How an unset default region is spelled in the payload. Matches the wildcard
# LoginHelper._format_region_names already emits for unscoped flood.
DEFAULT_SCOPE_WILDCARD = "*"

# daemon_state key holding the persisted schedule.
STATE_KEY = "neighbors_publisher"

# A restored schedule never fires inside this window after boot, even when it is
# already overdue. Keeps a restart quiet and lets the radio and brokers settle
# before a cycle claims the airtime. Firmware has no equivalent -- it treats
# every boot as immediately due.
STARTUP_GRACE_SECONDS = 300.0

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
    total_neighbors: Optional[int] = None,
    queried_neighbors: Optional[int] = None,
    self_default_scope: str = DEFAULT_SCOPE_WILDCARD,
) -> dict:
    """Assemble the ``neighbors`` topic payload.

    ``entries`` are ordered most- to least-useful (most recently heard first, then
    stronger SNR, then pubkey) exactly as the firmware orders them. The firmware
    needs that order so it can drop the tail when its fixed buffer fills; we keep
    it because it is the documented shape of the topic and it puts the useful rows
    first for consumers.

    ``self`` carries this node's own advertised scopes plus ``default_scope``, the
    region it stamps on outgoing floods (``*`` when it floods unscoped). The
    firmware tracks a ``default_scope`` internally but does not publish it; it is
    included here because a consumer reading the table cannot otherwise tell which
    of several scopes this node actually transmits under.

    Two progress counters mirror firmware ``buildNeighborsMessage``:

    * ``total_neighbors`` — neighbours in this cycle's table, which is also how
      many rows ``neighbors`` carries.
    * ``queried_neighbors`` — how many scope requests reached the air.

    The firmware's third field, ``truncated``, is deliberately not emitted: it
    reports that a fixed PSRAM JSON buffer filled and the tail was dropped, and
    openhop has no such buffer, so it could only ever be false. That also keeps
    ``total_neighbors`` equal to the published row count here, where firmware
    allows it to run ahead. Both are emitted only when the caller supplies the
    counts, matching the firmware's ``total_neighbors >= 0`` guard.
    """
    ordered = sorted(
        entries,
        key=lambda e: (e.get("heard_secs_ago", 0), -float(e.get("snr", 0.0)), e.get("pubkey", "")),
    )
    payload = {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "origin_id": origin_id,
    }
    # Key order matches the firmware writer so the two payloads diff cleanly.
    if total_neighbors is not None:
        payload["total_neighbors"] = int(total_neighbors)
        payload["queried_neighbors"] = int(
            queried_neighbors if queried_neighbors is not None else total_neighbors
        )
    payload["self"] = {
        "scopes": self_scopes or "",
        "default_scope": self_default_scope or DEFAULT_SCOPE_WILDCARD,
    }
    payload["neighbors"] = ordered
    return payload


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
        # Single-neighbour queries running outside a cycle. A cycle must not start
        # on top of one: it would reach the sweep and find the helper's lock held,
        # which raises and costs the whole cycle (including its discovery
        # broadcast). Tracked as a count, and as tasks so shutdown can cancel them.
        self._queries_in_flight = 0
        self._query_tasks: set = set()
        # Discovery responses collected during the current cycle, keyed by pubkey.
        self._discovery_seen: Dict[str, dict] = {}
        self._next_publish_at: Optional[float] = None
        self._last_result: Optional[str] = None
        # When the last cycle finished, whatever its outcome -- this is what
        # status() reports alongside last_result.
        self._last_publish_at: Optional[float] = None
        # When a cycle last actually reached a broker. The schedule is measured
        # from this, not from the above: a cycle that failed to publish reschedules
        # on the short retry delay, and restoring from a failed attempt would
        # silently turn that retry into a full interval.
        self._last_success_at: Optional[float] = None
        # Whether the feature has been enabled at any point in this process. The
        # disabled branch of _tick clears the schedule so re-enabling publishes
        # promptly, which must not fire before we have ever been enabled -- that
        # would throw away a schedule just restored from disk.
        self._was_enabled = False

    # ------------------------------------------------------------------
    # Config accessors (re-read every cycle so live edits take effect)
    # ------------------------------------------------------------------
    @property
    def _neighbors_config(self) -> dict:
        return neighbors_config_block(self.config)

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
        # Say so once, loudly, rather than from the config accessors: those run
        # several times per tick and would bury the log.
        raw_block = (self.config.get("mqtt_brokers") or {}) if isinstance(self.config, dict) else {}
        if isinstance(raw_block, dict) and not isinstance(raw_block.get("neighbors", {}), dict):
            logger.warning(
                "mqtt_brokers.neighbors is %s, not a settings block - using defaults. "
                "The per-broker 'neighbors: true' flag is what opts a broker in.",
                type(raw_block.get("neighbors")).__name__,
            )
        self._restore_schedule()
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="neighbors-publisher")
        logger.info("Neighbors publisher started (interval %.1fh)", self.interval_seconds / 3600.0)

    # ------------------------------------------------------------------
    # Schedule persistence
    # ------------------------------------------------------------------
    def _restore_schedule(self) -> None:
        """Resume the schedule from the last publish instead of restarting it.

        Without this a restart leaves ``_next_publish_at`` unset, which reads as
        "due" and spends a discovery broadcast plus a serialized scope query per
        neighbour on every boot. ``_next_publish_at`` is monotonic and so cannot
        be stored directly; the persisted value is the wall-clock publish time,
        converted back to a monotonic deadline here.
        """
        storage = self._storage()
        reader = getattr(storage, "get_daemon_state", None) if storage else None
        if not callable(reader):
            # Older storage backend, or none wired up: behave as before.
            self._next_publish_at = time.monotonic() + STARTUP_GRACE_SECONDS
            return

        state = reader(STATE_KEY) or {}
        last = _as_epoch(state.get("last_success_at"))

        # Restore the display fields too, so a restart does not report the node as
        # having never run.
        self._last_result = state.get("last_result") or None
        self._last_publish_at = _as_epoch(state.get("last_publish_at")) or None
        interval = self.interval_seconds
        now = time.time()

        if last <= 0 or last > now:
            # No successful publish on record, or a timestamp from the future --
            # the clock moved backwards, or the row is junk. Either way it cannot
            # place the next cycle, so fall back to the grace delay.
            if last > now:
                logger.warning(
                    "Persisted neighbours publish time is %.0fs in the future; ignoring it",
                    last - now,
                )
            self._next_publish_at = time.monotonic() + STARTUP_GRACE_SECONDS
            return

        self._last_success_at = last
        # Never sooner than the grace window, never later than a full interval
        # from now -- the latter bounds the damage from an interval that shrank
        # since the last publish.
        delay = min(max((last + interval) - now, STARTUP_GRACE_SECONDS), interval)
        self._next_publish_at = time.monotonic() + delay
        logger.info(
            "Neighbours schedule resumed: last published %.1fh ago, next in %.1fh",
            (now - last) / 3600.0,
            delay / 3600.0,
        )

    def _persist_schedule(self) -> None:
        storage = self._storage()
        writer = getattr(storage, "set_daemon_state", None) if storage else None
        if not callable(writer):
            return
        writer(
            STATE_KEY,
            {
                "last_success_at": self._last_success_at,
                "last_publish_at": self._last_publish_at,
                "last_result": self._last_result,
            },
        )

    def trigger_cycle(self) -> bool:
        """Start a manual cycle, tracked so shutdown can cancel it.

        A cycle runs for minutes (a discovery window plus one serialized scope
        query per neighbour), so an untracked task would keep transmitting while
        the daemon tears down. Returns False when a cycle is already running.
        """
        if self._active or (self._manual_task is not None and not self._manual_task.done()):
            return False
        if self._queries_in_flight:
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
        tasks = [
            t
            for t in (self._task, self._manual_task, *self._query_tasks)
            if t and not t.done() and t is not asyncio.current_task()
        ]
        self._task = None
        self._manual_task = None
        self._query_tasks.clear()
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
            # firmware's next_neighbors_publish = 0 reset. Only once the feature
            # has actually been on in this process, though: at boot the MQTT
            # handler may not have its connections up yet, and clearing here would
            # discard the schedule just restored from disk and re-run the sweep
            # anyway -- the exact thing persistence exists to prevent.
            if self._was_enabled:
                self._next_publish_at = None
            return

        self._was_enabled = True
        handler = self._mqtt_handler()
        if not handler or not handler.has_connected_neighbors_brokers():
            logger.debug("Neighbors publish deferred: no connected opted-in broker")
            return

        now = time.monotonic()
        if self._next_publish_at is not None and now < self._next_publish_at:
            return

        if self._queries_in_flight:
            # Deferred, not rescheduled: the next tick is 30 s away and a single
            # query is far shorter than that, so the cycle simply runs then.
            logger.debug("Neighbors cycle deferred: a manual scope query is in flight")
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
                try:
                    scope_results = await self.scope_helper.sweep(targets)
                except RuntimeError as e:
                    # A manual query took the helper between the checks in _tick /
                    # trigger_cycle and here -- the discovery window above leaves a
                    # wide gap for that. Give up on this pass rather than publish a
                    # table with every scope missing; the finally below reschedules
                    # on the short retry delay.
                    logger.warning("Neighbors cycle abandoned: %s", e)
                    self._last_result = f"deferred: {e}"
                    return {"success": False, "error": str(e)}
                self._persist_scope_results(scope_results)
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
            if published:
                self._last_success_at = self._last_publish_at
            self._persist_schedule()
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

    @staticmethod
    def _was_asked(result: ScopeResult) -> bool:
        """Whether this outcome represents a query the node actually attempted.

        ``timeout`` is ambiguous on its own: the sweep reports it both for a
        neighbour that was asked and stayed silent and for one it never reached
        (budget exhausted), so ``transmitted`` is what separates those. A
        ``send_failed`` was attempted and refused by the transmit path -- the
        duty-cycle pre-flight -- which is worth recording even though nothing went
        on air, otherwise a repeater that keeps refusing reads as "never queried"
        forever.
        """
        return result.transmitted or result.status == STATUS_SEND_FAILED

    def _persist_scope_results(
        self, results: Dict[str, ScopeResult], now: Optional[float] = None
    ) -> None:
        """Store what a sweep learned, so it outlives the MQTT publish.

        The payload used to be the only consumer, which left the web UI with
        nothing to show between cycles. One row per neighbour the node actually
        asked (see :meth:`_was_asked`); targets the sweep never reached are left
        untouched rather than credited with a query that never happened.
        """
        storage = self._storage()
        writer = getattr(storage, "record_neighbor_scope", None) if storage else None
        if not callable(writer):
            return

        stamp = time.time() if now is None else now
        for pubkey, result in (results or {}).items():
            if not self._was_asked(result):
                continue
            try:
                # scopes is passed only for an answer, so a failed query keeps the
                # last known value instead of blanking it.
                writer(
                    pubkey,
                    result.status,
                    result.scopes if result.status == STATUS_RESPONDED else None,
                    stamp,
                )
            except Exception as e:
                logger.debug(f"Could not persist scopes for {pubkey[:8]}: {e}")

    async def query_one(self, pubkey: str) -> dict:
        """Query a single neighbour's scopes now, outside the periodic cycle.

        Deliberately independent of ``enabled()``: the answer is stored for the
        web UI, so this is useful on a repeater that publishes to no broker at
        all. Nothing is published as a result -- the periodic cycle owns that.

        Raises ``ValueError`` for a key that cannot be queried and ``RuntimeError``
        when a cycle or another query already holds the scope helper; the caller
        turns both into a message. The cycle check is on ``_active`` rather than on
        the helper's lock because a cycle spends its first minute in the discovery
        window without holding that lock, and colliding later -- once the sweep has
        taken it -- would cost the whole cycle.
        """
        key = str(pubkey or "").strip().lower()
        if len(key) != 64:
            # ECDH against the responder needs the full 32-byte key; a prefix
            # (which is all an advert-only sighting may carry) cannot be used.
            raise ValueError("A full 64-character public key is required")
        try:
            bytes.fromhex(key)
        except ValueError:
            raise ValueError("Public key is not valid hex") from None
        if key == self._local_pubkey_hex():
            raise ValueError("Cannot query this repeater's own scopes")
        if not self.scope_helper:
            raise RuntimeError("Scope helper not available")

        if self._active:
            # A cycle owns the helper for its whole run, including the discovery
            # window before the sweep takes the lock. Refusing here is the mirror
            # of the sweep-side guard below and keeps the two from colliding.
            raise RuntimeError("A neighbours cycle is running - try again once it finishes")

        snapshot = self._snapshot_for(key)
        self._queries_in_flight += 1
        task = asyncio.current_task()
        if task is not None:
            # Tracked for the same reason trigger_cycle tracks its task: this holds
            # the radio for up to a response window, and shutdown has to be able to
            # cut it short rather than transmit through the teardown.
            self._query_tasks.add(task)
        try:
            try:
                results = await self.scope_helper.sweep([snapshot])
            except RuntimeError:
                # The helper's own wording names its internals; say what the
                # operator can act on instead.
                raise RuntimeError(
                    "A neighbour scope sweep is already running - try again once it finishes"
                ) from None

            now = time.time()
            self._persist_scope_results(results, now=now)
            result = results.get(key) or ScopeResult(STATUS_TIMEOUT)
            return self._scope_record(key, result, now)
        finally:
            self._queries_in_flight = max(0, self._queries_in_flight - 1)
            if task is not None:
                self._query_tasks.discard(task)

    def _scope_record(self, pubkey: str, result: ScopeResult, now: float) -> dict:
        """The stored view of one query's outcome, as the API returns it.

        Read back through the stored row rather than reported straight from
        ``result``: a failed query deliberately keeps the neighbour's last known
        scopes, so returning this query's empty string would tell the client to
        forget an answer the database still holds.
        """
        responded = result.status == STATUS_RESPONDED
        record = {
            "pubkey": pubkey,
            "status": result.status,
            "scopes": result.scopes if responded else "",
            "transmitted": result.transmitted,
            "queried_at": now if self._was_asked(result) else None,
            "responded_at": now if responded else None,
        }
        if responded:
            return record

        storage = self._storage()
        reader = getattr(storage, "get_neighbor_scopes", None) if storage else None
        if not callable(reader):
            return record
        try:
            stored = (reader() or {}).get(pubkey) or {}
        except Exception as e:
            logger.debug(f"Could not re-read stored scopes for {pubkey[:8]}: {e}")
            return record
        if stored.get("responded_at") is not None:
            record["scopes"] = stored.get("scopes") or ""
            record["responded_at"] = stored.get("responded_at")
        return record

    def _snapshot_for(self, pubkey: str) -> NeighborSnapshot:
        """Build a one-target snapshot, borrowing last_seen/snr when we have them.

        Neither field affects the query -- they are carried so a single query goes
        through exactly the same path as a sweep entry.
        """
        storage = self._storage()
        if storage:
            try:
                # The adverts table does not normalise key case, so match on the
                # lowercased form rather than indexing directly.
                for candidate, info in (storage.get_neighbors() or {}).items():
                    if str(candidate or "").lower() != pubkey:
                        continue
                    return NeighborSnapshot(
                        pubkey=pubkey,
                        # Same source the cycle snapshot uses: the last DIRECT
                        # reception when known, so both paths describe the
                        # neighbour identically.
                        last_seen=float(
                            info.get("last_zero_hop_seen") or info.get("last_seen") or 0.0
                        ),
                        snr=float(info.get("snr") or 0.0),
                    )
            except Exception as e:
                logger.debug(f"Could not read neighbour row for {pubkey[:8]}: {e}")
        return NeighborSnapshot(pubkey=pubkey)

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

        persist_discovery_result(self._storage(), result)
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

        "Heard via advert" means heard DIRECTLY via advert: freshness is judged
        on ``last_zero_hop_seen``, so a formerly-direct neighbour whose relayed
        floods keep refreshing ``last_seen`` ages out of this table like it does
        out of the firmware's (whose entries only ever update on direct events).
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
            # Freshness is judged on the last DIRECT reception, not last_seen:
            # zero_hop is sticky and last_seen refreshes on relayed adverts too,
            # so a node heard directly once would otherwise stay in the table
            # for as long as its multi-hop floods keep arriving — published with
            # a fresh-looking heard_secs_ago next to a months-old snr. Falls
            # back to last_seen when the value is absent OR null — null covers
            # zero_hop rows written by a downgraded binary after the migration
            # already ran, and matches the fallback GET_NEIGHBOURS and the CLI
            # use, so the three views agree on such rows.
            direct_seen = float(info.get("last_zero_hop_seen") or info.get("last_seen") or 0.0)
            if direct_seen <= 0 or (now - direct_seen) > max_age:
                continue
            # heard_secs_ago and the ordering derive from this too, keeping the
            # published row self-consistent with its zero-hop-only snr.
            merged[key] = {"last_seen": direct_seen, "snr": float(info.get("snr") or 0.0)}

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
            # Only logged, not published: the payload reports the capped table, so
            # the dropped rows would otherwise be invisible here.
            logger.info(
                "Neighbour table has %d entries; querying the freshest %d",
                len(snapshots),
                self.max_neighbors,
            )
            snapshots = snapshots[: self.max_neighbors]
        return snapshots

    def _build_payload(
        self,
        targets: List[NeighborSnapshot],
        scope_results: Dict[str, ScopeResult],
    ) -> dict:
        now = time.time()
        entries = []
        queried = 0
        for target in targets:
            result = scope_results.get(target.pubkey) or ScopeResult(STATUS_TIMEOUT)
            if result.transmitted:
                queried += 1
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
            self_default_scope=self._self_default_scope(),
            entries=entries,
            total_neighbors=len(entries),
            queried_neighbors=queried,
        )

    def _self_default_scope(self) -> str:
        """The region this node stamps on outgoing floods, or ``*`` when unset.

        Read from live config on every cycle because ``region default <name>`` over
        the mesh CLI writes straight into ``config["mesh"]`` (the same dict this
        holds) and expects to take effect without a restart.

        Normalised like the ``scopes`` field beside it: the transport-key table
        stores region names with a leading ``#``, which is not part of the name a
        consumer matches on, so it is stripped here as
        ``LoginHelper._format_region_names`` strips it there.
        """
        mesh_cfg = self.config.get("mesh", {}) if isinstance(self.config, dict) else {}
        if not isinstance(mesh_cfg, dict):
            return DEFAULT_SCOPE_WILDCARD
        raw = mesh_cfg.get("default_region")
        name = str(raw).strip() if raw not in (None, "") else ""
        if name.startswith("#"):
            name = name[1:].strip()
        return name or DEFAULT_SCOPE_WILDCARD

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


def _as_epoch(value) -> float:
    """Coerce a persisted timestamp to a float, or 0.0 when it is unusable."""
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
