import logging
import time
from typing import Dict, Optional

import jwt

logger = logging.getLogger(__name__)


class JWTHandler:
    def __init__(self, secret: str, expiry_minutes: int = 15):
        self.secret = secret
        self.expiry_minutes = expiry_minutes
    
    def create_jwt(self, username: str, client_id: str) -> str:

        now = int(time.time())
        expiry = now + (self.expiry_minutes * 60)

        payload = {"sub": username, "exp": expiry, "iat": now, "client_id": client_id}

        token = jwt.encode(payload, self.secret, algorithm="HS256")
        logger.info(f"Created JWT for user '{username}' with client_id '{client_id[:8]}...'")
        return token

    def verify_jwt(self, token: str) -> Optional[Dict]:
        try:
            payload = jwt.decode(token, self.secret, algorithms=["HS256"])
            return payload
        except jwt.ExpiredSignatureError:
            logger.warning("JWT token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            return None
