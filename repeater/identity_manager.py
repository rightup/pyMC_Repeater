import logging
from typing import Dict, Optional, Tuple, Any

logger = logging.getLogger("IdentityManager")


class IdentityManager:
   
    def __init__(self, config: dict):
        self.config = config
        self.identities: Dict[int, Tuple[Any, dict, str]] = {}
        self.named_identities: Dict[str, Tuple[Any, dict, str]] = {}
        self.registered_hashes: Dict[int, str] = {}
    
    def register_identity(self, name: str, identity, config: dict, identity_type: str):
        hash_byte = identity.get_public_key()[0]
        
        if hash_byte in self.identities:
            existing_name = self.registered_hashes.get(hash_byte, "unknown")
            logger.error(
                f"Hash collision! Identity '{name}' (hash=0x{hash_byte:02X}) "
                f"conflicts with existing identity '{existing_name}'"
            )
            return False
        
        self.identities[hash_byte] = (identity, config, identity_type)
        self.named_identities[name] = (identity, config, identity_type)
        self.registered_hashes[hash_byte] = f"{identity_type}:{name}"
        
        logger.info(
            f"Identity registered: name={name}, hash=0x{hash_byte:02X}, type={identity_type}"
        )
        return True
    
    def get_identity_by_hash(self, hash_byte: int) -> Optional[Tuple[Any, dict, str]]:
        return self.identities.get(hash_byte)
    
    def get_identity_by_name(self, name: str) -> Optional[Tuple[Any, dict, str]]:
        return self.named_identities.get(name)
    
    def has_identity(self, hash_byte: int) -> bool:
        return hash_byte in self.identities
    
    def list_identities(self) -> list:
        identities = []
        for hash_byte, (identity, config, id_type) in self.identities.items():
            name = self.registered_hashes.get(hash_byte, "unknown")
            identities.append({
                "hash": f"0x{hash_byte:02X}",
                "name": name,
                "type": id_type,
                "address": identity.get_address_bytes().hex() if identity else "N/A"
            })
        return identities
    
    def has_identity_type(self, identity_type: str) -> bool:
        return any(id_type == identity_type for _, _, id_type in self.identities.values())
    
    def get_identities_by_type(self, identity_type: str) -> list:
        results = []
        for name, (identity, config, id_type) in self.named_identities.items():
            if id_type == identity_type:
                results.append((name, identity, config))
        return results
