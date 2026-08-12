"""
BME280 temperature, humidity and barometric pressure sensor plug-in.

Requires: pip install smbus2

Config example:
  - type: bme280
    name: "ambient"
    enabled: true
    auto_install_packages: false
    settings:
      i2c_address: 0x76   # 0x77 when SDO is tied high
      bus_number: 1        # I2C bus number (1 for Raspberry Pi default)
      read_timeout_seconds: 1.0  # Max time to wait for the measurement (polls every 5 ms)
"""

from __future__ import annotations

import struct
import time
from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry

# BME280 register addresses
_REG_CALIB_LOW = 0x88  # dig_T1..dig_P9 + dig_H1, 26 bytes
_REG_CALIB_HIGH = 0xE1  # dig_H2..dig_H6, 7 bytes
_REG_ID = 0xD0
_REG_CTRL_HUM = 0xF2
_REG_STATUS = 0xF3
_REG_CTRL_MEAS = 0xF4
_REG_CONFIG = 0xF5
_REG_DATA = 0xF7  # press(3) + temp(3) + hum(2)

_CHIP_ID = 0x60
_CHIP_ID_BMP280 = 0x58  # Same register map, no humidity channel

_OVERSAMPLING_X1 = 0x01
_MODE_FORCED = 0x01
# osrs_t x1, osrs_p x1, forced mode
_CTRL_MEAS_FORCED = (_OVERSAMPLING_X1 << 5) | (_OVERSAMPLING_X1 << 2) | _MODE_FORCED
_STATUS_MEASURING = 0x08


