"""
Hardware statistics collection using psutil.
KISS - Keep It Simple Stupid approach.
"""

import psutil
import time
import logging

logger = logging.getLogger("HardwareStats")


class HardwareStatsCollector:
    """Simple hardware statistics collector using psutil."""
    
    def __init__(self):
        """Initialize the hardware stats collector."""
        self.start_time = time.time()
    
    def get_stats(self):
        """
        Get current hardware statistics.
        Returns a dictionary with system stats.
        """
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
            swap = psutil.swap_memory()
            
            # Disk stats
            disk = psutil.disk_usage('/')
            
            # Network stats (total across all interfaces)
            net_io = psutil.net_io_counters()
            
            # Temperature (if available)
            temperature = None
            try:
                temps = psutil.sensors_temperatures()
                if 'cpu_thermal' in temps and len(temps['cpu_thermal']) > 0:
                    temperature = temps['cpu_thermal'][0].current
                elif temps:
                    # Fallback to first available temperature sensor
                    first_sensor = next(iter(temps.values()))
                    if first_sensor:
                        temperature = first_sensor[0].current
            except (AttributeError, OSError):
                # Temperature sensors not available
                pass
            
            # Load average (Unix only)
            load_avg = None
            try:
                load_avg = psutil.getloadavg()
            except (AttributeError, OSError):
                # Not available on all systems
                pass
            
            # System boot time
            boot_time = psutil.boot_time()
            system_uptime = now - boot_time
            
            stats = {
                "timestamp": now,
                "uptime_seconds": uptime,
                "system_uptime_seconds": system_uptime,
                
                # CPU
                "cpu": {
                    "percent": cpu_percent,
                    "count": cpu_count,
                    "frequency_mhz": cpu_freq.current if cpu_freq else None,
                    "load_average": list(load_avg) if load_avg else None
                },
                
                # Memory
                "memory": {
                    "total_mb": round(memory.total / 1024 / 1024, 1),
                    "available_mb": round(memory.available / 1024 / 1024, 1),
                    "used_mb": round(memory.used / 1024 / 1024, 1),
                    "percent": memory.percent
                },
                
                # Swap
                "swap": {
                    "total_mb": round(swap.total / 1024 / 1024, 1),
                    "used_mb": round(swap.used / 1024 / 1024, 1),
                    "percent": swap.percent
                },
                
                # Disk
                "disk": {
                    "total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
                    "used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
                    "free_gb": round(disk.free / 1024 / 1024 / 1024, 1),
                    "percent": round((disk.used / disk.total) * 100, 1)
                },
                
                # Network
                "network": {
                    "bytes_sent": net_io.bytes_sent,
                    "bytes_recv": net_io.bytes_recv,
                    "packets_sent": net_io.packets_sent,
                    "packets_recv": net_io.packets_recv,
                    "errors_in": net_io.errin,
                    "errors_out": net_io.errout,
                    "drops_in": net_io.dropin,
                    "drops_out": net_io.dropout
                },
                
                # Temperature
                "temperature_celsius": temperature
            }
            
            return stats
            
        except Exception as e:
            logger.error(f"Error collecting hardware stats: {e}")
            return {
                "timestamp": time.time(),
                "error": str(e)
            }
    
    def get_processes_summary(self, limit=10):
        """
        Get top processes by CPU and memory usage.
        Returns a dictionary with process information.
        """
        try:
            processes = []
            
            # Get all processes
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                try:
                    processes.append(proc.info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            # Sort by CPU usage
            top_cpu = sorted(processes, key=lambda x: x['cpu_percent'] or 0, reverse=True)[:limit]
            
            # Sort by memory usage  
            top_memory = sorted(processes, key=lambda x: x['memory_percent'] or 0, reverse=True)[:limit]
            
            return {
                "top_cpu": top_cpu,
                "top_memory": top_memory,
                "total_processes": len(processes)
            }
            
        except Exception as e:
            logger.error(f"Error collecting process stats: {e}")
            return {
                "error": str(e)
            }