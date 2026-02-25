# WM1302 Fork Merge Proposal

**From**: l34rn3d/pyMC_Repeater_WM1302 — branch `dev_merge`
**Into**: rightup/pyMC_Repeater
**Date**: February 25, 2026

---

## Overview

This fork adds full WM1302/SX1302 LoRa concentrator support to pyMC_Repeater while maintaining complete backward compatibility with all existing SX1262-based hardware.

Validated on Debian 13 (Trixie) aarch64 — sensecap-zebra and sensecap-cricket with SX1302 hardware.

---

## New Files

| File | Purpose |
|------|---------|
| `setup-sx1302.sh` | One-shot SX1302 setup script — run once after selecting WM1302 in the web wizard |
| `repeater/hardware/__init__.py` | Package marker for hardware module |
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
Removed the earlier stub `build_sx1302_library()`. No SX1302-specific logic remains. SX1262 install and upgrade paths are unchanged.

### `setup-radio-config.sh`
Replaced with fork version. Key additions over upstream:
- `create_reset_script()` function (pinctrl-based, GPIO 17/18/5/13)
- Hardware type detection (sx1302 vs sx1262 branch)
- SX1302 library clone/build logic for terminal wizard path

### `radio-presets.json`
- Added Australia (Narrow), Australia: SA/WA, Australia: QLD presets
- Czech Republic frequency corrected to 869.432 MHz
- Vietnam split into Narrow and Deprecated presets

### `.gitignore`
Added `sx1302_hal/` — library is built from source at install time, not stored in the repository.

---

## Key Functional Differences

### Hardware
- Upstream: 6 × SX1262 transceiver modules
- Fork: All 6 upstream + WM1302/SX1302 concentrator

### TX Power
- SX1262: 22 dBm max
- SX1302: 26 dBm max

### Bandwidth Constraint
- SX1302 **cannot** operate at 62.5 kHz — limited to 125/250/500 kHz
- Documented in hardware profile

### Build Requirement
- SX1262: pure Python, no change to install flow
- SX1302: requires `gcc`, `make`, `build-essential` to compile sx1302_hal. `setup-sx1302.sh` handles this — SX1262 users are not affected.

### Threading Model
The SX1302 uses a background C library receive loop. The driver captures the asyncio event loop at init and uses `asyncio.run_coroutine_threadsafe()` to schedule callbacks — avoids blocking the main async loop.

### Noise Floor Measurement
SX1302 includes an SX1261 companion chip capable of spectral scanning. The driver runs a 200-sample spectral scan every 30 seconds at the operating frequency and records the result to the SQLite `noise_floor` table. SX1262 hardware does not do this.

### CRC Validation
Explicit `STAT_CRC_OK` check in the SX1302 RX loop before passing packets to the dispatcher. Corrupted packets are dropped and logged at INFO level.

---

## Backward Compatibility

- All existing SX1262 configurations work with zero changes
- `manage.sh` install/upgrade is unchanged for SX1262 users — no C build, no extra dependencies
- `radio_type` field only required for SX1302; SX1262 profiles are unchanged
- No changes to public APIs, config schema, or service behaviour for SX1262 users
- No new Python dependencies

---

## sx1302_hal Dependency

The WM1302 requires Semtech's open-source C library:
https://github.com/Lora-net/sx1302_hal

`sx1302_hal/` is **not** included in the repository. `setup-sx1302.sh` clones it from GitHub and builds it for the target architecture. Pre-built binaries are not distributed.

---

## SX1302 Installation Flow

After `sudo ./manage.sh install` and selecting SX1302 hardware in the web wizard, the user must run:

```bash
sudo ./setup-sx1302.sh
```

This builds `libloragw.so`, writes the GPIO reset script, writes `radio_type: "sx1302"` and the `sx1302:` block to `config.yaml`, and restarts the service. There is currently no mechanism to trigger this automatically from the web wizard.

---

## What This PR Does NOT Change

- Python version requirement
- pymc_core dependency version
- Web dashboard
- MQTT, RRDTool, SQLite storage logic
- Systemd service configuration
- Any SX1262 driver code
- `manage.sh` install/upgrade flow for SX1262 users
