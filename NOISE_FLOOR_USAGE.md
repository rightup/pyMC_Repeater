# Noise Floor Measurement - Usage Guide

## Overview
The noise floor measurement capability has been added to provide real-time RF environment monitoring.

## Implementation

### 1. Radio Wrapper (pyMC_core)
Added `get_noise_floor()` method to `SX1262Radio` class:

```python
def get_noise_floor(self) -> Optional[float]:
    """
    Get current noise floor (instantaneous RSSI) in dBm.
    Returns None if radio is not initialized or if reading fails.
    """
```

### 2. Repeater Engine (pyMC_Repeater)
Added `get_noise_floor()` method to `RepeaterHandler` class:

```python
def get_noise_floor(self) -> Optional[float]:
    """
    Get the current noise floor (instantaneous RSSI) from the radio in dBm.
    Returns None if radio is not available or reading fails.
    """
```

The noise floor is automatically included in the stats dictionary returned by `get_stats()`:

```python
stats = handler.get_stats()
noise_floor = stats.get('noise_floor_dbm')  # Returns float or None
```

## Usage Examples

### Example 1: Get Noise Floor Directly
```python
# From the repeater engine
handler = RepeaterHandler(config, dispatcher, local_hash)
noise_floor_dbm = handler.get_noise_floor()

if noise_floor_dbm is not None:
    print(f"Current noise floor: {noise_floor_dbm:.1f} dBm")
else:
    print("Noise floor not available")
```

### Example 2: Access via Stats
```python
# Get all stats including noise floor
stats = handler.get_stats()
noise_floor = stats.get('noise_floor_dbm')

if noise_floor is not None:
    print(f"RF Environment: {noise_floor:.1f} dBm")
```

### Example 3: Monitor RF Environment
```python
import asyncio

async def monitor_rf_environment(handler, interval=5.0):
    """Monitor noise floor every N seconds"""
    while True:
        noise_floor = handler.get_noise_floor()
        if noise_floor is not None:
            if noise_floor > -100:
                print(f"⚠️  High RF noise: {noise_floor:.1f} dBm")
            else:
                print(f"✓ Normal RF environment: {noise_floor:.1f} dBm")
        await asyncio.sleep(interval)
```

### Example 4: Channel Assessment Before TX
```python
async def should_transmit(handler, threshold_dbm=-110):
    """
    Check if channel is clear before transmitting.
    Returns True if noise floor is below threshold (channel clear).
    """
    noise_floor = handler.get_noise_floor()
    
    if noise_floor is None:
        # Can't determine, allow transmission
        return True
    
    if noise_floor > threshold_dbm:
        # Channel busy - high noise
        print(f"Channel busy: {noise_floor:.1f} dBm > {threshold_dbm} dBm")
        return False
    
    # Channel clear
    return True
```

## Integration with Web Dashboard

The noise floor is automatically available in the `/api/stats` endpoint:

```javascript
// JavaScript example for web dashboard
fetch('/api/stats')
    .then(response => response.json())
    .then(data => {
        const noiseFloor = data.noise_floor_dbm;
        if (noiseFloor !== null) {
            updateNoiseFloorDisplay(noiseFloor);
        }
    });
```

## Interpretation

### Typical Values
- **-120 to -110 dBm**: Very quiet RF environment (rural, low interference)
- **-110 to -100 dBm**: Normal RF environment (typical conditions)
- **-100 to -90 dBm**: Moderate RF noise (urban, some interference)
- **-90 dBm and above**: High RF noise (congested environment, potential issues)

### Use Cases
1. **Collision Avoidance**: Check noise floor before transmitting to detect if another station is already transmitting
2. **RF Environment Monitoring**: Track RF noise levels over time for site assessment
3. **Adaptive Transmission**: Adjust TX timing or power based on channel conditions
4. **Debugging**: Identify sources of interference or poor reception

## Technical Details

### Calculation
The noise floor is calculated from the SX1262's instantaneous RSSI register:
```python
raw_rssi = self.lora.getRssiInst()
noise_floor_dbm = -(float(raw_rssi) / 2)
```

### Update Rate
The noise floor is read on-demand when `get_noise_floor()` is called. There is no caching - each call queries the radio hardware directly.

### Error Handling
- Returns `None` if radio is not initialized
- Returns `None` if read fails (hardware error)
- Logs debug message on error (doesn't raise exceptions)

## Future Enhancements

Potential future improvements:
1. **Averaging**: Average noise floor over multiple samples for stability
2. **History**: Track noise floor history for trend analysis
3. **Thresholds**: Configurable thresholds for channel busy detection
4. **Carrier Sense**: Automatic carrier sense before each transmission
5. **Spectral Analysis**: Extended to include RSSI across multiple channels
