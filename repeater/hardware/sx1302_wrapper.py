"""SX1302 Radio Wrapper for pyMC_Repeater

This wrapper provides a simplified interface to the SX1302 concentrator
that is compatible with the repeater's expectations.
"""

import asyncio
import ctypes
import logging
import subprocess
import threading
import time
from ctypes import c_int16, c_int32, c_uint16
from typing import Optional, Tuple

from .sx1302_bindings import (
    LGW_SPECTRAL_SCAN_RESULT_SIZE,
    SPECTRAL_SCAN_STATUS_ABORTED,
    SPECTRAL_SCAN_STATUS_COMPLETED,
    SPECTRAL_SCAN_STATUS_NONE,
    STAT_CRC_OK,
    BW_125KHZ,
    BW_250KHZ,
    BW_500KHZ,
    CR_LORA_4_5,
    CR_LORA_4_6,
    CR_LORA_4_7,
    CR_LORA_4_8,
    DR_LORA_SF7,
    DR_LORA_SF8,
    DR_LORA_SF9,
    DR_LORA_SF10,
    DR_LORA_SF11,
    DR_LORA_SF12,
    IMMEDIATE,
    LGW_COM_SPI,
    LGW_HAL_ERROR,
    LGW_HAL_SUCCESS,
    LGW_RADIO_TYPE_SX1250,
    MOD_LORA,
    lgw_board_setconf,
    lgw_conf_board_s,
    lgw_conf_rxif_s,
    lgw_conf_rxrf_s,
    lgw_conf_sx1261_s,
    lgw_pkt_rx_s,
    lgw_pkt_tx_s,
    lgw_receive,
    lgw_rxif_setconf,
    lgw_rxrf_setconf,
    lgw_send,
    lgw_spectral_scan_abort,
    lgw_spectral_scan_get_results,
    lgw_spectral_scan_get_status,
    lgw_spectral_scan_start,
    lgw_start,
    lgw_stop,
    lgw_sx1261_setconf,
)

logger = logging.getLogger("SX1302Radio")