@SensorRegistry.register("bme280")
class BME280Sensor(SensorBase):
    sensor_type = "bme280"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None, log=None):
        super().__init__(name=name, config=config, log=log)

        self.i2c_address = int(self.settings.get("i2c_address", 0x76))
        self.bus_number = int(self.settings.get("bus_number", 1))
        self._poll_interval = 0.005  # 5 ms between measurement-complete checks
        self._poll_attempts = max(
            1, int(float(self.settings.get("read_timeout_seconds", 1.0)) / self._poll_interval)
        )
        self._calibration: Optional[Dict[str, Any]] = None

        self.available = False
        if not self.ensure_python_modules([("smbus2", "smbus2")]):
            return

        try:
            import smbus2  # type: ignore[import-not-found]

            self._smbus2 = smbus2

            bus = smbus2.SMBus(self.bus_number)
            try:
                chip_id = bus.read_byte_data(self.i2c_address, _REG_ID)
                if chip_id != _CHIP_ID:
                    hint = (
                        " (that is a BMP280, which has no humidity channel)"
                        if (chip_id == _CHIP_ID_BMP280)
                        else ""
                    )
                    raise RuntimeError(f"unexpected chip id 0x{chip_id:02X}{hint}")
                self._calibration = self._read_calibration(bus)
            finally:
                bus.close()

            self.available = True
            self.log.info(
                "BME280 initialized (addr=0x%02X, bus=%d)",
                self.i2c_address,
                self.bus_number,
            )
        except Exception as exc:
            self.log.warning(
                "BME280 init failed (addr=0x%02X, bus=%d): %s",
                self.i2c_address,
                self.bus_number,
                exc,
            )
            self.available = False

    def _read_calibration(self, bus) -> Dict[str, Any]:
        """Read the factory calibration block."""
        low = bytes(bus.read_i2c_block_data(self.i2c_address, _REG_CALIB_LOW, 26))
        high = bytes(bus.read_i2c_block_data(self.i2c_address, _REG_CALIB_HIGH, 7))

        t1, t2, t3 = struct.unpack_from("<Hhh", low, 0)
        p1, p2, p3, p4, p5, p6, p7, p8, p9 = struct.unpack_from("<Hhhhhhhhh", low, 6)
        h1 = low[25]

        h2 = struct.unpack_from("<h", high, 0)[0]
        h3 = high[2]
        # dig_H4 and dig_H5 are signed 12-bit values sharing the middle byte
        h4 = (high[3] << 4) | (high[4] & 0x0F)
        h5 = (high[5] << 4) | (high[4] >> 4)
        h4 = h4 - 4096 if h4 > 2047 else h4
        h5 = h5 - 4096 if h5 > 2047 else h5
        h6 = struct.unpack_from("<b", high, 6)[0]

        return {
            "t": (t1, t2, t3),
            "p": (p1, p2, p3, p4, p5, p6, p7, p8, p9),
            "h": (h1, h2, h3, h4, h5, h6),
        }

    def _measure(self, bus) -> tuple[int, int, int]:
        """Trigger one forced-mode measurement and return raw ADC counts."""
        # ctrl_hum only takes effect on the following ctrl_meas write
        bus.write_byte_data(self.i2c_address, _REG_CTRL_HUM, _OVERSAMPLING_X1)
        bus.write_byte_data(self.i2c_address, _REG_CONFIG, 0x00)  # IIR filter off
        bus.write_byte_data(self.i2c_address, _REG_CTRL_MEAS, _CTRL_MEAS_FORCED)

        for _ in range(self._poll_attempts):
            time.sleep(self._poll_interval)
            if not bus.read_byte_data(self.i2c_address, _REG_STATUS) & _STATUS_MEASURING:
                break
        else:
            raise RuntimeError(
                f"measurement timed out after {self._poll_attempts * self._poll_interval:.1f}s"
            )

        d = bus.read_i2c_block_data(self.i2c_address, _REG_DATA, 8)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        adc_h = (d[6] << 8) | d[7]
        return adc_t, adc_p, adc_h

    def _compensate(self, adc_t: int, adc_p: int, adc_h: int) -> tuple[float, float, float]:
        """Apply the datasheet floating-point compensation formulas (section 8.1)."""
        calibration = self._calibration
        t1, t2, t3 = calibration["t"]
        p1, p2, p3, p4, p5, p6, p7, p8, p9 = calibration["p"]
        h1, h2, h3, h4, h5, h6 = calibration["h"]

        var1 = (adc_t / 16384.0 - t1 / 1024.0) * t2
        var2 = ((adc_t / 131072.0 - t1 / 8192.0) ** 2) * t3
        t_fine = var1 + var2
        temperature_c = t_fine / 5120.0

        var1 = t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * p6 / 32768.0
        var2 = var2 + var1 * p5 * 2.0
        var2 = var2 / 4.0 + p4 * 65536.0
        var1 = (p3 * var1 * var1 / 524288.0 + p2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * p1
        if var1 == 0:
            raise RuntimeError("pressure compensation divided by zero")
        pressure = 1048576.0 - adc_p
        pressure = (pressure - var2 / 4096.0) * 6250.0 / var1
        var1 = p9 * pressure * pressure / 2147483648.0
        var2 = pressure * p8 / 32768.0
        pressure_pa = pressure + (var1 + var2 + p7) / 16.0

        humidity = t_fine - 76800.0
        humidity = (adc_h - (h4 * 64.0 + h5 / 16384.0 * humidity)) * (
            h2 / 65536.0 * (1.0 + h6 / 67108864.0 * humidity * (1.0 + h3 / 67108864.0 * humidity))
        )
        humidity = humidity * (1.0 - h1 * humidity / 524288.0)
        humidity_pct = min(100.0, max(0.0, humidity))

        return temperature_c, pressure_pa, humidity_pct

    def _read(self) -> Dict[str, Any]:
        """Read temperature, pressure and humidity from the BME280."""
        if not self.available:
            raise RuntimeError("BME280 device not available")

        bus = self._smbus2.SMBus(self.bus_number)
        try:
            temperature_c, pressure_pa, humidity_pct = self._compensate(*self._measure(bus))
            return {
                "temperature_c": round(temperature_c, 2),
                "humidity_pct": round(humidity_pct, 2),
                "pressure_hpa": round(pressure_pa / 100.0, 2),
            }
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"BME280 read failed: {exc}") from exc
        finally:
            bus.close()
