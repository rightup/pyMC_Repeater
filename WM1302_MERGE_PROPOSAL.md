# WM1302 Fork Merge Proposal

**From**: l34rn3d/pyMC_Repeater_WM1302 — branch `dev_merge`
**Into**: rightup/pyMC_Repeater
**Date**: February 25, 2026

---

## Overview

Adds WM1302/SX1302 LoRa concentrator support. Validated on Debian 13 (Trixie) aarch64 — sensecap-zebra and sensecap-cricket.

All existing SX1262 hardware, config, and install paths are unchanged.

---

## New Files

| File | Purpose |
|------|---------|
| `setup-sx1302.sh` | Builds sx1302_hal, writes GPIO reset script, writes config, restarts service. Run once after web wizard selects WM1302. |
| `repeater/hardware/sx1302_wrapper.py` | WM1302 radio driver (thread-safe, async-compatible) |
| `repeater/hardware/sx1302_bindings.py` | ctypes bindings for Semtech sx1302_hal C library |

---

## Modified Files

### `radio-settings.json`
Added one new hardware profile:
```json
"sx1302": {
  "name": "SX1302 LoRa Concentrator",
  "hardware_type": "sx1302",
  "com_path": "/dev/spidev0.0",
  "tx_power": 26,
  "preamble_length": 17
}
```

### `repeater/config.py`
Added `radio_type` dispatch so `get_radio_for_board()` instantiates `SX1302Radio` when `radio_type == "sx1302"`. SX1262 path unchanged.

### `manage.sh`
No SX1302-specific logic. SX1262 install and upgrade paths unchanged.

### `setup-radio-config.sh`
Key additions over upstream:
- `create_reset_script()` — pinctrl-based GPIO reset (pins 17/18/5/13)
- Hardware type detection, SX1302 vs SX1262 branch
- SX1302 library clone/build for the terminal wizard path

### `radio-presets.json`
- Added Australia (Narrow), Australia: SA/WA, Australia: QLD presets
- Czech Republic corrected to 869.432 MHz
- Vietnam split into Narrow and Deprecated presets

---

## SX1302 vs SX1262

| | SX1262 | SX1302 |
|---|---|---|
| TX power | 22 dBm | 26 dBm |
| Bandwidth | 62.5 / 125 / 250 / 500 kHz | 125 / 250 / 500 kHz only |
| Build requirement | None | `gcc`, `make`, `build-essential` via `setup-sx1302.sh` |
| RX loop | Python | Background thread calling C library |
| Noise floor | Not available | SX1261 spectral scan every 30 s → SQLite |
| CRC check | Library-handled | Explicit `STAT_CRC_OK` check; bad packets dropped |

---

## sx1302_hal Dependency

Requires Semtech's open-source C library: https://github.com/Lora-net/sx1302_hal

Not included in the repository — `setup-sx1302.sh` clones and builds it on the target device.

---

## SX1302 Installation Flow

After `sudo ./manage.sh install` and selecting SX1302 hardware in the web wizard, run:

```bash
sudo ./setup-sx1302.sh
```

The web wizard has no mechanism to trigger this automatically.

---

## Backward Compatibility

- No changes to SX1262 driver, config schema, APIs, or service behaviour
- `manage.sh` install/upgrade: no C build, no extra dependencies for SX1262 users
- `radio_type` key only required for SX1302 — existing configs are unaffected
- No new Python dependencies
