"""
Advertisement packet handling helper for pyMC Repeater.

This module processes advertisement packets for neighbor tracking and discovery.
Includes adaptive rate limiting based on mesh activity.
"""

import asyncio
import logging
import time
import itertools
from collections import OrderedDict, deque
from enum import Enum
from typing import Dict, Optional, Tuple

from pymc_core.node.handlers.advert import AdvertHandler

logger = logging.getLogger("AdvertHelper")


class MeshActivityTier(Enum):
    """Mesh activity levels for adaptive rate limiting."""
    QUIET = "quiet"
    NORMAL = "normal"
    BUSY = "busy"
    CONGESTED = "congested"


# Tier multipliers for rate limit scaling
TIER_MULTIPLIERS = {
    MeshActivityTier.QUIET: 0.0,       # No rate limiting
    MeshActivityTier.NORMAL: 0.5,      # Light limiting
    MeshActivityTier.BUSY: 1.0,        # Standard limiting
    MeshActivityTier.CONGESTED: 2.0,   # Aggressive limiting
}


class AdvertHelper:
    """Helper class for processing advertisement packets in the repeater."""

    def __init__(self, local_identity, storage, config=None, log_fn=None):
        """
        Initialize the advert helper.

        Args:
            local_identity: The LocalIdentity instance for this repeater
            storage: StorageCollector instance for persisting advert data
            log_fn: Optional logging function for AdvertHandler
        """
        self.local_identity = local_identity
        self.storage = storage
        self.config = config or {}
        
        # Create AdvertHandler internally as a parsing utility
        self.advert_handler = AdvertHandler(log_fn=log_fn or logger.info)
        
        # Cache for tracking known neighbors (avoid repeated database queries)
        self._known_neighbors = set()

        repeater_cfg = self.config.get("repeater", {})

        # --- Adaptive mode config ---
        adaptive_cfg = repeater_cfg.get("advert_adaptive", {})
        self._adaptive_enabled = bool(adaptive_cfg.get("enabled", True))
        self._ewma_alpha = max(0.01, min(1.0, float(adaptive_cfg.get("ewma_alpha", 0.1))))
        self._tier_hysteresis_seconds = max(0.0, float(adaptive_cfg.get("hysteresis_seconds", 300.0)))
        
        # Tier thresholds (packets per minute)
        thresholds = adaptive_cfg.get("thresholds", {})
        self._threshold_normal = float(thresholds.get("normal", 1.0))
        self._threshold_busy = float(thresholds.get("busy", 5.0))
        self._threshold_congested = float(thresholds.get("congested", 15.0))

        # --- Base rate limit config (scaled by tier) ---
        rate_cfg = repeater_cfg.get("advert_rate_limit", {})
        self._rate_limit_enabled = bool(rate_cfg.get("enabled", True))
        self._base_bucket_capacity = max(1.0, float(rate_cfg.get("bucket_capacity", 2)))
        self._base_refill_tokens = max(0.1, float(rate_cfg.get("refill_tokens", 1.0)))
        self._base_refill_interval = max(1.0, float(rate_cfg.get("refill_interval_seconds", 36000.0)))
        self._base_min_interval = max(0.0, float(rate_cfg.get("min_interval_seconds", 3600.0)))

        # --- Penalty box config ---
        penalty_cfg = repeater_cfg.get("advert_penalty_box", {})
        self._penalty_enabled = bool(penalty_cfg.get("enabled", True))
        self._penalty_violation_threshold = max(1, int(penalty_cfg.get("violation_threshold", 2)))
        self._penalty_decay_seconds = max(1.0, float(penalty_cfg.get("violation_decay_seconds", 43200.0)))
        self._penalty_base_seconds = max(1.0, float(penalty_cfg.get("base_penalty_seconds", 21600.0)))
        self._penalty_multiplier = max(1.0, float(penalty_cfg.get("penalty_multiplier", 2.0)))
        self._penalty_max_seconds = max(
            self._penalty_base_seconds,
            float(penalty_cfg.get("max_penalty_seconds", 86400.0)),
        )

        # --- Advert dedupe config ---
        dedupe_cfg = repeater_cfg.get("advert_dedupe", {})
        self._advert_dedupe_ttl_seconds = max(1.0, float(dedupe_cfg.get("ttl_seconds", 120.0)))
        self._advert_dedupe_max_hashes = max(100, int(dedupe_cfg.get("max_hashes", 10000)))

        # --- Per-pubkey state ---
        self._bucket_state: Dict[str, dict] = {}
        self._penalty_until: Dict[str, float] = {}
        self._violation_state: Dict[str, dict] = {}
        self._recent_advert_hashes: OrderedDict[str, float] = OrderedDict()

        # --- Adaptive metrics state ---
        self._adverts_ewma = 0.0  # EWMA of adverts per minute
        self._packets_ewma = 0.0  # EWMA of total packets per minute
        self._duplicates_ewma = 0.0  # EWMA of duplicate ratio
        self._last_metrics_update = time.time()
        self._metrics_window_seconds = 60.0
        self._adverts_in_window = 0
        self._packets_in_window = 0
        self._duplicates_in_window = 0

        # Current activity tier with hysteresis
        self._current_tier = MeshActivityTier.NORMAL
        self._tier_since = time.time()
        self._pending_tier: Optional[MeshActivityTier] = None
        self._pending_tier_since = 0.0

        # Stats counters
        self._stats_adverts_allowed = 0
        self._stats_adverts_dropped = 0
        self._stats_advert_duplicates = 0
        self._stats_tier_changes = 0
        
        # Recent drops tracking — bounded deque so append is O(1) and the
        # oldest entry is evicted automatically (no pop(0) O(n) shift needed).
        self._recent_drops: deque = deque(maxlen=20)
        
        # Memory management
        self._last_cleanup = time.time()
        self._cleanup_interval_seconds = 3600.0  # Clean up every hour
        self._bucket_state_retention_seconds = 604800.0  # Keep inactive pubkeys for 7 days
        self._max_tracked_pubkeys = 10000  # Hard limit on tracked pubkeys

        logger.info(
            f"Advert limiter: adaptive={self._adaptive_enabled}, "
            f"rate_limit={self._rate_limit_enabled}, "
            f"bucket={self._base_bucket_capacity:.1f}, "
            f"penalty={self._penalty_enabled}, "
            f"dedupe=True"
        )

    # -------------------------------------------------------------------------
    # Memory management
    # -------------------------------------------------------------------------

    def _cleanup_old_state(self, now: float) -> None:
        """Clean up old/expired entries to prevent unbounded memory growth."""
        while self._recent_advert_hashes:
            oldest_hash, expires_at = next(iter(self._recent_advert_hashes.items()))
            if expires_at > now:
                break
            self._recent_advert_hashes.pop(oldest_hash, None)

        while len(self._recent_advert_hashes) > self._advert_dedupe_max_hashes:
            self._recent_advert_hashes.popitem(last=False)


        expired_penalties = [pk for pk, until in self._penalty_until.items() if until < now]
        for pk in expired_penalties:
            del self._penalty_until[pk]
        

        inactive_pubkeys = [
            pk for pk, state in self._bucket_state.items()
            if now - state.get("last_seen", 0) > self._bucket_state_retention_seconds
        ]
        for pk in inactive_pubkeys:
            del self._bucket_state[pk]
            if pk in self._violation_state:
                del self._violation_state[pk]
        
        # 3. Decay old violations based on decay time
        for pk, vstate in list(self._violation_state.items()):
            last_violation = vstate.get("last_violation", 0)
            if now - last_violation > self._penalty_decay_seconds:
                # Reset violation count after decay period
                vstate["count"] = 0
        
        if len(self._bucket_state) > self._max_tracked_pubkeys:
            # Sort by last_seen and remove oldest 10%
            sorted_pubkeys = sorted(
                self._bucket_state.items(),
                key=lambda x: x[1].get("last_seen", 0)
            )
            to_remove = int(len(sorted_pubkeys) * 0.1)
            for pk, _ in sorted_pubkeys[:to_remove]:
                del self._bucket_state[pk]
                if pk in self._violation_state:
                    del self._violation_state[pk]
                if pk in self._penalty_until:
                    del self._penalty_until[pk]
        
        # 5. Limit known neighbors set to prevent unbounded growth
        if len(self._known_neighbors) > 1000:
            # itertools.islice avoids materialising the full list first (O(n) → O(k))
            self._known_neighbors = set(itertools.islice(self._known_neighbors, 500))
        
        if expired_penalties or inactive_pubkeys:
            logger.debug(
                f"Cleaned up {len(expired_penalties)} expired penalties, "
                f"{len(inactive_pubkeys)} inactive pubkeys. "
                f"Tracking: {len(self._bucket_state)} buckets, "
                f"{len(self._penalty_until)} penalties, "
                f"{len(self._known_neighbors)} neighbors, "
                f"{len(self._recent_advert_hashes)} advert hashes"
            )

    def _dedupe_advert_packet_hash(self, packet, now: float) -> bool:
        """Return True when advert packet hash was already seen recently."""
        try:
            pkt_hash = packet.calculate_packet_hash().hex().upper()
        except Exception:
            return False

        expires_at = self._recent_advert_hashes.get(pkt_hash)
        if expires_at and expires_at > now:
            # Move to end so hot hashes remain least likely to be evicted
            self._recent_advert_hashes.move_to_end(pkt_hash)
            return True

        # Track first-seen (or expired hash re-seen)
        self._recent_advert_hashes[pkt_hash] = now + self._advert_dedupe_ttl_seconds
        self._recent_advert_hashes.move_to_end(pkt_hash)

        # Opportunistic cleanup to keep memory bounded between scheduled cleanup runs
        while len(self._recent_advert_hashes) > self._advert_dedupe_max_hashes:
            self._recent_advert_hashes.popitem(last=False)

        return False

    # -------------------------------------------------------------------------
    # Adaptive tier calculation
    # -------------------------------------------------------------------------

    def _update_metrics_window(self, now: float, is_advert: bool = True, is_duplicate: bool = False) -> None:
        """Update rolling metrics window and EWMA."""
        elapsed = now - self._last_metrics_update

        if elapsed >= self._metrics_window_seconds:
            # Calculate rates for window
            adverts_per_min = (self._adverts_in_window / elapsed) * 60.0
            packets_per_min = (self._packets_in_window / elapsed) * 60.0
            dup_ratio = (
                self._duplicates_in_window / max(1, self._packets_in_window)
            )

            # Update EWMA
            alpha = self._ewma_alpha
            self._adverts_ewma = alpha * adverts_per_min + (1 - alpha) * self._adverts_ewma
            self._packets_ewma = alpha * packets_per_min + (1 - alpha) * self._packets_ewma
            self._duplicates_ewma = alpha * dup_ratio + (1 - alpha) * self._duplicates_ewma

            # Reset window
            self._adverts_in_window = 0
            self._packets_in_window = 0
            self._duplicates_in_window = 0
            self._last_metrics_update = now
            
            # Periodic cleanup
            if now - self._last_cleanup >= self._cleanup_interval_seconds:
                self._cleanup_old_state(now)
                self._last_cleanup = now

        # Count this event
        if is_advert:
            self._adverts_in_window += 1
        self._packets_in_window += 1
        if is_duplicate:
            self._duplicates_in_window += 1

    def _calculate_target_tier(self) -> MeshActivityTier:
        """Determine target tier based on current EWMA metrics."""
        # Combined activity score (adverts + packets weighted)
        activity = self._adverts_ewma + (self._packets_ewma * 0.1)

        if activity >= self._threshold_congested:
            return MeshActivityTier.CONGESTED
        elif activity >= self._threshold_busy:
            return MeshActivityTier.BUSY
        elif activity >= self._threshold_normal:
            return MeshActivityTier.NORMAL
        else:
            return MeshActivityTier.QUIET

    def _update_tier(self, now: float) -> None:
        """Update current tier with hysteresis to prevent flapping."""
        if not self._adaptive_enabled:
            return

        target = self._calculate_target_tier()

        if target == self._current_tier:
            # Stable, clear pending
            self._pending_tier = None
            return

        if self._pending_tier != target:
            # New pending tier
            self._pending_tier = target
            self._pending_tier_since = now
            return

        # Check hysteresis
        if (now - self._pending_tier_since) >= self._tier_hysteresis_seconds:
            old_tier = self._current_tier
            self._current_tier = target
            self._tier_since = now
            self._pending_tier = None
            self._stats_tier_changes += 1
            logger.info(f"Mesh activity tier changed: {old_tier.value} → {target.value}")

    def get_current_tier(self) -> MeshActivityTier:
        """Get current mesh activity tier."""
        return self._current_tier

    def _get_effective_limits(self) -> Tuple[float, float, float, float]:
        """Get effective rate limits scaled by current tier."""
        if not self._adaptive_enabled:
            return (
                self._base_bucket_capacity,
                self._base_refill_tokens,
                self._base_refill_interval,
                self._base_min_interval,
            )

        multiplier = TIER_MULTIPLIERS.get(self._current_tier, 1.0)

        if multiplier == 0.0:
            # QUIET mode: effectively disable rate limiting
            return (100.0, 100.0, 1.0, 0.0)

        # Scale intervals UP (stricter) as multiplier increases
        return (
            self._base_bucket_capacity,
            self._base_refill_tokens,
            self._base_refill_interval * multiplier,
            self._base_min_interval * multiplier,
        )

    def _refill_tokens_if_needed(self, pubkey: str, now: float) -> dict:
        """Refill token bucket using effective (tier-scaled) limits."""
        bucket_cap, refill_tokens, refill_interval, _ = self._get_effective_limits()
        
        state = self._bucket_state.get(pubkey)
        if state is None:
            state = {
                "tokens": bucket_cap,
                "last_refill": now,
                "last_seen": 0.0,
            }
            self._bucket_state[pubkey] = state
            return state

        elapsed = now - state["last_refill"]
        if elapsed <= 0:
            return state

        refill_steps = elapsed / refill_interval
        if refill_steps > 0:
            state["tokens"] = min(
                bucket_cap,
                state["tokens"] + (refill_steps * refill_tokens),
            )
            state["last_refill"] = now
        return state

    def _record_violation_and_maybe_penalize(self, pubkey: str, now: float) -> None:
        if not self._penalty_enabled:
            return

        state = self._violation_state.get(pubkey)
        if state is None:
            state = {"count": 0, "last_violation": 0.0}
            self._violation_state[pubkey] = state

        if (now - state["last_violation"]) > self._penalty_decay_seconds:
            state["count"] = 0

        state["count"] += 1
        state["last_violation"] = now

        if state["count"] < self._penalty_violation_threshold:
            return

        level = state["count"] - self._penalty_violation_threshold
        penalty_seconds = min(
            self._penalty_max_seconds,
            self._penalty_base_seconds * (self._penalty_multiplier**level),
        )
        new_until = now + penalty_seconds
        old_until = self._penalty_until.get(pubkey, 0.0)

        if new_until > old_until:
            self._penalty_until[pubkey] = new_until
            logger.warning(
                f"Advert penalty activated for {pubkey[:16]}... "
                f"({penalty_seconds:.1f}s, violations={state['count']})"
            )

    def _allow_advert(self, pubkey: str, now: float) -> Tuple[bool, str]:
        """Check if advert is allowed using adaptive tier-scaled limits."""
        # Update metrics and tier
        self._update_metrics_window(now, is_advert=True)
        self._update_tier(now)
        
        if not self._rate_limit_enabled:
            self._stats_adverts_allowed += 1
            return True, ""

        # QUIET tier bypasses rate limiting
        if self._adaptive_enabled and self._current_tier == MeshActivityTier.QUIET:
            self._stats_adverts_allowed += 1
            return True, ""

        penalty_until = self._penalty_until.get(pubkey, 0.0)
        if now < penalty_until:
            remaining = penalty_until - now
            self._stats_adverts_dropped += 1
            return False, f"advert penalty box active ({remaining:.1f}s remaining)"

        state = self._refill_tokens_if_needed(pubkey, now)
        _, _, _, min_interval = self._get_effective_limits()

        last_seen = float(state.get("last_seen", 0.0))
        if min_interval > 0 and last_seen > 0:
            since_last = now - last_seen
            if since_last < min_interval:
                self._record_violation_and_maybe_penalize(pubkey, now)
                self._stats_adverts_dropped += 1
                return (
                    False,
                    f"advert min-interval hit ({since_last:.2f}s < {min_interval:.2f}s)",
                )

        if state["tokens"] < 1.0:
            self._record_violation_and_maybe_penalize(pubkey, now)
            self._stats_adverts_dropped += 1
            return False, "advert rate limit exceeded"

        state["tokens"] -= 1.0
        state["last_seen"] = now
        self._stats_adverts_allowed += 1
        return True, ""

    def record_packet_seen(self, is_duplicate: bool = False) -> None:
        """Record a packet seen for metrics (called by router for non-advert packets)."""
        now = time.time()
        self._update_metrics_window(now, is_advert=False, is_duplicate=is_duplicate)

    def get_rate_limit_stats(self) -> dict:
        """Get comprehensive rate limiting and adaptive tier statistics."""
        now = time.time()
        bucket_cap, refill_tokens, refill_interval, min_interval = self._get_effective_limits()
        
        # Active penalties
        active_penalties = {
            pk[:16]: round(until - now, 1)
            for pk, until in self._penalty_until.items()
            if until > now
        }
        
        # Per-pubkey bucket states
        bucket_summary = {}
        for pk, state in self._bucket_state.items():
            bucket_summary[pk[:16]] = {
                "tokens": round(state["tokens"], 2),
                "last_seen_ago": round(now - state["last_seen"], 1) if state["last_seen"] > 0 else None,
            }
        
        return {
            "adaptive": {
                "enabled": self._adaptive_enabled,
                "current_tier": self._current_tier.value,
                "tier_since": round(now - self._tier_since, 1),
                "pending_tier": self._pending_tier.value if self._pending_tier else None,
                "tier_changes": self._stats_tier_changes,
            },
            "metrics": {
                "adverts_per_min_ewma": round(self._adverts_ewma, 2),
                "packets_per_min_ewma": round(self._packets_ewma, 2),
                "duplicate_ratio_ewma": round(self._duplicates_ewma, 3),
            },
            "effective_limits": {
                "bucket_capacity": bucket_cap,
                "refill_tokens": refill_tokens,
                "refill_interval_seconds": round(refill_interval, 1),
                "min_interval_seconds": round(min_interval, 1),
            },
            "stats": {
                "adverts_allowed": self._stats_adverts_allowed,
                "adverts_dropped": self._stats_adverts_dropped,
                "adverts_duplicate_reheard": self._stats_advert_duplicates,
                "drop_rate": round(
                    self._stats_adverts_dropped / max(1, self._stats_adverts_allowed + self._stats_adverts_dropped),
                    3,
                ),
            },
            "dedupe": {
                "enabled": True,
                "ttl_seconds": self._advert_dedupe_ttl_seconds,
                "tracked_hashes": len(self._recent_advert_hashes),
                "max_hashes": self._advert_dedupe_max_hashes,
            },
            "active_penalties": active_penalties,
            "tracked_pubkeys": len(self._bucket_state),
            "bucket_states": bucket_summary,
            "recent_drops": [
                {
                    "pubkey": drop["pubkey"],
                    "name": drop["name"],
                    "reason": drop["reason"],
                    "seconds_ago": round(now - drop["timestamp"], 1)
                }
                for drop in reversed(self._recent_drops)  # Most recent first
            ],
        }

    async def process_advert_packet(self, packet, rssi: int, snr: float) -> None:
        """
        Process an incoming advertisement packet.

        This method uses AdvertHandler to parse the packet, then stores
        the neighbor information for tracking and discovery.

        Args:
            packet: The advertisement packet to process
            rssi: Received signal strength indicator
            snr: Signal-to-noise ratio
        """
        try:
            # Set signal metrics on packet for handler to use
            packet._snr = snr
            packet._rssi = rssi
            
            # Use AdvertHandler to parse the packet - it now returns parsed data
            advert_data = await self.advert_handler(packet)
            
            if not advert_data or not advert_data.get("valid"):
                logger.warning("Invalid advert packet received, dropping.")
                packet.mark_do_not_retransmit()
                packet.drop_reason = "Invalid advert packet"
                return
            
            # Extract data from parsed advert
            pubkey = advert_data["public_key"]
            node_name = advert_data["name"]
            contact_type = advert_data["contact_type"]

            now = time.time()

            # Re-heard duplicates should be measured but not consume limiter tokens.
            if self._dedupe_advert_packet_hash(packet, now):
                self._stats_advert_duplicates += 1
                self._update_metrics_window(now, is_advert=False, is_duplicate=True)
                logger.debug(
                    "Duplicate advert re-heard from '%s' (%s...), skipping limiter/storage",
                    node_name,
                    pubkey[:16],
                )
                return

            # Per-pubkey rate limiting (token bucket + penalty box)
            allowed, reason = self._allow_advert(pubkey, now)
            if not allowed:
                logger.warning(f"Dropping advert from '{node_name}' ({pubkey[:16]}...): {reason}")
                packet.mark_do_not_retransmit()
                packet.drop_reason = reason
                
                # Track recent drop (deduplicate by pubkey)
                pubkey_short = pubkey[:16]
                
                # Remove any existing entry for this pubkey, then append the
                # updated record.  Rebuilding as a deque preserves maxlen so
                # the oldest entry is evicted automatically — no pop(0) needed.
                self._recent_drops = deque(
                    (d for d in self._recent_drops if d["pubkey"] != pubkey_short),
                    maxlen=20,
                )
                self._recent_drops.append({
                    "pubkey": pubkey_short,
                    "name": node_name,
                    "reason": reason,
                    "timestamp": now
                })
                
                return
            
            # Skip our own adverts
            if self.local_identity:
                local_pubkey = self.local_identity.get_public_key().hex()
                if pubkey == local_pubkey:
                    logger.debug("Ignoring own advert in neighbor tracking")
                    return

            # Get route type from packet header
            from pymc_core.protocol.constants import PH_ROUTE_MASK

            route_type = packet.header & PH_ROUTE_MASK

            # Check if this is a new neighbor (run DB read in thread to avoid blocking event loop)
            current_time = now
            if pubkey not in self._known_neighbors:
                # Only check database if not in cache
                if self.storage:
                    current_neighbors = await asyncio.to_thread(
                        self.storage.get_neighbors
                    )
                else:
                    current_neighbors = {}
                is_new_neighbor = pubkey not in current_neighbors
                
                if is_new_neighbor:
                    self._known_neighbors.add(pubkey)
                    logger.info(f"Discovered new neighbor: {node_name} ({pubkey[:16]}...)")
            else:
                is_new_neighbor = False
            
            # Determine zero-hop: direct routes are always zero-hop,
            # flood routes are zero-hop if path_len <= 1 (received directly)
            path_len = len(packet.path) if packet.path else 0
            zero_hop = path_len == 0
            
            # Build advert record
            advert_record = {
                "timestamp": current_time,
                "pubkey": pubkey,
                "node_name": node_name,
                "is_repeater": "REPEATER" in contact_type.upper(),
                "route_type": route_type,
                "contact_type": contact_type,
                "latitude": advert_data["latitude"],
                "longitude": advert_data["longitude"],
                "rssi": rssi,
                "snr": snr,
                "is_new_neighbor": is_new_neighbor,
                "zero_hop": zero_hop,
            }
            
            # Store to database (run in thread so event loop stays responsive;
            # blocking here can cause companion TCP clients to disconnect)
            if self.storage:
                try:
                    await asyncio.to_thread(
                        self.storage.record_advert,
                        advert_record,
                    )
                except Exception as e:
                    logger.error(f"Failed to store advert record: {e}")
        
        except Exception as e:
            logger.error(f"Error processing advert packet: {e}", exc_info=True)

    def reload_config(self) -> None:
        """Reload rate limiting configuration from self.config (called after live config updates)."""
        try:
            repeater_cfg = self.config.get("repeater", {})

            # Adaptive mode config
            adaptive_cfg = repeater_cfg.get("advert_adaptive", {})
            self._adaptive_enabled = bool(adaptive_cfg.get("enabled", True))
            self._ewma_alpha = max(0.01, min(1.0, float(adaptive_cfg.get("ewma_alpha", 0.1))))
            self._tier_hysteresis_seconds = max(0.0, float(adaptive_cfg.get("hysteresis_seconds", 300.0)))
            
            thresholds = adaptive_cfg.get("thresholds", {})
            self._threshold_normal = float(thresholds.get("normal", 1.0))
            self._threshold_busy = float(thresholds.get("busy", 5.0))
            self._threshold_congested = float(thresholds.get("congested", 15.0))

            # Base rate limit config
            rate_cfg = repeater_cfg.get("advert_rate_limit", {})
            self._rate_limit_enabled = bool(rate_cfg.get("enabled", True))
            self._base_bucket_capacity = max(1.0, float(rate_cfg.get("bucket_capacity", 2)))
            self._base_refill_tokens = max(0.1, float(rate_cfg.get("refill_tokens", 1.0)))
            self._base_refill_interval = max(1.0, float(rate_cfg.get("refill_interval_seconds", 36000.0)))
            self._base_min_interval = max(0.0, float(rate_cfg.get("min_interval_seconds", 3600.0)))

            # Penalty box config
            penalty_cfg = repeater_cfg.get("advert_penalty_box", {})
            self._penalty_enabled = bool(penalty_cfg.get("enabled", True))
            self._penalty_violation_threshold = max(1, int(penalty_cfg.get("violation_threshold", 2)))
            self._penalty_decay_seconds = max(1.0, float(penalty_cfg.get("violation_decay_seconds", 43200.0)))
            self._penalty_base_seconds = max(1.0, float(penalty_cfg.get("base_penalty_seconds", 21600.0)))
            self._penalty_multiplier = max(1.0, float(penalty_cfg.get("penalty_multiplier", 2.0)))
            self._penalty_max_seconds = max(
                self._penalty_base_seconds,
                float(penalty_cfg.get("max_penalty_seconds", 86400.0)),
            )

            # Advert dedupe config
            dedupe_cfg = repeater_cfg.get("advert_dedupe", {})
            self._advert_dedupe_ttl_seconds = max(1.0, float(dedupe_cfg.get("ttl_seconds", 120.0)))
            self._advert_dedupe_max_hashes = max(100, int(dedupe_cfg.get("max_hashes", 10000)))

            logger.info(
                f"Advert limiter config reloaded: adaptive={self._adaptive_enabled}, "
                f"rate_limit={self._rate_limit_enabled}, bucket={self._base_bucket_capacity:.1f}, "
                f"dedupe=True"
            )
        except Exception as e:
            logger.error(f"Error reloading advert limiter config: {e}")
