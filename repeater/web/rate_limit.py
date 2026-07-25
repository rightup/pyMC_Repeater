"""Bounded in-memory token buckets for inexpensive per-principal admission."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Dict, Hashable, Iterable, Optional, Set, Tuple

MAX_SSE_QUEUE_SIZE = 4096
MAX_SSE_KEEPALIVE_SEC = 60


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    last_seen: float


class PrincipalTokenBucket:
    """A small, thread-safe token bucket keyed by an authenticated principal."""

    def __init__(
        self,
        *,
        capacity: int,
        refill_per_second: float,
        idle_ttl_seconds: float = 3600.0,
        max_principals: int = 4096,
    ) -> None:
        if isinstance(capacity, bool):
            raise ValueError("capacity must be a positive integer")
        try:
            parsed_capacity = int(capacity)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("capacity must be a positive integer") from None
        if parsed_capacity < 1:
            raise ValueError("capacity must be a positive integer")

        parsed_refill = self._positive_finite(
            refill_per_second,
            "refill_per_second",
        )
        parsed_idle_ttl = self._positive_finite(
            idle_ttl_seconds,
            "idle_ttl_seconds",
        )
        if isinstance(max_principals, bool):
            raise ValueError("max_principals must be a positive integer")
        try:
            parsed_max_principals = int(max_principals)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("max_principals must be a positive integer") from None
        if parsed_max_principals < 1:
            raise ValueError("max_principals must be a positive integer")

        self.capacity = parsed_capacity
        self.refill_per_second = parsed_refill
        self.idle_ttl_seconds = max(60.0, parsed_idle_ttl)
        self.max_principals = max(32, parsed_max_principals)
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _positive_finite(value, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a finite positive number")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be a finite positive number") from None
        if not math.isfinite(parsed) or parsed <= 0.0:
            raise ValueError(f"{name} must be a finite positive number")
        return parsed

    def consume(self, principal: str, *, cost: float = 1.0) -> Optional[float]:
        """Consume tokens, returning retry seconds when admission is denied."""

        now = time.monotonic()
        cost = self._positive_finite(cost, "cost")
        with self._lock:
            bucket = self._prepare_locked(principal, now)
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return None
            return (cost - bucket.tokens) / self.refill_per_second

    def _prepare_locked(self, principal: str, now: float) -> _Bucket:
        """Return a current bucket. Caller must hold ``self._lock``."""

        self._prune_locked(now)
        bucket = self._buckets.get(principal)
        if bucket is None:
            if len(self._buckets) >= self.max_principals:
                oldest = min(self._buckets, key=lambda key: self._buckets[key].last_seen)
                self._buckets.pop(oldest, None)
            bucket = _Bucket(float(self.capacity), now, now)
            self._buckets[principal] = bucket

        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(
            float(self.capacity),
            bucket.tokens + elapsed * self.refill_per_second,
        )
        bucket.updated_at = now
        bucket.last_seen = now
        return bucket

    def _prune_locked(self, now: float) -> None:
        expired = [
            principal
            for principal, bucket in self._buckets.items()
            if now - bucket.last_seen >= self.idle_ttl_seconds
        ]
        for principal in expired:
            self._buckets.pop(principal, None)


class SSEAdmission:
    """One shared, thread-safe connection budget for companion SSE routes."""

    def __init__(self, max_connections: int) -> None:
        if isinstance(max_connections, bool) or not isinstance(max_connections, int):
            raise ValueError("sse_max_connections must be a positive integer")
        if max_connections < 1:
            raise ValueError("sse_max_connections must be a positive integer")
        self.max_connections = max_connections
        self._active: Set[Tuple[str, Hashable]] = set()
        self._lock = threading.Lock()

    def acquire(self, principal: str, companion: Hashable) -> bool:
        """Reserve one principal/companion stream, or return ``False``."""

        key = (principal, companion)
        with self._lock:
            if key in self._active or len(self._active) >= self.max_connections:
                return False
            self._active.add(key)
            return True

    def release(self, principal: str, companion: Hashable) -> None:
        """Release a reservation. Repeated cleanup is intentionally harmless."""

        with self._lock:
            self._active.discard((principal, companion))

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)


def sse_stream_settings(config: dict) -> Tuple[int, int]:
    """Return the validated shared SSE queue and keepalive settings."""

    http_config = config.get("http", {}) if isinstance(config, dict) else {}
    if not isinstance(http_config, dict):
        raise ValueError("http must be an object")

    def setting(name: str, default: int, minimum: int, maximum: int) -> int:
        value = http_config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"http.{name} must be a positive integer")
        if value > maximum:
            raise ValueError(
                f"http.{name} must be no greater than {maximum}"
            )
        return max(minimum, value)

    return (
        setting("sse_queue_maxsize", 64, 32, MAX_SSE_QUEUE_SIZE),
        setting("sse_keepalive_sec", 15, 5, MAX_SSE_KEEPALIVE_SEC),
    )


def validate_sse_connection_capacity(config: dict, sse_max_connections: int) -> None:
    """Reserve two HTTP workers for non-streaming API requests."""

    http_config = config.get("http", {}) if isinstance(config, dict) else {}
    if not isinstance(http_config, dict):
        raise ValueError("http must be an object")
    try:
        thread_pool = max(2, int(http_config.get("thread_pool", 8)))
        thread_pool_max = max(
            thread_pool,
            int(http_config.get("thread_pool_max", 16)),
        )
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            "http.thread_pool and http.thread_pool_max must be integers"
        ) from None
    maximum_streams = thread_pool_max - 2
    if sse_max_connections > maximum_streams:
        raise ValueError(
            "mobile_api.sse_max_connections must be at most "
            f"{maximum_streams} when the effective http.thread_pool_max is "
            f"{thread_pool_max}; two HTTP workers are reserved for other APIs"
        )


Admission = Tuple[PrincipalTokenBucket, str, float]


def consume_all(admissions: Iterable[Admission]) -> Optional[float]:
    """Atomically consume several token-bucket budgets.

    Returns the longest retry delay when any budget denies admission.  In
    that case no budget is consumed, which is important when one radio
    action is governed by both a per-principal and a process-wide limit.
    """

    combined: Dict[tuple[PrincipalTokenBucket, str], float] = {}
    for limiter, principal, raw_cost in admissions:
        cost = limiter._positive_finite(raw_cost, "cost")
        key = (limiter, principal)
        combined[key] = combined.get(key, 0.0) + cost
    if not combined:
        return None

    limiters = sorted({limiter for limiter, _principal in combined}, key=id)
    for limiter in limiters:
        limiter._lock.acquire()
    try:
        now = time.monotonic()
        prepared = []
        retry_after = 0.0
        for (limiter, principal), cost in combined.items():
            bucket = limiter._prepare_locked(principal, now)
            prepared.append((bucket, cost))
            if bucket.tokens < cost:
                retry_after = max(
                    retry_after,
                    (cost - bucket.tokens) / limiter.refill_per_second,
                )
        if retry_after > 0.0:
            return retry_after
        for bucket, cost in prepared:
            bucket.tokens -= cost
        return None
    finally:
        for limiter in reversed(limiters):
            limiter._lock.release()
