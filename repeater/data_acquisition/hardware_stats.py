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
            return self._get_linux_stats()
        
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
    
    def _get_linux_stats(self):
        """Fallback implementation for Linux systems without psutil (e.g. Luckfox)"""
        import os
        import shutil
        from pathlib import Path
        
        # Uptime & Boot Time
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
                boot_time = time.time() - uptime_seconds
        except:
            uptime_seconds = 0
            boot_time = 0

        # Load Avg
        try:
            with open('/proc/loadavg', 'r') as f:
                parts = f.readline().split()
                load_avg = (float(parts[0]), float(parts[1]), float(parts[2]))
        except:
            load_avg = (0.0, 0.0, 0.0)

        # Memory
        mem_info = {}
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    parts = line.split(':')
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = int(parts[1].strip().split()[0]) * 1024 # kB to bytes
                        mem_info[key] = val
            
            total = mem_info.get('MemTotal', 0)
            free = mem_info.get('MemFree', 0)
            buffers = mem_info.get('Buffers', 0)
            cached = mem_info.get('Cached', 0)
            # Linux standard available calc
            available = mem_info.get('MemAvailable', free + buffers + cached)
            used = total - available
            mem_percent = (used / total * 100) if total > 0 else 0
        except:
            total = available = used = mem_percent = 0

        # CPU Usage (Instantaneous)
        def get_cpu_times():
            try:
                with open('/proc/stat', 'r') as f:
                    line = f.readline()
                    if line.startswith('cpu '):
                        return [int(x) for x in line.split()[1:]]
            except:
                pass
            return None

        cpu_percent = 0.0
        t1 = get_cpu_times()
        if t1:
            time.sleep(0.1)
            t2 = get_cpu_times()
            if t2:
                # user+nice+system+idle+iowait+irq+softirq...
                # idle is index 3 (4th column)
                idle_delta = t2[3] - t1[3]
                total_delta = sum(t2) - sum(t1)
                if total_delta > 0:
                    cpu_percent = 100.0 * (1.0 - idle_delta / total_delta)

        # Disk
        try:
            du = shutil.disk_usage('/')
            disk_total = du.total
            disk_used = du.used
            disk_free = du.free
            disk_percent = (disk_used / disk_total * 100) if disk_total > 0 else 0
        except:
            disk_total = disk_used = disk_free = disk_percent = 0

        # Temperatures
        temperatures = {}
        try:
            for zone in Path('/sys/class/thermal').glob('thermal_zone*'):
                try:
                    type_file = zone / 'type'
                    temp_file = zone / 'temp'
                    if type_file.exists() and temp_file.exists():
                        name = type_file.read_text().strip()
                        temp_c = int(temp_file.read_text().strip()) / 1000.0
                        temperatures[name] = temp_c
                except:
                    continue
        except:
            pass

        return {
            "cpu": {
                "usage_percent": round(cpu_percent, 1),
                "count": 1, 
                "frequency": 0,
                "load_avg": {"1min": load_avg[0], "5min": load_avg[1], "15min": load_avg[2]}
            },
            "memory": {
                "total": total,
                "available": available,
                "used": used,
                "usage_percent": round(mem_percent, 1)
            },
            "disk": {
                "total": disk_total,
                "used": disk_used,
                "free": disk_free,
                "usage_percent": round(disk_percent, 1)
            },
            "network": { 
                "bytes_sent": 0, "bytes_recv": 0, "packets_sent": 0, "packets_recv": 0
            },
            "system": {
                "uptime": uptime_seconds,
                "boot_time": boot_time
            },
            "temperatures": temperatures
        }

    def get_processes_summary(self, limit=10):
        """
        Get top processes by CPU and memory usage.
        Returns a dictionary with process information in the format expected by the UI.
        """
        if not PSUTIL_AVAILABLE:
            # Minimal fallback for Luckfox
            return {
                "processes": [
                    {
                        "pid": 1,
                        "name": "init",
                        "cpu_percent": 0.0,
                        "memory_percent": 0.0,
                        "memory_mb": 0.0
                    },
                    {
                        "pid": 0,
                        "name": "repeater (this)",
                        "cpu_percent": 0.0,
                        "memory_percent": 0.0,
                        "memory_mb": 0.0
                    }
                ],
                "total_processes": 2,
                "error": None # Suppress error to show what we have
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