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
Replaced stub `build_sx1302_library()` with `setup_sx1302_hal()` and `create_sx1302_reset_script()`.

`setup_sx1302_hal()` runs during both install and upgrade and:
- Clones `Lora-net/sx1302_hal` from GitHub if not present
- Patches `libtools/Makefile` and `libloragw/Makefile` to add `-fPIC` (required for shared library on all architectures including aarch64)
- Builds `libtools` and `libloragw` static archives
- Globs all `.a` files from `libtools/` and links them with `libloragw.a` into `libloragw.so`
- Calls `create_sx1302_reset_script()` to write the GPIO reset script

`create_sx1302_reset_script()` writes a `pinctrl`-based `reset_lgw.sh` to `sx1302_hal/libloragw/` compatible with Raspberry Pi OS Bookworm and Debian 13 Trixie.

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
- Documented in hardware profile; setup script filters incompatible presets

### Build Requirement
- SX1262: pure Python, no change
- SX1302: requires `gcc`, `make`, `swig`, `build-essential` to compile sx1302_hal once at install time. `manage.sh` handles this automatically.

### Threading Model
The SX1302 uses a background C library receive loop. The driver captures the asyncio event loop at init and uses `asyncio.run_coroutine_threadsafe()` to schedule callbacks — avoids blocking the main async loop.

### Noise Floor Measurement
SX1302 includes an SX1261 companion chip capable of spectral scanning. The driver runs a 200-sample spectral scan every 30 seconds at the operating frequency and records the result to the SQLite `noise_floor` table. SX1262 hardware does not do this.

### CRC Validation
Explicit `STAT_CRC_OK` check in the SX1302 RX loop before passing packets to the dispatcher. Corrupted packets are dropped and logged at INFO level.

---

## Backward Compatibility

- All existing SX1262 configurations work with zero changes
- `radio_type` field only required for SX1302; SX1262 profiles are unchanged
- No changes to public APIs, config schema, or service behaviour for SX1262 users
- No new Python dependencies

---

## sx1302_hal Dependency

The WM1302 requires Semtech's open-source C library:
https://github.com/Lora-net/sx1302_hal

`sx1302_hal/` is **not** included in the repository. `manage.sh` clones it from GitHub at install time and builds it for the target architecture. Pre-built binaries are not distributed.

---

## What This PR Does NOT Change

- Python version requirement
- pymc_core dependency version
- Web dashboard
- MQTT, RRDTool, SQLite storage logic
- Systemd service configuration
- Any SX1262 driver code

---

## Integration Gaps Found During Testing

### 1. Web UI does not write `radio_type` or `sx1302` config section *(open — requires upstream fix)*

**Impact**: Critical. The upstream web setup wizard accepts SX1302 hardware selection but does not write `radio_type: "sx1302"` or the `sx1302:` block to `config.yaml`. On service restart, `get_radio_for_board()` defaults to `sx1262`, loading the wrong driver.

**Required fix**: The upstream web setup wizard and `ConfigManager` need to handle `radio_type` and the `sx1302` config section when SX1302 hardware is selected. This is the largest additional change required for a clean merge.

**Current workaround**:
```bash
sudo tee -a /etc/pymc_repeater/config.yaml << 'EOF'

radio_type: "sx1302"

sx1302:
  com_path: "/dev/spidev0.0"
  sx1261_spi_path: "/dev/spidev0.1"
EOF
sudo systemctl restart pymc-repeater
```

---

### 2. `manage.sh` install does not copy or build `sx1302_hal` *(fixed in dev_merge)*

**Impact**: Service crashes on first start with `libloragw.so: cannot open shared object file`.

**Fix applied**: `setup_sx1302_hal()` in `manage.sh` clones `Lora-net/sx1302_hal` from GitHub and builds it during install and upgrade. Called automatically — no manual steps required.

---

### 3. `sx1302_hal` must be built from source on the target device *(fixed in dev_merge)*

**Impact**: Pre-compiled x86_64 artifacts in the original fork repository are unusable on aarch64 hardware.

**Fix applied**: `sx1302_hal/` removed from repository and added to `.gitignore`. `setup_sx1302_hal()` always clones and builds from source on the target device.

---

### 4. GPIO reset script (`reset_lgw.sh`) is not created during install *(fixed in dev_merge)*

**Impact**: `lgw_start()` returns -1 (hardware not responding) because the concentrator is not properly reset before initialisation.

**Fix applied**: `create_sx1302_reset_script()` is called by `setup_sx1302_hal()` after every clone/build. Uses `pinctrl` (not `gpioset`) — required on Debian 12/13.

---

## Bugs Found and Fixed During Testing

### 5. `libloragw.so` missing `tinymt32_init` symbol *(fixed in dev_merge)*

**Impact**: `OSError: libloragw.so: undefined symbol: tinymt32_init` on service start.

**Root cause**: `libtools` in `sx1302_hal` builds separate archives (`libtinymt32.a`, `libparson.a`, `libbase64.a`) rather than a single `libtools.a`. The original link step assumed one archive and silently omitted the others, producing an incomplete `.so`.

**Fix applied**: `setup_sx1302_hal()` now globs all `.a` files from `libtools/` and includes them all in the link command. Missing archives cause an explicit build failure rather than a silent omission.

---

### 6. `SX1302Radio.send()` returning `True` causes `AttributeError` in engine *(fixed in dev_merge)*

**Impact**: Every retransmitted packet triggers:
```
AttributeError: 'bool' object has no attribute 'get'
  File "repeater/engine.py", line 183, in __call__
    lbt_attempts = tx_metadata.get('lbt_attempts', 0)
```

**Root cause**: `pymc_core` dispatcher stores the return value of `radio.send()` as `packet._tx_metadata` if truthy. SX1302's `send()` was returning `True` on success. `engine.py` then attempted to call `.get()` on a bool when extracting LBT metadata.

**Fix applied**: `SX1302Radio.send()` now returns `None` on success. SX1302 has no LBT subsystem — returning `None` correctly signals no LBT metadata, causing the dispatcher to leave `_tx_metadata` unset and `engine.py` to skip the LBT block cleanly.
