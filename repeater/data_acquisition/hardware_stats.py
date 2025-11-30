"""
Hardware statistics collection using psutil.
KISS - Keep It Simple Stupid approach.
"""

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    psutil = None

import time
import logging

logger = logging.getLogger("HardwareStats")


class HardwareStatsCollector:
    
    def __init__(self):

        self.start_time = time.time()
    
    def get_stats(self):

        if not PSUTIL_AVAILABLE:
            logger.error("psutil not available - cannot collect hardware stats")
            return {
                "error": "psutil library not available - cannot collect hardware statistics"
            }
        
        try:
            # Get current timestamp
            now = time.time()
            uptime = now - self.start_time
            
            # CPU stats
            cpu_percent = psutil.cpu_percent(interval=0.1)
            cpu_count = psutil.cpu_count()
            cpu_freq = psutil.cpu_freq()
            
            # Memory stats
            memory = psutil.virtual_memory()
            
            # Disk stats
            disk = psutil.disk_usage('/')
            
            # Network stats (total across all interfaces)
            net_io = psutil.net_io_counters()
            
            # Load average (Unix only)
            load_avg = None
            try:
                load_avg = psutil.getloadavg()
            except (AttributeError, OSError):
                # Not available on all systems - use zeros
                load_avg = (0.0, 0.0, 0.0)
            
            # System boot time
            boot_time = psutil.boot_time()
            system_uptime = now - boot_time
            
            # Temperature (if available)
            temperatures = {}
            try:
                temps = psutil.sensors_temperatures()
                for name, entries in temps.items():
                    for i, entry in enumerate(entries):
                        temp_name = f"{name}_{i}" if len(entries) > 1 else name
                        temperatures[temp_name] = entry.current
            except (AttributeError, OSError):
                # Temperature sensors not available
                pass
            
            # Format data structure to match Vue component expectations
            stats = {
                "cpu": {
                    "usage_percent": cpu_percent,
                    "count": cpu_count,
                    "frequency": cpu_freq.current if cpu_freq else 0,
                    "load_avg": {
                        "1min": load_avg[0],
                        "5min": load_avg[1], 
                        "15min": load_avg[2]
                    }
                },
                "memory": {
                    "total": memory.total,
                    "available": memory.available,
                    "used": memory.used,
                    "usage_percent": memory.percent
                },
                "disk": {
                    "total": disk.total,
                    "used": disk.used,
                    "free": disk.free,
                    "usage_percent": round((disk.used / disk.total) * 100, 1)
                },
                "network": {
                    "bytes_sent": net_io.bytes_sent,
                    "bytes_recv": net_io.bytes_recv,
                    "packets_sent": net_io.packets_sent,
                    "packets_recv": net_io.packets_recv
                },
                "system": {
                    "uptime": system_uptime,
                    "boot_time": boot_time
                }
            }
            
            # Add temperatures if available
            if temperatures:
                stats["temperatures"] = temperatures
            
            return stats
            
        except Exception as e:
            logger.error(f"Error collecting hardware stats: {e}")
            return {
                "error": str(e)
            }
    
    def get_processes_summary(self, limit=10):
        """
        Get top processes by CPU and memory usage.
        Returns a dictionary with process information in the format expected by the UI.
        """
        if not PSUTIL_AVAILABLE:
            logger.error("psutil not available - cannot collect process stats")
            return {
                "processes": [],
                "total_processes": 0,
                "error": "psutil library not available - cannot collect process statistics"
            }
        
        try:
            processes = []
            
            # Get all processes
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info']):
                try:
                    pinfo = proc.info
                    # Calculate memory in MB
                    memory_mb = 0
                    if pinfo['memory_info']:
                        memory_mb = pinfo['memory_info'].rss / 1024 / 1024  # RSS in MB
                    
                    process_data = {
                        "pid": pinfo['pid'],
                        "name": pinfo['name'] or 'Unknown',
                        "cpu_percent": pinfo['cpu_percent'] or 0.0,
                        "memory_percent": pinfo['memory_percent'] or 0.0,
                        "memory_mb": round(memory_mb, 1)
                    }
                    processes.append(process_data)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            # Sort by CPU usage and get top processes
            top_processes = sorted(processes, key=lambda x: x['cpu_percent'], reverse=True)[:limit]
            
            return {
                "processes": top_processes,
                "total_processes": len(processes)
            }
            
        except Exception as e:
            logger.error(f"Error collecting process stats: {e}")
            return {
                "processes": [],
                "total_processes": 0,
                "error": str(e)
            }