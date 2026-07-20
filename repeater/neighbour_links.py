import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from openhop_core.protocol import Packet
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_TRACE,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)


@dataclass
class NeighbourLink:
    peer_hash: str
    path_hash_size: int

    first_seen: float
    last_seen: float
    last_seen_monotonic: float

    sample_count: int = 0
    duplicate_sample_count: int = 0

    last_rssi: float = 0.0
    last_snr: float = 0.0
    last_score: float = 0.0

    ewma_rssi: float = 0.0
    ewma_snr: float = 0.0
    ewma_score: float = 0.0

    best_score: float = 0.0
    worst_score: float = 1.0

    def update(
        self,
        *,
        now: float,
        now_monotonic: float,
        rssi: float,
        snr: float,
        score: float,
        is_duplicate: bool,
        alpha: float,
    ) -> None:
        first_sample = self.sample_count == 0
        self.sample_count += 1
        if is_duplicate:
            self.duplicate_sample_count += 1

        self.last_seen = now
        self.last_seen_monotonic = now_monotonic
        self.last_rssi = rssi
        self.last_snr = snr
        self.last_score = score

        if first_sample:
            self.first_seen = now
            self.ewma_rssi = rssi
            self.ewma_snr = snr
            self.ewma_score = score
            self.best_score = score
            self.worst_score = score
            return

        self.ewma_rssi = alpha * rssi + (1.0 - alpha) * self.ewma_rssi
        self.ewma_snr = alpha * snr + (1.0 - alpha) * self.ewma_snr
        self.ewma_score = alpha * score + (1.0 - alpha) * self.ewma_score

        if score > self.best_score:
            self.best_score = score
        if score < self.worst_score:
            self.worst_score = score


class NeighbourLinkTracker:
    def __init__(self, config: dict):
        self._neighbour_links: dict[str, NeighbourLink] = {}
        self._neighbour_links_lock = threading.RLock()

        self._metrics_enabled = True
        self._ewma_alpha = 0.20
        self._ttl_seconds = 86400.0
        self._max_entries = 512
        self.refresh_config(config)

    @property
    def links(self) -> dict[str, NeighbourLink]:
        return self._neighbour_links

    @property
    def lock(self) -> threading.RLock:
        return self._neighbour_links_lock

    @property
    def metrics_enabled(self) -> bool:
        return self._metrics_enabled

    @property
    def ewma_alpha(self) -> float:
        return self._ewma_alpha

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def refresh_config(self, config: dict) -> None:
        repeater_config = config.get("repeater", {})

        self._metrics_enabled = bool(repeater_config.get("neighbour_link_metrics_enabled", True))

        try:
            alpha = float(repeater_config.get("neighbour_link_ewma_alpha", 0.20))
        except (TypeError, ValueError):
            alpha = 0.20
        self._ewma_alpha = max(0.0, min(1.0, alpha))

        try:
            ttl = float(repeater_config.get("neighbour_link_ttl_seconds", 86400))
        except (TypeError, ValueError):
            ttl = 86400.0
        self._ttl_seconds = max(1.0, ttl)

        try:
            max_entries = int(repeater_config.get("neighbour_link_max_entries", 512))
        except (TypeError, ValueError):
            max_entries = 512
        self._max_entries = max(1, max_entries)

    def purge_expired_locked(self, now_monotonic: float) -> None:
        expired_keys = [
            key
            for key, link in self._neighbour_links.items()
            if (now_monotonic - link.last_seen_monotonic) > self._ttl_seconds
        ]
        for key in expired_keys:
            del self._neighbour_links[key]

    def evict_stalest_locked(self) -> None:
        if not self._neighbour_links:
            return
        stalest_key = min(
            self._neighbour_links,
            key=lambda key: self._neighbour_links[key].last_seen_monotonic,
        )
        del self._neighbour_links[stalest_key]

    @staticmethod
    def get_upstream_peer_identity(
        packet: Packet,
        route_type: int,
        payload_type: Optional[int],
        *,
        path_hashes=None,
        path_hash_size: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[int]]:
        if route_type not in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD):
            return None, None
        if payload_type == PAYLOAD_TYPE_TRACE:
            return None, None

        hashes = path_hashes if path_hashes is not None else packet.get_path_hashes_hex()
        if not hashes:
            return None, None

        size = path_hash_size
        if size is None:
            size = packet.get_path_hash_size() if hasattr(packet, "get_path_hash_size") else None
        if not size or int(size) <= 0:
            return None, None

        peer_hash = str(hashes[-1]).upper()
        if not peer_hash:
            return None, None

        return peer_hash, int(size)

    def observe(
        self,
        packet: Packet,
        *,
        route_type: int,
        payload_type: Optional[int],
        rssi: float,
        snr: float,
        score: float,
        is_duplicate: bool,
    ) -> None:
        if not self._metrics_enabled:
            return

        peer_hash, path_hash_size = self.get_upstream_peer_identity(
            packet,
            route_type,
            payload_type,
        )
        if not peer_hash or not path_hash_size:
            return

        now = time.time()
        now_monotonic = time.monotonic()
        key = f"{path_hash_size}:{peer_hash}"

        with self._neighbour_links_lock:
            self.purge_expired_locked(now_monotonic)
            link = self._neighbour_links.get(key)

            if link is None and len(self._neighbour_links) >= self._max_entries:
                self.evict_stalest_locked()

            if link is None:
                link = NeighbourLink(
                    peer_hash=peer_hash,
                    path_hash_size=path_hash_size,
                    first_seen=now,
                    last_seen=now,
                    last_seen_monotonic=now_monotonic,
                )
                self._neighbour_links[key] = link

            link.update(
                now=now,
                now_monotonic=now_monotonic,
                rssi=float(rssi),
                snr=float(snr),
                score=float(score),
                is_duplicate=is_duplicate,
                alpha=self._ewma_alpha,
            )

    def snapshot(
        self,
        *,
        active_within_seconds: float = 900.0,
    ) -> list[dict]:
        try:
            active_window = max(0.0, float(active_within_seconds))
        except (TypeError, ValueError):
            active_window = 900.0

        now_monotonic = time.monotonic()
        snapshot = []
        with self._neighbour_links_lock:
            self.purge_expired_locked(now_monotonic)
            for link in self._neighbour_links.values():
                age_seconds = max(0.0, now_monotonic - link.last_seen_monotonic)
                snapshot.append(
                    {
                        "peer_hash": link.peer_hash,
                        "path_hash_size": link.path_hash_size,
                        "sample_count": link.sample_count,
                        "duplicate_sample_count": link.duplicate_sample_count,
                        "first_seen": link.first_seen,
                        "last_seen": link.last_seen,
                        "age_seconds": age_seconds,
                        "active": age_seconds <= active_window,
                        "last_rssi": link.last_rssi,
                        "last_snr": link.last_snr,
                        "last_score": link.last_score,
                        "ewma_rssi": link.ewma_rssi,
                        "ewma_snr": link.ewma_snr,
                        "ewma_score": link.ewma_score,
                        "best_score": link.best_score,
                        "worst_score": link.worst_score,
                    }
                )

        return sorted(snapshot, key=lambda item: item["last_seen"], reverse=True)
