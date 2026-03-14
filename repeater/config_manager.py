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
            
            # Default sections to update if not specified
            if sections is None:
                sections = ['repeater', 'delays', 'radio', 'acl', 'identities']
            
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
            
            return True
            
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
        result = {
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
