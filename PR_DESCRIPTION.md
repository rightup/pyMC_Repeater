# API Extensions for Web Dashboard Configuration

## Summary
This PR adds HTTP API endpoints to enable web dashboards (like pyMC Console) to read and update repeater configuration without requiring SSH access or service restarts.

## Changes

### 1. Expose additional config values in `/api/stats` (engine.py)
Adds the following fields to the stats API response for MeshCore CLI parity:
- `config.repeater.max_flood_hops` - Max flood hops setting (for `get flood.max`)
- `config.repeater.advert_interval_minutes` - Local advert interval (for `get advert.interval`)  
- `config.delays.rx_delay_base` - RX delay base setting (for `get rxdelay`)

These fields are already present in config.yaml but were not exposed via the stats API.

### 2. Add `POST /api/update_radio_config` endpoint (api_endpoints.py)
New endpoint to update radio and repeater configuration with live in-memory updates.

**Supported settings:**
| Field | Description | Range |
|-------|-------------|-------|
| `tx_power` | TX power in dBm | 2-30 |
| `tx_delay_factor` | TX delay factor | 0.0-5.0 |
| `direct_tx_delay_factor` | Direct TX delay | 0.0-5.0 |
| `rx_delay_base` | RX delay base | >= 0 |
| `node_name` | Node name | non-empty string |
| `latitude` | Latitude | -90 to 90 |
| `longitude` | Longitude | -180 to 180 |
| `max_flood_hops` | Max flood hops | 0-64 |
| `flood_advert_interval_hours` | Flood advert interval | 0 (off) or 3-48 |
| `advert_interval_minutes` | Local advert interval | 0 (off) or 1-10080 |

**Features:**
- Validates all input parameters with appropriate ranges
- Saves to config.yaml for persistence
- Updates daemon's in-memory config for immediate effect (no restart needed for most settings)
- Returns `live_update` status so clients know if restart is required

**Example request:**
```bash
curl -X POST http://localhost:8000/api/update_radio_config \
  -H "Content-Type: application/json" \
  -d '{"tx_power": 20, "advert_interval_minutes": 180}'
```

**Example response:**
```json
{
  "success": true,
  "data": {
    "applied": ["power=20dBm", "advert.interval=180m"],
    "persisted": true,
    "live_update": true,
    "restart_required": false,
    "message": "Settings applied immediately."
  }
}
```

## Testing
- [x] Tested with pyMC Console Terminal
- [x] Verified config.yaml persistence
- [x] Verified live in-memory updates via `/api/stats`
- [x] Tested validation error handling

## Related
- pyMC Console: https://github.com/dmduran12/pymc_console
