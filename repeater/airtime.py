import logging
import time
from typing import Tuple

logger = logging.getLogger("AirtimeManager")


class AirtimeManager:
    def __init__(self, config: dict):
        self.config = config
        self.max_airtime_per_minute = config.get("duty_cycle", {}).get(
            "max_airtime_per_minute", 3600
        )

        # Track airtime in rolling window
        self.tx_history = []  # [(timestamp, airtime_ms), ...]
        self.window_size = 60  # seconds
        self.total_airtime_ms = 0

    def calculate_airtime(
        self,
        payload_len: int,
        spreading_factor: int = 7,
        bandwidth_hz: int = 125000,
    ) -> float:

        bw_khz = bandwidth_hz / 1000
        symbol_time = (2**spreading_factor) / bw_khz
        preamble_time = 8 * symbol_time
        payload_symbols = (payload_len + 4.25) * 8
        payload_time = payload_symbols * symbol_time

        total_ms = preamble_time + payload_time
        return total_ms

    def can_transmit(self, airtime_ms: float) -> Tuple[bool, float]:
        enforcement_enabled = self.config.get("duty_cycle", {}).get("enforcement_enabled", True)
        if not enforcement_enabled:
            # Duty cycle enforcement disabled - always allow
            return True, 0.0

        now = time.time()

        # Remove old entries outside window
        self.tx_history = [(ts, at) for ts, at in self.tx_history if now - ts < self.window_size]

        # Calculate current airtime in window
        current_airtime = sum(at for _, at in self.tx_history)

        if current_airtime + airtime_ms <= self.max_airtime_per_minute:
            return True, 0.0

        # Calculate wait time until oldest entry expires
        if self.tx_history:
            oldest_ts, oldest_at = self.tx_history[0]
            wait_time = (oldest_ts + self.window_size) - now
            return False, max(0, wait_time)

        return False, 1.0

    def record_tx(self, airtime_ms: float):
        self.tx_history.append((time.time(), airtime_ms))
        self.total_airtime_ms += airtime_ms
        logger.debug(f"TX recorded: {airtime_ms: .1f}ms (total: {self.total_airtime_ms: .0f}ms)")

    def get_stats(self) -> dict:
        now = time.time()
        self.tx_history = [(ts, at) for ts, at in self.tx_history if now - ts < self.window_size]

        current_airtime = sum(at for _, at in self.tx_history)
        utilization = (current_airtime / self.max_airtime_per_minute) * 100

        return {
            "current_airtime_ms": current_airtime,
            "max_airtime_ms": self.max_airtime_per_minute,
            "utilization_percent": utilization,
            "total_airtime_ms": self.total_airtime_ms,
        }
