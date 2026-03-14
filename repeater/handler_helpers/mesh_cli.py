import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yaml

logger = logging.getLogger(__name__)


class MeshCLI:

    def __init__(
        self,
        config_path: str,
        config: Dict[str, Any],
        config_manager,  # ConfigManager instance for save & live updates
        identity_type: str = "repeater",
        enable_regions: bool = True,
        send_advert_callback: Optional[Callable] = None,
        identity=None,
        storage_handler=None,
    ):

        self.config_path = Path(config_path)
        self.config = config
        self.config_manager = config_manager
        self.identity_type = identity_type
        self.enable_regions = enable_regions
        self.send_advert_callback = send_advert_callback
        self.identity = identity
        self.storage_handler = storage_handler

        # Get repeater config shortcut
        self.repeater_config = config.get("repeater", {})

    def handle_command(self, sender_pubkey: bytes, command: str, is_admin: bool) -> str:

        # Check admin permission first
        if not is_admin:
            return "Error: Admin permission required"

        logger.debug(f"handle_command received: '{command}' (len={len(command)})")

        # Extract optional sequence prefix (XX|)
        prefix = ""
        if len(command) > 4 and command[2] == "|":
            prefix = command[:3]
            command = command[3:]
            logger.debug(f"Extracted prefix: '{prefix}', remaining command: '{command}'")

        # Strip leading/trailing whitespace
        command = command.strip()
        logger.debug(f"After strip: '{command}'")

        # Route to appropriate handler
        reply = self._route_command(command)

        # Add prefix back to reply if present
        if prefix:
            return prefix + reply
        return reply

    def _route_command(self, command: str) -> str:

        # System commands
        if command == "reboot":
            return self._cmd_reboot()
        elif command == "advert":
            return self._cmd_advert()
        elif command.startswith("clock"):
            return self._cmd_clock(command)
        elif command.startswith("time "):
            return self._cmd_time(command)
        elif command == "start ota":
            return "Error: OTA not supported in Python repeater"
        elif command.startswith("password "):
            return self._cmd_password(command)
        elif command == "clear stats":
            return self._cmd_clear_stats()
        elif command == "ver":
            return self._cmd_version()

        # Get commands
        elif command.startswith("get "):
            return self._cmd_get(command[4:])

        # Set commands
        elif command.startswith("set "):
            return self._cmd_set(command[4:])

        # ACL commands
        elif command.startswith("setperm "):
            return self._cmd_setperm(command)
        elif command == "get acl":
            return "Error: Use 'get acl' via serial console only"

        # Region commands (repeaters only)
        elif command.startswith("region"):
            if self.enable_regions:
                return self._cmd_region(command)
            else:
                return "Error: Region commands not available for room servers"

        # Neighbor commands
        elif command == "neighbors":
            return self._cmd_neighbors()
        elif command.startswith("neighbor.remove "):
            return self._cmd_neighbor_remove(command)

        # Temporary radio params
        elif command.startswith("tempradio "):
            return self._cmd_tempradio(command)

        # Sensor commands
        elif command.startswith("sensor "):
            return "Error: Sensor commands not implemented in Python repeater"

        # GPS commands
        elif command.startswith("gps"):
            return "Error: GPS commands not implemented in Python repeater"

        # Logging commands
        elif command.startswith("log "):
            return self._cmd_log(command)

        # Statistics commands
        elif command.startswith("stats-"):
            return "Error: Stats commands not fully implemented yet"

        else:
            return "Unknown command"

    # ==================== System Commands ====================

    def _cmd_reboot(self) -> str:
        """Reboot the repeater process."""
        from repeater.service_utils import restart_service

        logger.warning("Reboot command received via mesh CLI")
        success, message = restart_service()

        if success:
            return f"OK - {message}"
        else:
            return f"Error: {message}"

    def _cmd_advert(self) -> str:
        """Send self advertisement."""
        if not self.send_advert_callback:
            logger.warning("Advert command received but no callback configured")
            return "Error: Advert functionality not configured"

        try:
            import asyncio

            async def delayed_advert():
                """Delay advert to let CLI response send first (matches C++ 1500ms delay)."""
                await asyncio.sleep(1.5)
                await self.send_advert_callback()

            asyncio.create_task(delayed_advert())
            logger.info("Advert scheduled for sending (1.5s delay)")
            return "OK - Advert sent"
        except Exception as e:
            logger.error(f"Failed to schedule advert: {e}", exc_info=True)
            return f"Error: {e}"

    def _cmd_clock(self, command: str) -> str:
        """Handle clock commands."""
        if command == "clock":
            # Display current time
            import datetime

            dt = datetime.datetime.utcnow()
            return f"{dt.hour:02d}:{dt.minute:02d} - {dt.day}/{dt.month}/{dt.year} UTC"
        elif command == "clock sync":
            # Clock sync happens automatically via sender_timestamp in protocol
            return "OK - clock sync not needed (system time used)"
        else:
            return "Unknown clock command"

    def _cmd_time(self, command: str) -> str:
        """Set time - not supported in Python (use system time)."""
        return "Error: Time setting not supported (system time is used)"

    def _cmd_password(self, command: str) -> str:
        """Change admin password."""
        new_password = command[9:].strip()

        if not new_password:
            return "Error: Password cannot be empty"

        # Update security config
        if "security" not in self.config:
            self.config["security"] = {}

        self.config["security"]["password"] = new_password

        # Save config and live update
        try:
            saved, err = self.config_manager.save_to_file()
            if not saved:
                logger.error(f"Failed to save password: {err}")
                return f"Error: Failed to save config: {err}"
            self.config_manager.live_update_daemon(["security"])
            return f"password now: {new_password}"
        except Exception as e:
            logger.error(f"Failed to save password: {e}")
            return "Error: Failed to save password"

    def _cmd_clear_stats(self) -> str:
        """Clear statistics."""
        # TODO: Implement stats clearing
        return "Error: Not yet implemented"

    def _cmd_version(self) -> str:
        """Get version information."""
        role = "room_server" if self.identity_type == "room_server" else "repeater"
        version = self.config.get("version", "1.0.0")
        return f"pyMC_{role} v{version}"

    # ==================== Get Commands ====================

    def _cmd_get(self, param: str) -> str:
        """Handle get commands."""
        param = param.strip()
        logger.debug(f"_cmd_get called with param: '{param}' (len={len(param)})")

        if param == "af":
            af = self.repeater_config.get("airtime_factor", 1.0)
            return f"> {af}"

        elif param == "name":
            name = self.repeater_config.get("name", "Unknown")
            return f"> {name}"

        elif param == "repeat":
            disabled = self.repeater_config.get("disable_forward", False)
            return f"> {'off' if disabled else 'on'}"

        elif param == "lat":
            lat = self.repeater_config.get("latitude", 0.0)
            return f"> {lat}"

        elif param == "lon":
            lon = self.repeater_config.get("longitude", 0.0)
            return f"> {lon}"

        elif param == "radio":
            radio = self.config.get("radio", {})
            freq_hz = radio.get("frequency", 915000000)
            bw_hz = radio.get("bandwidth", 125000)
            sf = radio.get("spreading_factor", 7)
            cr = radio.get("coding_rate", 5)
            # Convert Hz to MHz for freq, Hz to kHz for bandwidth (match C++ ftoa output)
            freq_mhz = freq_hz / 1_000_000.0
            bw_khz = bw_hz / 1_000.0
            return f"> {freq_mhz},{bw_khz},{sf},{cr}"

        elif param == "freq":
            freq_hz = self.config.get("radio", {}).get("frequency", 915000000)
            freq_mhz = freq_hz / 1_000_000.0
            return f"> {freq_mhz}"

        elif param == "tx":
            power = self.config.get("radio", {}).get("tx_power", 20)
            return f"> {power}"

        elif param == "public.key":
            if not self.identity:
                return "Error: Identity not available"
            try:
                pubkey = self.identity.get_public_key()
                pubkey_hex = pubkey.hex()
                return f"> {pubkey_hex}"
            except Exception as e:
                logger.error(f"Failed to get public key: {e}")
                return f"Error: {e}"

        elif param == "role":
            role = "room_server" if self.identity_type == "room_server" else "repeater"
            return f"> {role}"

        elif param == "guest.password":
            guest_pw = self.config.get("security", {}).get("guest_password", "")
            return f"> {guest_pw}"

        elif param == "allow.read.only":
            allow = self.config.get("security", {}).get("allow_read_only", False)
            return f"> {'on' if allow else 'off'}"

        elif param == "advert.interval":
            interval = self.repeater_config.get("advert_interval_minutes", 120)
            return f"> {interval}"

        elif param == "flood.advert.interval":
            interval = self.repeater_config.get("flood_advert_interval_hours", 24)
            return f"> {interval}"

        elif param == "flood.max":
            max_flood = self.repeater_config.get("max_flood_hops", 3)
            return f"> {max_flood}"

        elif param == "rxdelay":
            delay = self.repeater_config.get("rx_delay_base", 0.0)
            return f"> {delay}"

        elif param == "txdelay":
            delay = self.repeater_config.get("tx_delay_factor", 1.0)
            return f"> {delay}"

        elif param == "direct.txdelay":
            delay = self.repeater_config.get("direct_tx_delay_factor", 0.5)
            return f"> {delay}"

        elif param == "multi.acks":
            acks = self.repeater_config.get("multi_acks", 0)
            return f"> {acks}"

        elif param == "int.thresh":
            thresh = self.repeater_config.get("interference_threshold", -120)
            return f"> {thresh}"

        elif param == "agc.reset.interval":
            interval = self.repeater_config.get("agc_reset_interval", 0)
            return f"> {interval}"

        else:
            return f"??: {param}"

    # ==================== Set Commands ====================

    def _cmd_set(self, param: str) -> str:
        """Handle set commands."""
        parts = param.split(None, 1)
        if len(parts) < 2:
            return "Error: Missing value"

        key, value = parts[0], parts[1]

        try:
            if key == "af":
                self.repeater_config["airtime_factor"] = float(value)
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return "OK"

            elif key == "name":
                self.repeater_config["node_name"] = value
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return "OK"

            elif key == "repeat":
                disabled = value.lower() == "off"
                self.repeater_config["disable_forward"] = disabled
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return f"OK - repeat is now {'OFF' if disabled else 'ON'}"

            elif key == "lat":
                self.repeater_config["latitude"] = float(value)
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return "OK"

            elif key == "lon":
                self.repeater_config["longitude"] = float(value)
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return "OK"

            elif key == "radio":
                # Format: freq bw sf cr
                radio_parts = value.split()
                if len(radio_parts) != 4:
                    return "Error: Expected freq bw sf cr"

                if "radio" not in self.config:
                    self.config["radio"] = {}

                self.config["radio"]["frequency"] = float(radio_parts[0])
                self.config["radio"]["bandwidth"] = float(radio_parts[1])
                self.config["radio"]["spreading_factor"] = int(radio_parts[2])
                self.config["radio"]["coding_rate"] = int(radio_parts[3])
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["radio"])
                return "OK - restart repeater to apply"

            elif key == "freq":
                if "radio" not in self.config:
                    self.config["radio"] = {}
                self.config["radio"]["frequency"] = float(value)
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["radio"])
                return "OK - restart repeater to apply"

            elif key == "tx":
                if "radio" not in self.config:
                    self.config["radio"] = {}
                self.config["radio"]["tx_power"] = int(value)
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["radio"])
                return "OK"

            elif key == "guest.password":
                if "security" not in self.config:
                    self.config["security"] = {}
                self.config["security"]["guest_password"] = value
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["security"])
                return "OK"

            elif key == "allow.read.only":
                if "security" not in self.config:
                    self.config["security"] = {}
                self.config["security"]["allow_read_only"] = value.lower() == "on"
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["security"])
                return "OK"

            elif key == "advert.interval":
                mins = int(value)
                if mins > 0 and (mins < 60 or mins > 240):
                    return "Error: interval range is 60-240 minutes"
                self.repeater_config["advert_interval_minutes"] = mins
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return "OK"

            elif key == "flood.advert.interval":
                hours = int(value)
                if (hours > 0 and hours < 3) or hours > 48:
                    return "Error: interval range is 3-48 hours"
                self.repeater_config["flood_advert_interval_hours"] = hours
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return "OK"

            elif key == "flood.max":
                max_val = int(value)
                if max_val > 64:
                    return "Error: max 64"
                self.repeater_config["max_flood_hops"] = max_val
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return "OK"

            elif key == "rxdelay":
                delay = float(value)
                if delay < 0:
                    return "Error: cannot be negative"
                self.repeater_config["rx_delay_base"] = delay
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater", "delays"])
                return "OK"

            elif key == "txdelay":
                delay = float(value)
                if delay < 0:
                    return "Error: cannot be negative"
                self.repeater_config["tx_delay_factor"] = delay
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater", "delays"])
                return "OK"

            elif key == "direct.txdelay":
                delay = float(value)
                if delay < 0:
                    return "Error: cannot be negative"
                self.repeater_config["direct_tx_delay_factor"] = delay
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater", "delays"])
                return "OK"

            elif key == "multi.acks":
                self.repeater_config["multi_acks"] = int(value)
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return "OK"

            elif key == "int.thresh":
                self.repeater_config["interference_threshold"] = int(value)
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return "OK"

            elif key == "agc.reset.interval":
                interval = int(value)
                # Round to nearest multiple of 4
                rounded = (interval // 4) * 4
                self.repeater_config["agc_reset_interval"] = rounded
                saved, _ = self.config_manager.save_to_file()
                self.config_manager.live_update_daemon(["repeater"])
                return f"OK - interval rounded to {rounded}"

            else:
                return f"unknown config: {key}"

        except ValueError as e:
            return f"Error: invalid value - {e}"
        except Exception as e:
            logger.error(f"Set command error: {e}")
            return f"Error: {e}"

    # ==================== ACL Commands ====================

    def _cmd_setperm(self, command: str) -> str:
        """Set permissions for a public key."""
        # Format: setperm {pubkey-hex} {permissions-int}
        parts = command[8:].split()
        if len(parts) < 2:
            return "Err - bad params"

        pubkey_hex = parts[0]
        try:
            permissions = int(parts[1])
        except ValueError:
            return "Err - invalid permissions"

        # TODO: Apply permissions via ACL
        logger.info(f"setperm command: {pubkey_hex} -> {permissions}")
        return "Error: Not yet implemented - use config file"

    # ==================== Region Commands ====================

    def _cmd_region(self, command: str) -> str:
        """Handle region commands."""
        parts = command.split()

        if len(parts) == 1:
            return "Error: Region commands not implemented in Python repeater"

        subcommand = parts[1]

        if subcommand == "load":
            return "Error: Region commands not implemented"
        elif subcommand == "save":
            return "Error: Region commands not implemented"
        elif subcommand in ("allowf", "denyf", "get", "home", "put", "remove"):
            return "Error: Region commands not implemented"
        else:
            return "Err - ??"

    # ==================== Neighbor Commands ====================

    def _cmd_neighbors(self) -> str:
        """List neighbors."""
        if not self.storage_handler:
            return "Error: Storage not available"

        try:
            neighbors = self.storage_handler.get_neighbors()

            if not neighbors:
                return "No neighbors discovered yet"

            # Filter to only show repeaters and zero hop nodes
            filtered_neighbors = {
                pubkey: info
                for pubkey, info in neighbors.items()
                if info.get("is_repeater", False) or info.get("zero_hop", False)
            }

            if not filtered_neighbors:
                return "No repeaters or zero hop neighbors discovered yet"

            # Format output similar to C++ version
            # Format: "<pubkey_prefix> heard Xs ago"
            import time

            current_time = int(time.time())

            lines = []
            for pubkey, info in filtered_neighbors.items():
                last_seen = info.get("last_seen", 0)
                seconds_ago = int(current_time - last_seen)

                # Get first 4 bytes of pubkey as hex (match C++ format)
                pubkey_short = pubkey[:8] if len(pubkey) >= 8 else pubkey
                snr = info.get("snr", 0) or 0

                # Format: <4byte_hex>:<seconds_ago>:<snr> (matches C++ format)
                lines.append(f"{pubkey_short}:{seconds_ago}:{int(snr)}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Failed to list neighbors: {e}", exc_info=True)
            return f"Error: {e}"

    def _cmd_neighbor_remove(self, command: str) -> str:
        """Remove a neighbor."""
        pubkey_hex = command[16:].strip()

        if not pubkey_hex:
            return "ERR: Missing pubkey"

        # TODO: Remove neighbor from routing table
        logger.info(f"neighbor.remove: {pubkey_hex}")
        return "Error: Not yet implemented"

    # ==================== Temporary Radio Commands ====================

    def _cmd_tempradio(self, command: str) -> str:
        """Apply temporary radio parameters."""
        # Format: tempradio {freq} {bw} {sf} {cr} {timeout_mins}
        parts = command[10:].split()

        if len(parts) < 5:
            return "Error: Expected freq bw sf cr timeout_mins"

        try:
            freq = float(parts[0])
            bw = float(parts[1])
            sf = int(parts[2])
            cr = int(parts[3])
            timeout_mins = int(parts[4])

            # Validate
            if not (300.0 <= freq <= 2500.0):
                return "Error: invalid frequency"
            if not (7.0 <= bw <= 500.0):
                return "Error: invalid bandwidth"
            if not (5 <= sf <= 12):
                return "Error: invalid spreading factor"
            if not (5 <= cr <= 8):
                return "Error: invalid coding rate"
            if timeout_mins <= 0:
                return "Error: invalid timeout"

            # TODO: Apply temporary radio parameters
            logger.info(f"tempradio: {freq}MHz {bw}kHz SF{sf} CR4/{cr} for {timeout_mins}min")
            return "Error: Not yet implemented"

        except ValueError:
            return "Error, invalid params"

    # ==================== Logging Commands ====================

    def _cmd_log(self, command: str) -> str:
        """Handle log commands."""
        if command == "log start":
            # TODO: Enable logging
            return "Error: Not yet implemented"
        elif command == "log stop":
            # TODO: Disable logging
            return "Error: Not yet implemented"
        elif command == "log erase":
            # TODO: Clear log file
            return "Error: Not yet implemented"
        elif command == "log":
            return "Error: Use journalctl to view logs"
        else:
            return "Unknown log command"
