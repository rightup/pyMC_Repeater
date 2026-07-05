"""
BME280 temperature, humidity, and pressure sensor plug-in.

If you're using docker, be sure to add the I2C group to group_add
You can find that group ID by running `getent group i2c`

Requires: pip install smbus2

Config example:
  - type: bme280
    name: "ambient"
    enabled: true
    auto_install_packages: true
    settings:
      i2c_address: 0x76
      bus_number: 1
      read_timeout_seconds: 1.0
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry

# Register addresses
_REG_ID = 0xD0
_REG_STATUS = 0xF3
_REG_CTRL_HUM = 0xF2
_REG_CTRL_MEAS = 0xF4
_REG_DATA = 0xF7


@dataclass
class Calibration:
    T1: int
    T2: int
    T3: int

    P1: int
    P2: int
    P3: int
    P4: int
    P5: int
    P6: int
    P7: int
    P8: int
    P9: int

    H1: int
    H2: int
    H3: int
    H4: int
    H5: int
    H6: int


@SensorRegistry.register("bme280")
class BME280Sensor(SensorBase):
    sensor_type = "bme280"

    CHIP_ID = 0x60

    def __init__(
        self,
        name: str,
        config: Optional[Dict[str, Any]] = None,
        log=None,
    ):
        super().__init__(name=name, config=config, log=log)

        self.i2c_address = int(self.settings.get("i2c_address", 0x76))
        self.bus_number = int(self.settings.get("bus_number", 1))
        self.read_timeout_seconds = float(
            self.settings.get("read_timeout_seconds", 1.0)
        )

        self.available = False
        self._cal: Optional[Calibration] = None

        if not self.ensure_python_modules(
            [
                ("smbus2", "smbus2"),
            ]
        ):
            return

        try:
            import smbus2  # type: ignore

            self._smbus2 = smbus2

            with smbus2.SMBus(self.bus_number) as bus:
                chip = bus.read_byte_data(self.i2c_address, _REG_ID)

                if chip != self.CHIP_ID:
                    raise RuntimeError(
                        f"Unexpected chip ID 0x{chip:02X} (expected 0x60)"
                    )

                self._cal = self._read_calibration(bus)

            self.available = True

            self.log.info(
                "BME280 initialized (addr=0x%02X, bus=%d)",
                self.i2c_address,
                self.bus_number,
            )

        except Exception as exc:
            self.available = False
            self.log.warning(
                "BME280 init failed (addr=0x%02X, bus=%d): %s",
                self.i2c_address,
                self.bus_number,
                exc,
            )

    @staticmethod
    def _u16(lsb: int, msb: int) -> int:
        return (msb << 8) | lsb

    @staticmethod
    def _s16(lsb: int, msb: int) -> int:
        value = (msb << 8) | lsb
        if value > 32767:
            value -= 65536
        return value

    def _read_calibration(self, bus) -> Calibration:
        c1 = bus.read_i2c_block_data(self.i2c_address, 0x88, 24)
        c2 = bus.read_i2c_block_data(self.i2c_address, 0xE1, 7)

        h4 = (c2[3] << 4) | (c2[4] & 0x0F)
        if h4 & 0x800:
            h4 -= 4096

        h5 = (c2[5] << 4) | (c2[4] >> 4)
        if h5 & 0x800:
            h5 -= 4096

        h6 = c2[6]
        if h6 > 127:
            h6 -= 256

        return Calibration(
            T1=self._u16(c1[0], c1[1]),
            T2=self._s16(c1[2], c1[3]),
            T3=self._s16(c1[4], c1[5]),
            P1=self._u16(c1[6], c1[7]),
            P2=self._s16(c1[8], c1[9]),
            P3=self._s16(c1[10], c1[11]),
            P4=self._s16(c1[12], c1[13]),
            P5=self._s16(c1[14], c1[15]),
            P6=self._s16(c1[16], c1[17]),
            P7=self._s16(c1[18], c1[19]),
            P8=self._s16(c1[20], c1[21]),
            P9=self._s16(c1[22], c1[23]),
            H1=bus.read_byte_data(self.i2c_address, 0xA1),
            H2=self._s16(c2[0], c2[1]),
            H3=c2[2],
            H4=h4,
            H5=h5,
            H6=h6,
        )

    def _compensate_temperature(self, adc_t: int):
        """Return (temperature_c, t_fine)."""
        c = self._cal
        assert c is not None

        var1 = ((adc_t / 16384.0) - (c.T1 / 1024.0)) * c.T2
        var2 = (((adc_t / 131072.0) - (c.T1 / 8192.0)) ** 2) * c.T3

        t_fine = var1 + var2
        temperature = t_fine / 5120.0

        return temperature, t_fine

    def _compensate_pressure(self, adc_p: int, t_fine: float) -> float:
        """Return pressure in hPa."""
        c = self._cal
        assert c is not None

        var1 = t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * c.P6 / 32768.0
        var2 += var1 * c.P5 * 2.0
        var2 = var2 / 4.0 + c.P4 * 65536.0

        var1 = (
            (c.P3 * var1 * var1 / 524288.0)
            + (c.P2 * var1)
        ) / 524288.0

        var1 = (1.0 + var1 / 32768.0) * c.P1

        if var1 == 0:
            return 0.0

        pressure = 1048576.0 - adc_p
        pressure = (pressure - var2 / 4096.0) * 6250.0 / var1

        var1 = c.P9 * pressure * pressure / 2147483648.0
        var2 = pressure * c.P8 / 32768.0

        pressure += (var1 + var2 + c.P7) / 16.0

        return pressure / 100.0

    def _compensate_humidity(self, adc_h: int, t_fine: float) -> float:
        """Return relative humidity in percent."""
        c = self._cal
        assert c is not None

        humidity = t_fine - 76800.0

        humidity = (
            adc_h
            - (c.H4 * 64.0 + c.H5 / 16384.0 * humidity)
        ) * (
            c.H2
            / 65536.0
            * (
                1.0
                + c.H6 / 67108864.0 * humidity
                * (
                    1.0
                    + c.H3 / 67108864.0 * humidity
                )
            )
        )

        humidity *= (
            1.0
            - c.H1 * humidity / 524288.0
        )

        return max(0.0, min(100.0, humidity))

    def _read(self) -> Dict[str, Any]:
        if not self.available:
            raise RuntimeError("BME280 device not available")

        try:
            with self._smbus2.SMBus(self.bus_number) as bus:

                # Humidity oversampling x1
                bus.write_byte_data(self.i2c_address, _REG_CTRL_HUM, 0x01)

                # Temperature x1, Pressure x1, Forced mode
                bus.write_byte_data(self.i2c_address, _REG_CTRL_MEAS, 0x25)

                deadline = time.time() + self.read_timeout_seconds

                while bus.read_byte_data(self.i2c_address, _REG_STATUS) & 0x08:
                    if time.time() > deadline:
                        raise RuntimeError("Timed out waiting for measurement")
                    time.sleep(0.005)

                data = bus.read_i2c_block_data(self.i2c_address, _REG_DATA, 8)

            adc_p = (
                (data[0] << 12)
                | (data[1] << 4)
                | (data[2] >> 4)
            )

            adc_t = (
                (data[3] << 12)
                | (data[4] << 4)
                | (data[5] >> 4)
            )

            adc_h = (data[6] << 8) | data[7]

            temperature, t_fine = self._compensate_temperature(adc_t)
            pressure = self._compensate_pressure(adc_p, t_fine)
            humidity = self._compensate_humidity(adc_h, t_fine)

            return {
                "temperature_c": round(temperature, 2),
                "humidity_pct": round(humidity, 2),
                "pressure_hpa": round(pressure, 2),
            }

        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"BME280 read failed: {exc}") from exc
