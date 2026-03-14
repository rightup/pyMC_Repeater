import hashlib
import hmac
import logging
import secrets
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class APITokenManager:
    def __init__(self, sqlite_handler, secret_key: str):

        self.db = sqlite_handler
        self.secret_key = secret_key.encode("utf-8")

    def generate_api_token(self) -> str:
        return secrets.token_hex(32)

    def hash_token(self, token: str) -> str:
        return hmac.new(self.secret_key, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def create_token(self, name: str) -> tuple[int, str]:
        plaintext_token = self.generate_api_token()
        token_hash = self.hash_token(plaintext_token)
        
        token_id = self.db.create_api_token(name, token_hash)
        
        logger.info(f"Created API token '{name}' with ID {token_id}")
        return token_id, plaintext_token
    
    def verify_token(self, token: str) -> Optional[Dict]:
        token_hash = self.hash_token(token)
        return self.db.verify_api_token(token_hash)
    
    def revoke_token(self, token_id: int) -> bool:
        deleted = self.db.revoke_api_token(token_id)
        
        if deleted:
            logger.info(f"Revoked API token ID {token_id}")
        
        return deleted

    def list_tokens(self) -> List[Dict]:
        return self.db.list_api_tokens()
