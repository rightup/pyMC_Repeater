import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import rrdtool
    RRDTOOL_AVAILABLE = True
except ImportError:
    RRDTOOL_AVAILABLE = False

logger = logging.getLogger("RRDToolHandler")


class RRDToolHandler:
    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.rrd_path = self.storage_dir / "metrics.rrd"
        self.available = RRDTOOL_AVAILABLE
        self._init_rrd()

    def _init_rrd(self):
        if not self.available:
            logger.warning("RRDTool not available - skipping RRD initialization")
            return
            
        if self.rrd_path.exists():
            logger.info(f"RRD database exists: {self.rrd_path}")
            return
            
        try:
            rrdtool.create(
                str(self.rrd_path),
                "--step", "60",
                "--start", str(int(time.time() - 60)),
                
                "DS:rx_count:COUNTER:120:0:U",
                "DS:tx_count:COUNTER:120:0:U",
                "DS:drop_count:COUNTER:120:0:U",
                "DS:avg_rssi:GAUGE:120:-200:0",
                "DS:avg_snr:GAUGE:120:-30:30",
                "DS:avg_length:GAUGE:120:0:256",
                "DS:avg_score:GAUGE:120:0:1",
                "DS:neighbor_count:GAUGE:120:0:U",
                
                "DS:type_0:COUNTER:120:0:U",
                "DS:type_1:COUNTER:120:0:U",
                "DS:type_2:COUNTER:120:0:U",
                "DS:type_3:COUNTER:120:0:U",
                "DS:type_4:COUNTER:120:0:U",
                "DS:type_5:COUNTER:120:0:U",
                "DS:type_6:COUNTER:120:0:U",
                "DS:type_7:COUNTER:120:0:U",
                "DS:type_8:COUNTER:120:0:U",
                "DS:type_9:COUNTER:120:0:U",
                "DS:type_10:COUNTER:120:0:U",
                "DS:type_11:COUNTER:120:0:U",
                "DS:type_12:COUNTER:120:0:U",
                "DS:type_13:COUNTER:120:0:U",
                "DS:type_14:COUNTER:120:0:U",
                "DS:type_15:COUNTER:120:0:U",
                "DS:type_other:COUNTER:120:0:U",
                
                "RRA:AVERAGE:0.5:1:10080",
                "RRA:AVERAGE:0.5:5:8640",
                "RRA:AVERAGE:0.5:60:8760",
                "RRA:MAX:0.5:1:10080",
                "RRA:MIN:0.5:1:10080"
            )
            logger.info(f"RRD database created: {self.rrd_path}")
            
        except Exception as e:
            logger.error(f"Failed to create RRD database: {e}")

    def update_packet_metrics(self, record: dict, cumulative_counts: dict):
        if not self.available or not self.rrd_path.exists():
            logger.debug("RRD not available or doesn't exist for packet metrics")
            return
            
        try:
            timestamp = int(record.get("timestamp", time.time()))
            
            try:
                info = rrdtool.info(str(self.rrd_path))
                last_update = int(info.get("last_update", timestamp - 60))
                logger.debug(f"RRD packet update: timestamp={timestamp}, last_update={last_update}")
                if timestamp <= last_update:
                    logger.debug(f"Skipping RRD packet update: timestamp {timestamp} <= last_update {last_update}")
                    return
            except Exception as e:
                logger.debug(f"Failed to get RRD info for packet update: {e}")
            
            rx_total = cumulative_counts.get("rx_total", 0)
            tx_total = cumulative_counts.get("tx_total", 0)
            drop_total = cumulative_counts.get("drop_total", 0)
            type_counts = cumulative_counts.get("type_counts", {})
            
            type_values = []
            for i in range(16):
                type_values.append(str(type_counts.get(f"type_{i}", 0)))
            type_values.append(str(type_counts.get("type_other", 0)))
            
            basic_values = f"{timestamp}:{rx_total}:{tx_total}:{drop_total}:" \
                          f"{record.get('rssi', 'U')}:{record.get('snr', 'U')}:" \
                          f"{record.get('length', 'U')}:{record.get('score', 'U')}:" \
                          f"U"
            
            type_values_str = ":".join(type_values)
            values = f"{basic_values}:{type_values_str}"
            
            logger.debug(f"Updating RRD with packet values: {values}")
            rrdtool.update(str(self.rrd_path), values)
            logger.debug(f"RRD packet update successful")
            
        except Exception as e:
            logger.error(f"Failed to update RRD packet metrics: {e}")
            logger.debug(f"RRD packet update failed - record: {record}")

    def get_data(self, start_time: Optional[int] = None, end_time: Optional[int] = None, 
                 resolution: str = "average") -> Optional[dict]:
        if not self.available or not self.rrd_path.exists():
            logger.error(f"RRD not available: available={self.available}, rrd_path exists={self.rrd_path.exists()}")
            return None
            
        try:
            if end_time is None:
                end_time = int(time.time())
            if start_time is None:
                start_time = end_time - (24 * 3600)
                
            logger.debug(f"RRD fetch: start={start_time}, end={end_time}, resolution={resolution}")
                
            fetch_result = rrdtool.fetch(
                str(self.rrd_path),
                resolution.upper(),
                "--start", str(start_time),
                "--end", str(end_time)
            )
            
            if not fetch_result:
                logger.error("RRD fetch returned None")
                return None
                
            (start, end, step), data_sources, data_points = fetch_result
            logger.debug(f"RRD fetch result: start={start}, end={end}, step={step}, sources={len(data_sources)}, points={len(data_points)}")
            logger.debug(f"Data sources: {data_sources}")
            
            if data_points:
                logger.debug(f"First data point: {data_points[0]}")
                logger.debug(f"Last data point: {data_points[-1]}")
            else:
                logger.warning("No data points returned from RRD fetch")
            
            result = {
                "start_time": start,
                "end_time": end,
                "step": step,
                "data_sources": data_sources,
                "packet_types": {},
                "metrics": {}
            }
            
            timestamps = []
            current_time = start
            
            for ds in data_sources:
                if ds.startswith('type_'):
                    if 'packet_types' not in result:
                        result['packet_types'] = {}
                    result['packet_types'][ds] = []
                else:
                    result['metrics'][ds] = []
            
            for point in data_points:
                timestamps.append(current_time)
                
                for i, value in enumerate(point):
                    ds_name = data_sources[i]
                    if ds_name.startswith('type_'):
                        result['packet_types'][ds_name].append(value)
                    else:
                        result['metrics'][ds_name].append(value)
                        
                current_time += step
            
            result['timestamps'] = timestamps
            logger.debug(f"RRD data processed successfully: {len(timestamps)} timestamps, packet_types keys: {list(result['packet_types'].keys())}")
            
            for type_key in ['type_2', 'type_4', 'type_5']:
                if type_key in result['packet_types']:
                    values = result['packet_types'][type_key]
                    non_none_values = [v for v in values if v is not None]
                    logger.debug(f"{type_key} values: count={len(values)}, non-none={len(non_none_values)}, sample={values[:3] if values else 'empty'}")
                    
            return result
            
        except Exception as e:
            logger.error(f"Failed to get RRD data: {e}")
            return None

    def get_packet_type_stats(self, hours: int = 24) -> Optional[dict]:
        try:
            end_time = int(time.time())
            start_time = end_time - (hours * 3600)
            
            logger.debug(f"Getting packet type stats for {hours} hours from {start_time} to {end_time}")
            
            rrd_data = self.get_data(start_time, end_time)
            if not rrd_data or 'packet_types' not in rrd_data:
                logger.warning(f"No RRD data available")
                return None
            
            logger.debug(f"RRD packet_types keys: {list(rrd_data['packet_types'].keys())}")
            
            type_totals = {}
            packet_type_names = {
                'type_0': 'Request (REQ)',
                'type_1': 'Response (RESPONSE)', 
                'type_2': 'Plain Text Message (TXT_MSG)',
                'type_3': 'Acknowledgment (ACK)',
                'type_4': 'Node Advertisement (ADVERT)',
                'type_5': 'Group Text Message (GRP_TXT)',
                'type_6': 'Group Datagram (GRP_DATA)',
                'type_7': 'Anonymous Request (ANON_REQ)',
                'type_8': 'Returned Path (PATH)',
                'type_9': 'Trace (TRACE)',
                'type_10': 'Multi-part Packet',
                'type_11': 'Reserved Type 11',
                'type_12': 'Reserved Type 12',
                'type_13': 'Reserved Type 13',
                'type_14': 'Reserved Type 14',
                'type_15': 'Custom Packet (RAW_CUSTOM)',
                'type_other': 'Other Types (>15)'
            }
            
            total_valid_points = 0
            for type_key, data_points in rrd_data['packet_types'].items():
                valid_points = [p for p in data_points if p is not None]
                total_valid_points += len(valid_points)
            
            if total_valid_points < 10:
                logger.warning(f"RRD data too sparse ({total_valid_points} valid points)")
                return None
            
            for type_key, data_points in rrd_data['packet_types'].items():
                valid_points = [p for p in data_points if p is not None]
                logger.debug(f"{type_key}: total_points={len(data_points)}, valid_points={len(valid_points)}")
                
                if len(valid_points) >= 2:
                    total = max(valid_points) - min(valid_points)
                    logger.debug(f"{type_key}: min={min(valid_points)}, max={max(valid_points)}, total={total}")
                elif len(valid_points) == 1:
                    total = valid_points[0]
                    logger.debug(f"{type_key}: single value={total}")
                else:
                    total = 0
                    logger.debug(f"{type_key}: no valid values, total=0")
                    
                type_name = packet_type_names.get(type_key, type_key)
                type_totals[type_name] = max(0, total or 0)
            
            logger.debug(f"Final type_totals: {type_totals}")
            
            result = {
                "hours": hours,
                "packet_type_totals": type_totals,
                "total_packets": sum(type_totals.values()),
                "period": f"{hours} hours",
                "data_source": "rrd"
            }
            
            logger.debug(f"Returning packet type stats: {result}")
            return result
            
        except Exception as e:
            logger.error(f"Failed to get packet type stats from RRD: {e}")
            return None