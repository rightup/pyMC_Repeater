import logging
import os
import yaml
from typing import Optional, Dict, Any, List

logger = logging.getLogger("ConfigManager")


class ConfigManager:
    """Manages configuration persistence and live updates to the daemon."""
    
    def __init__(self, config_path: str, config: dict, daemon_instance=None):
        """
        Initialize ConfigManager.
        
        Args:
            config_path: Path to the YAML config file
            config: Reference to the config dictionary
            daemon_instance: Optional reference to the daemon for live updates
        """
        self.config_path = config_path
        self.config = config
        self.daemon = daemon_instance

    def _get_live_radio_snapshot(self) -> Dict[str, Any]:
        radio_cfg = self.config.get("radio", {}) or {}
        return {
            "frequency": int(radio_cfg.get("frequency", 0) or 0),
            "bandwidth": int(radio_cfg.get("bandwidth", 0) or 0),
            "spreading_factor": int(radio_cfg.get("spreading_factor", 0) or 0),
            "coding_rate": int(radio_cfg.get("coding_rate", 0) or 0),
            "tx_power": int(radio_cfg.get("tx_power", 0) or 0),
        }

    def _sync_repeater_handler_radio_config(self, radio_cfg: Dict[str, Any]) -> None:
        repeater_handler = getattr(self.daemon, "repeater_handler", None)
        if not repeater_handler or not hasattr(repeater_handler, "radio_config"):
            return

        if not isinstance(repeater_handler.radio_config, dict):
            repeater_handler.radio_config = {}

        repeater_handler.radio_config.update(
            {
                key: value
                for key, value in radio_cfg.items()
                if value not in (None, 0)
            }
        )

    def _kiss_transport_restart_required(self) -> bool:
        radio = getattr(self.daemon, "radio", None)
        kiss_cfg = self.config.get("kiss", {}) or {}
        if radio is None or not kiss_cfg:
            return False

        runtime_port = getattr(radio, "port", None)
        runtime_baudrate = getattr(radio, "baudrate", None)

        configured_port = kiss_cfg.get("port")
        configured_baudrate = kiss_cfg.get("baud_rate")

        if configured_port and runtime_port and str(configured_port) != str(runtime_port):
            logger.info("KISS port change detected; service restart required")
            return True

        if configured_baudrate and runtime_baudrate and int(configured_baudrate) != int(runtime_baudrate):
            logger.info("KISS baud rate change detected; service restart required")
            return True

        return False

    def _apply_live_radio_config(self) -> bool:
        radio = getattr(self.daemon, "radio", None)
        if radio is None:
            logger.warning("Radio not available for live update")
            return False

        radio_cfg = self._get_live_radio_snapshot()

        try:
            if hasattr(radio, "configure_radio"):
                if hasattr(radio, "radio_config") and isinstance(radio.radio_config, dict):
                    radio.radio_config.update(radio_cfg)

                applied = radio.configure_radio(
                    frequency=radio_cfg["frequency"],
                    bandwidth=radio_cfg["bandwidth"],
                    spreading_factor=radio_cfg["spreading_factor"],
                    coding_rate=radio_cfg["coding_rate"],
                )
                if not applied:
                    logger.warning("Live radio reconfiguration failed")
                    return False
            else:
                current_frequency = getattr(radio, "frequency", None)
                current_bandwidth = getattr(radio, "bandwidth", None)
                current_spreading_factor = getattr(radio, "spreading_factor", None)
                current_coding_rate = getattr(radio, "coding_rate", None)
                current_tx_power = getattr(radio, "tx_power", None)

                if (
                    current_frequency != radio_cfg["frequency"]
                    and hasattr(radio, "set_frequency")
                    and not radio.set_frequency(radio_cfg["frequency"])
                ):
                    return False

                if (
                    current_tx_power != radio_cfg["tx_power"]
                    and hasattr(radio, "set_tx_power")
                    and not radio.set_tx_power(radio_cfg["tx_power"])
                ):
                    return False

                coding_rate_changed = current_coding_rate != radio_cfg["coding_rate"]
                if coding_rate_changed:
                    setattr(radio, "coding_rate", radio_cfg["coding_rate"])

                if current_spreading_factor != radio_cfg["spreading_factor"]:
                    if not hasattr(radio, "set_spreading_factor"):
                        return False
                    if not radio.set_spreading_factor(radio_cfg["spreading_factor"]):
                        return False

                if current_bandwidth != radio_cfg["bandwidth"]:
                    if not hasattr(radio, "set_bandwidth"):
                        return False
                    if not radio.set_bandwidth(radio_cfg["bandwidth"]):
                        return False
                elif coding_rate_changed:
                    if hasattr(radio, "set_bandwidth"):
                        if not radio.set_bandwidth(radio_cfg["bandwidth"]):
                            return False
                    elif hasattr(radio, "set_spreading_factor"):
                        if not radio.set_spreading_factor(radio_cfg["spreading_factor"]):
                            return False
                    else:
                        return False

            self._sync_repeater_handler_radio_config(radio_cfg)
            logger.info("Applied live radio configuration to running daemon")
            return True
        except Exception as e:
            logger.error(f"Failed to apply live radio config: {e}", exc_info=True)
            return False
    
    def save_to_file(self) -> bool:
        """
        Save current config to YAML file.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w') as f:
                # Use safe_dump with explicit width to prevent line wrapping
                # Setting width to a very large number prevents truncation of long strings like identity keys
                yaml.safe_dump(
                    self.config, 
                    f, 
                    default_flow_style=False, 
                    indent=2, 
                    width=1000000,  # Very large width to prevent any line wrapping
                    sort_keys=False,
                    allow_unicode=True
                )
            logger.info(f"Configuration saved to {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save config to {self.config_path}: {e}", exc_info=True)
            return False
    
    def live_update_daemon(self, sections: Optional[List[str]] = None) -> bool:
        """
        Apply configuration changes to the running daemon's in-memory config.
        
        Args:
            sections: List of config sections to update (e.g., ['repeater', 'delays']).
                     If None, updates all common sections.
        
        Returns:
            True if live update was successful, False otherwise
        """
        if not self.daemon or not hasattr(self.daemon, 'config'):
            logger.warning("Daemon not available for live update")
            return False
        
        try:
            daemon_config = self.daemon.config
            live_update_ok = True
            
            # Default sections to update if not specified
            if sections is None:
                sections = ['repeater', 'delays', 'radio', 'acl', 'identities', 'glass']
            
            # Update each section
            for section in sections:
                if section in self.config:
                    if section not in daemon_config:
                        daemon_config[section] = {}
                    
                    # Deep copy the section to avoid reference issues
                    if isinstance(self.config[section], dict):
                        daemon_config[section].update(self.config[section])
                    else:
                        daemon_config[section] = self.config[section]
                    
                    logger.debug(f"Live updated daemon config section: {section}")
            
            logger.info(f"Live updated daemon config sections: {', '.join(sections)}")
            
            # Also reload runtime config in RepeaterHandler if delays or repeater sections changed
            if self.daemon and hasattr(self.daemon, 'repeater_handler'):
                if any(s in ['delays', 'repeater'] for s in sections):
                    if hasattr(self.daemon.repeater_handler, 'reload_runtime_config'):
                        self.daemon.repeater_handler.reload_runtime_config()
                        logger.info("Reloaded RepeaterHandler runtime config")
            
            # Also reload advert_helper config if repeater section changed
            if self.daemon and hasattr(self.daemon, 'advert_helper') and self.daemon.advert_helper:
                if 'repeater' in sections:
                    if hasattr(self.daemon.advert_helper, 'reload_config'):
                        self.daemon.advert_helper.reload_config()
                        logger.info("Reloaded AdvertHelper config")

            # Re-apply dispatcher path hash mode when mesh section changed
            if 'mesh' in sections and self.daemon and hasattr(self.daemon, 'dispatcher'):
                mesh_cfg = self.daemon.config.get("mesh", {})
                path_hash_mode = mesh_cfg.get("path_hash_mode", 0)
                if path_hash_mode not in (0, 1, 2):
                    logger.warning(
                        f"Invalid mesh.path_hash_mode={path_hash_mode}, must be 0/1/2; using 0"
                    )
                    path_hash_mode = 0
                self.daemon.dispatcher.set_default_path_hash_mode(path_hash_mode)
                logger.info(f"Reloaded path hash mode: mesh.path_hash_mode={path_hash_mode}")

            if 'radio_type' in sections:
                logger.info("radio_type change detected; service restart required")
                live_update_ok = False

            if 'kiss' in sections and self._kiss_transport_restart_required():
                live_update_ok = False

            if 'radio' in sections:
                live_update_ok = self._apply_live_radio_config() and live_update_ok
            
            return live_update_ok
            
        except Exception as e:
            logger.error(f"Failed to live update daemon config: {e}", exc_info=True)
            return False
    
    def update_and_save(self, 
                       updates: Dict[str, Any], 
                       live_update: bool = True,
                       live_update_sections: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Apply updates to config, save to file, and optionally live update daemon.
        
        This is the main method that should be used by both mesh_cli and api_endpoints.
        
        Args:
            updates: Dictionary of config updates in nested format.
                    Example: {"repeater": {"node_name": "NewName"}, "delays": {"tx_delay_factor": 1.5}}
            live_update: Whether to apply changes to running daemon immediately
            live_update_sections: Specific sections to live update. If None, auto-detects from updates.
        
        Returns:
            Dict with keys:
                - success: bool - Whether operation succeeded
                - saved: bool - Whether config was saved to file
                - live_updated: bool - Whether daemon was live updated
                - error: str (optional) - Error message if failed
        """
        result: Dict[str, Any] = {
            "success": False,
            "saved": False,
            "live_updated": False
        }
        
        try:
            # Apply updates to config
            for section, values in updates.items():
                if section not in self.config:
                    self.config[section] = {}
                
                if isinstance(values, dict):
                    self.config[section].update(values)
                else:
                    self.config[section] = values
            
            # Save to file
            result["saved"] = self.save_to_file()
            
            if not result["saved"]:
                result["error"] = "Failed to save config to file"
                return result
            
            # Live update daemon if requested
            if live_update:
                # Auto-detect sections if not specified
                if live_update_sections is None:
                    live_update_sections = list(updates.keys())
                
                result["live_updated"] = self.live_update_daemon(live_update_sections)
            
            result["success"] = result["saved"]
            return result
            
        except Exception as e:
            logger.error(f"Error in update_and_save: {e}", exc_info=True)
            result["error"] = str(e)
            return result
    
    def update_nested(self, path: str, value: Any, live_update: bool = True) -> Dict[str, Any]:
        """
        Update a nested config value using dot notation.
        
        Convenience method for simple updates like "repeater.node_name" = "NewName"
        
        Args:
            path: Dot-separated path to config value (e.g., "repeater.node_name")
            value: Value to set
            live_update: Whether to apply changes to running daemon
        
        Returns:
            Result dict from update_and_save
        """
        parts = path.split('.')
        
        if len(parts) == 1:
            # Top-level key
            updates = {parts[0]: value}
        elif len(parts) == 2:
            # Nested one level (most common case)
            updates = {parts[0]: {parts[1]: value}}
        else:
            # Build nested dict for deeper paths
            updates = {}
            current = updates
            for i, part in enumerate(parts[:-1]):
                if i == 0:
                    current[part] = {}
                    current = current[part]
                else:
                    current[part] = {}
                    current = current[part]
            current[parts[-1]] = value
        
        # Determine which section to live update
        section = parts[0]
        
        return self.update_and_save(
            updates=updates,
            live_update=live_update,
            live_update_sections=[section] if live_update else None
        )
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get status information about the ConfigManager.
        
        Returns:
            Dict with config file path, existence, daemon availability
        """
        return {
            "config_path": self.config_path,
            "config_exists": os.path.exists(self.config_path),
            "daemon_available": self.daemon is not None and hasattr(self.daemon, 'config'),
            "config_sections": list(self.config.keys()) if self.config else []
        }
