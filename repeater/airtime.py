import logging
import math
import time
from typing import Tuple

logger = logging.getLogger("AirtimeManager")


class AirtimeManager:
    def __init__(self, config: dict):
        self.config = config
        self.radio_config = config.get("radio", {})
        self.max_airtime_per_minute = config.get("duty_cycle", {}).get(
            "max_airtime_per_minute", 3600
        )

        # Store radio settings for airtime calculations
        self.spreading_factor = self.radio_config.get("spreading_factor", 7)
        self.bandwidth = self.radio_config.get("bandwidth", 125000)
        self.coding_rate = self.radio_config.get("coding_rate", 5)
        self.preamble_length = self.radio_config.get("preamble_length", 8)

        # Track airtime in rolling window
        self.tx_history = []  # [(timestamp, airtime_ms), ...]
        self.window_size = 60  # seconds
        self.total_airtime_ms = 0
        self.total_rx_airtime_ms = 0

    def calculate_airtime(
        self,
        payload_len: int,
        spreading_factor: int = None,
        bandwidth_hz: int = None,
        coding_rate: int = None,
        preamble_len: int = None,
        crc_enabled: bool = True,
        explicit_header: bool = True,
    ) -> float:
        """
        Calculate LoRa packet airtime using the Semtech reference formula.

        Reference: https://www.semtech.com/design-support/lora-calculator

        Args:
            payload_len: Payload length in bytes
            spreading_factor: SF7-SF12 (uses config value if None)
            bandwidth_hz: Bandwidth in Hz (uses config value if None)
            coding_rate: CR denominator, 5=4/5, 6=4/6, 7=4/7, 8=4/8 (uses config value if None)
            preamble_len: Preamble symbols (uses config value if None)
            crc_enabled: Whether CRC is enabled (default: True)
            explicit_header: Whether explicit header mode is used (default: True)

        Returns:
            Airtime in milliseconds
        """
        sf = spreading_factor or self.spreading_factor
        bw_hz = (bandwidth_hz or self.bandwidth)
        cr = coding_rate or self.coding_rate
        preamble_len = preamble_len or self.preamble_length
        crc = 1 if crc_enabled else 0
        h = 0 if explicit_header else 1  # H=0 for explicit, H=1 for implicit

        # Low data rate optimization: required for SF11/SF12 at 125kHz
        de = 1 if (sf >= 11 and bw_hz <= 125000) else 0

        # Symbol time in milliseconds: T_sym = 2^SF / BW_kHz
        t_sym = (2 ** sf) / (bw_hz / 1000)

        # Preamble time: T_preamble = (n_preamble + 4.25) * T_sym
        t_preamble = (preamble_len + 4.25) * t_sym

        # Payload symbol calculation (Semtech formula):
        # n_payload = 8 + ceil(max(8*PL - 4*SF + 28 + 16*CRC - 20*H, 0) / (4*(SF - 2*DE))) * CR
        numerator = max(8 * payload_len - 4 * sf + 28 + 16 * crc - 20 * h, 0)
        denominator = 4 * (sf - 2 * de)
        n_payload = 8 + math.ceil(numerator / denominator) * cr

        # Payload time
        t_payload = n_payload * t_sym

        # Total packet airtime
        return t_preamble + t_payload

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

    def record_rx(self, airtime_ms: float):
        """Record received packet airtime (for total RX airtime stats)."""
        self.total_rx_airtime_ms += airtime_ms

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
            "total_rx_airtime_ms": self.total_rx_airtime_ms,
        }