class SX1302Radio:
    """SX1302 concentrator wrapper"""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(**kwargs)
        return cls._instance

    def __init__(
        self,
        frequency=869618000,
        spreading_factor=8,
        bandwidth=62500,
        coding_rate=8,
        preamble_length=17,
        tx_power=14,
        sync_word=13380,
        com_path=b"/dev/spidev0.0",
        sx1261_spi_path=None,
        **kwargs
    ):
        self.frequency = frequency
        self.spreading_factor = spreading_factor
        self.bandwidth = bandwidth
        self.coding_rate = coding_rate
        self.preamble_length = preamble_length
        self.tx_power = tx_power
        self.sync_word = sync_word
        self.com_path = com_path
        self.sx1261_spi_path = sx1261_spi_path

        self.is_started = False
        self._rx_callback = None
        self._rx_thread = None
        self._rx_running = False
        self._last_rssi = -120  # Last measured noise floor (dBm)
        self._last_snr = 0  # Last measured SNR
        self._loop = None  # Store event loop reference
        self._sx1261_enabled = False
        self._last_noise_scan = 0  # Trigger scan immediately on first run
        self._sx1261_abort_count = 0

        logger.info(
            f"Initializing SX1302: freq={frequency}Hz, SF={spreading_factor}, "
            f"BW={bandwidth}Hz, CR=4/{coding_rate}, preamble={preamble_length}"
        )

    def _map_bandwidth(self, bw_hz):
        """Map bandwidth in Hz to SX1302 constant"""
        if bw_hz <= 125000:
            return BW_125KHZ
        elif bw_hz <= 250000:
            return BW_250KHZ
        else:
            return BW_500KHZ

    def _map_coding_rate(self, cr):
        """Map coding rate (5-8) to SX1302 constant"""
        mapping = {5: CR_LORA_4_5, 6: CR_LORA_4_6, 7: CR_LORA_4_7, 8: CR_LORA_4_8}
        return mapping.get(cr, CR_LORA_4_8)

    def _map_spreading_factor(self, sf):
        """Map spreading factor to SX1302 datarate constant"""
        mapping = {
            7: DR_LORA_SF7,
            8: DR_LORA_SF8,
            9: DR_LORA_SF9,
            10: DR_LORA_SF10,
            11: DR_LORA_SF11,
            12: DR_LORA_SF12
        }
        return mapping.get(sf, DR_LORA_SF8)

    def _reset_concentrator(self):
        """Execute GPIO reset sequence for SX1302 (Debian 12 compatible)"""
        try:
            logger.info("Performing SX1302 GPIO reset sequence")

            # Call the reset script (more reliable than individual gpioset calls)
            import os
            script_path = os.path.join(
                os.path.dirname(__file__), "../../sx1302_hal/libloragw/reset_lgw.sh"
            )
            script_path = os.path.abspath(script_path)

            if os.path.exists(script_path):
                result = subprocess.run([script_path], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    logger.info("GPIO reset sequence completed via script")
                    time.sleep(0.5)  # Extra time for hardware stability
                    return True
                else:
                    logger.warning(f"Reset script failed: {result.stderr}")
            else:
                logger.warning(f"Reset script not found at {script_path}, using manual sequence")

            # Fallback: manual GPIO reset using gpioset
            gpioset_cmd = ["gpioset", "-m", "time", "-u", "100000", "gpiochip0"]

            # Power enable (GPIO 18)
            subprocess.run(gpioset_cmd + ["18=1"], check=False, capture_output=True)
            time.sleep(0.01)

            # SX1302 reset (GPIO 17) - main concentrator chip
            subprocess.run(gpioset_cmd + ["17=1"], check=False, capture_output=True)
            time.sleep(0.01)
            subprocess.run(gpioset_cmd + ["17=0"], check=False, capture_output=True)
            time.sleep(0.01)

            # SX1261 reset (GPIO 5) - spectral scan radio
            subprocess.run(gpioset_cmd + ["5=0"], check=False, capture_output=True)
            time.sleep(0.01)
            subprocess.run(gpioset_cmd + ["5=1"], check=False, capture_output=True)
            time.sleep(0.01)

            # ADC reset (GPIO 13)
            subprocess.run(gpioset_cmd + ["13=0"], check=False, capture_output=True)
            time.sleep(0.01)
            subprocess.run(gpioset_cmd + ["13=1"], check=False, capture_output=True)
            time.sleep(0.5)  # Wait for hardware to stabilize

            logger.info("GPIO reset sequence completed")
            return True

        except Exception as e:
            logger.warning(f"GPIO reset failed (may not be critical): {e}")
            return False

    def begin(self):
        """Initialize and start the concentrator"""
        if self.is_started:
            logger.warning("SX1302 already started")
            return True

        # Capture the event loop for callback scheduling
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, will try to get it later
            self._loop = None

        try:
            # Stop concentrator if it's running from previous session
            try:
                lgw_stop()
                logger.debug("Stopped any existing concentrator instance")
                time.sleep(0.2)
            except:
                pass

            # Perform GPIO reset sequence
            self._reset_concentrator()

            # Configure board
            board_conf = lgw_conf_board_s()
            board_conf.lorawan_public = False
            board_conf.clksrc = 0
            board_conf.full_duplex = False
            board_conf.com_type = LGW_COM_SPI

            # Ensure com_path is bytes
            if isinstance(self.com_path, str):
                board_conf.com_path = self.com_path.encode('utf-8')
            else:
                board_conf.com_path = self.com_path

            logger.debug(f"Board config: com_type={board_conf.com_type}, com_path={board_conf.com_path}")

            ret = lgw_board_setconf(board_conf)
            if ret != LGW_HAL_SUCCESS:
                logger.error(f"Board config failed with code: {ret}")
                logger.error(f"Ensure SPI device exists: {self.com_path}")
                logger.error("Check permissions: user should be in 'spi' and 'gpio' groups")
                return False

            logger.info("Board configuration successful")

            # Configure RF chain 0 (main RX/TX)
            rf_conf = lgw_conf_rxrf_s()
            rf_conf.enable = True
            rf_conf.freq_hz = self.frequency
            rf_conf.rssi_offset = -166.0
            rf_conf.type = LGW_RADIO_TYPE_SX1250
            rf_conf.tx_enable = True
            rf_conf.single_input_mode = False

            ret = lgw_rxrf_setconf(0, rf_conf)
            if ret != LGW_HAL_SUCCESS:
                logger.error(f"RF chain config failed: {ret}")
                return False

            # Configure IF chain for LoRa
            # SX1302 IF chains: 0-7 are multi-SF (125kHz only), 8 is LoRa standard (any BW)
            if_chain_num = 8 if self.bandwidth != 125000 else 0

            if_conf = lgw_conf_rxif_s()
            if_conf.enable = True
            if_conf.rf_chain = 0
            if_conf.freq_hz = 0  # Center frequency (relative to RF)
            if_conf.bandwidth = self._map_bandwidth(self.bandwidth)
            if_conf.datarate = self._map_spreading_factor(self.spreading_factor)
            if_conf.sync_word_size = 0
            if_conf.sync_word = 0

            ret = lgw_rxif_setconf(if_chain_num, if_conf)
            if ret != LGW_HAL_SUCCESS:
                logger.error(f"IF chain config failed: {ret}")
                return False

            # Configure SX1261 companion chip for spectral scan noise floor (optional)
            if self.sx1261_spi_path:
                sx1261_conf = lgw_conf_sx1261_s()
                sx1261_conf.enable = True
                path = self.sx1261_spi_path
                sx1261_conf.spi_path = path if isinstance(path, bytes) else path.encode()
                sx1261_conf.rssi_offset = 0
                sx1261_conf.lbt_conf.enable = False
                ret = lgw_sx1261_setconf(sx1261_conf)
                if ret == LGW_HAL_SUCCESS:
                    self._sx1261_enabled = True
                    logger.info("SX1261 configured for spectral scan noise floor")
                else:
                    logger.warning("SX1261 setconf failed — noise floor unavailable")

            # Start concentrator
            ret = lgw_start()
            if ret != LGW_HAL_SUCCESS:
                logger.error(f"Start failed: {ret}")
                return False

            self.is_started = True
            logger.info("SX1302 concentrator started successfully")

            # Start RX thread
            self._rx_running = True
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()

            return True

        except Exception as e:
            logger.error(f"Failed to initialize SX1302: {e}")
            return False

    def end(self):
        """Stop the concentrator"""
        if not self.is_started:
            return

        self._rx_running = False
        if self._rx_thread:
            self._rx_thread.join(timeout=2.0)

        lgw_stop()
        self.is_started = False
        logger.info("SX1302 concentrator stopped")

    async def send(self, data: bytes) -> bool:
        """Send a packet"""
        if not self.is_started:
            logger.error("Cannot send: concentrator not started")
            return False

        pkt = lgw_pkt_tx_s()
        pkt.freq_hz = self.frequency
        pkt.tx_mode = IMMEDIATE
        pkt.rf_chain = 0
        pkt.rf_power = self.tx_power
        pkt.modulation = MOD_LORA
        pkt.bandwidth = self._map_bandwidth(self.bandwidth)
        pkt.datarate = self.spreading_factor
        pkt.coderate = self._map_coding_rate(self.coding_rate)
        pkt.invert_pol = False
        pkt.preamble = self.preamble_length
        pkt.no_crc = False
        pkt.no_header = False
        pkt.size = len(data)

        for i, byte in enumerate(data):
            pkt.payload[i] = byte

        ret = lgw_send(pkt)
        if ret != LGW_HAL_SUCCESS:
            logger.error(f"Send failed: {ret}")
            return False

        return True

    def _measure_noise_floor(self):
        """Run a spectral scan on the operating frequency and update _last_rssi."""
        if not self._sx1261_enabled:
            return
        nb_scan = 200
        ret = lgw_spectral_scan_start(self.frequency, nb_scan)
        if ret != LGW_HAL_SUCCESS:
            logger.debug(f"Spectral scan start failed: {ret}")
            return

        status = c_int32(SPECTRAL_SCAN_STATUS_NONE)
        for _ in range(40):          # 40 × 50ms = 2s max
            lgw_spectral_scan_get_status(ctypes.byref(status))
            if status.value == SPECTRAL_SCAN_STATUS_COMPLETED:
                self._sx1261_abort_count = 0
                break
            if status.value == SPECTRAL_SCAN_STATUS_ABORTED:
                self._sx1261_abort_count += 1
                if self._sx1261_abort_count >= 3:
                    logger.warning("SX1261 spectral scan aborting repeatedly — disabling noise floor (no SX1261 on this hardware?)")
                    self._sx1261_enabled = False
                else:
                    logger.debug(f"Spectral scan aborted (status={status.value})")
                return
            time.sleep(0.05)
        else:
            logger.debug("Spectral scan timed out")
            lgw_spectral_scan_abort()
            return

        levels = (c_int16 * LGW_SPECTRAL_SCAN_RESULT_SIZE)()
        counts = (c_uint16 * LGW_SPECTRAL_SCAN_RESULT_SIZE)()
        ret = lgw_spectral_scan_get_results(ctypes.byref(levels), ctypes.byref(counts))
        if ret != LGW_HAL_SUCCESS:
            logger.debug(f"Spectral scan get_results failed: {ret}")
            return

        logger.debug(f"Spectral scan results: { {int(levels[i]): counts[i] for i in range(LGW_SPECTRAL_SCAN_RESULT_SIZE)} }")

        # Noise floor = highest threshold (closest to 0 dBm) where any samples were detected.
        # Levels decrease: 0, -4, -8 ... -128. Take the first bin with a non-zero count.
        noise_floor = None
        for i in range(LGW_SPECTRAL_SCAN_RESULT_SIZE):
            if counts[i] > 0:
                noise_floor = int(levels[i])
                break
        if noise_floor is not None:
            self._last_rssi = noise_floor
            logger.debug(f"Noise floor (spectral scan): {noise_floor} dBm")
        else:
            logger.debug("Spectral scan completed but no counts found")

    def _rx_loop(self):
        """Background thread for receiving packets"""
        while self._rx_running:
            try:
                packets = lgw_receive(max_pkt=8)
                for pkt in packets:
                    # Update SNR from packet
                    if pkt.size > 0:
                        self._last_snr = int(pkt.snr)

                    if pkt.size > 0 and pkt.status != STAT_CRC_OK:
                        logger.info(f"Dropped packet: bad CRC (status=0x{pkt.status:02X}, size={pkt.size})")

                    if self._rx_callback and pkt.size > 0 and pkt.status == STAT_CRC_OK:
                        payload = bytes(pkt.payload[: pkt.size])
                        # Schedule callback in the event loop
                        if self._loop:
                            if asyncio.iscoroutinefunction(self._rx_callback):
                                asyncio.run_coroutine_threadsafe(self._rx_callback(payload), self._loop)
                            else:
                                self._loop.call_soon_threadsafe(self._rx_callback, payload)
                        else:
                            # No event loop available, call directly (fallback)
                            logger.warning("No event loop available, calling callback directly")
                            self._rx_callback(payload)

                # Trigger noise floor scan every 30 seconds
                if time.time() - self._last_noise_scan >= 30:
                    self._measure_noise_floor()
                    self._last_noise_scan = time.time()
            except Exception as e:
                logger.error(f"RX loop error: {e}")
                time.sleep(0.1)

            time.sleep(0.01)  # Small delay to prevent CPU spinning

    def on_receive(self, callback):
        """Set callback for received packets"""
        self._rx_callback = callback

    def set_rx_callback(self, callback):
        """Set callback for received packets (alias for compatibility)"""
        self.on_receive(callback)

    def get_rssi(self) -> int:
        """Get current channel RSSI (noise floor) from last measurement"""
        return self._last_rssi

    def get_last_rssi(self) -> int:
        """Get last RSSI (alias for compatibility with dispatcher)"""
        return self.get_rssi()

    def get_noise_floor(self) -> int:
        """Get noise floor (alias for engine compatibility)"""
        return self.get_rssi()

    def get_last_snr(self) -> int:
        """Get last measured SNR"""
        return self._last_snr

    def standby(self):
        """Put radio in standby (not applicable to concentrator)"""
        pass

    def sleep(self):
        """Put radio to sleep (not applicable to concentrator)"""
        pass

    def set_frequency(self, freq_hz: int):
        """Set frequency (requires restart)"""
        logger.warning("Frequency change requires concentrator restart")
        self.frequency = freq_hz
