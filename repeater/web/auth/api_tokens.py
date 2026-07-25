import hashlib
import hmac
import logging
import secrets
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_TOKEN_NAME_DISPLAY_MAX_BYTES = 1024


def safe_api_token_name(value: object) -> str:
    """Return bounded, printable metadata without invalidating a legacy token."""

    raw = value if isinstance(value, str) else str(value)
    rendered = []
    rendered_bytes = 0
    truncated = False
    for character in raw:
        if character.isprintable():
            piece = character
        else:
            codepoint = ord(character)
            piece = f"\\u{codepoint:04x}" if codepoint <= 0xFFFF else f"\\U{codepoint:08x}"
        piece_bytes = len(piece.encode("utf-8"))
        if rendered_bytes + piece_bytes > _TOKEN_NAME_DISPLAY_MAX_BYTES:
            truncated = True
            break
        rendered.append(piece)
        rendered_bytes += piece_bytes
    result = "".join(rendered) or "<unnamed>"
    if not truncated:
        return result

    suffix = "…"
    suffix_bytes = len(suffix.encode("utf-8"))
    while rendered and rendered_bytes + suffix_bytes > _TOKEN_NAME_DISPLAY_MAX_BYTES:
        removed = rendered.pop()
        rendered_bytes -= len(removed.encode("utf-8"))
    return f"{''.join(rendered) or '<unnamed>'}{suffix}"


def _safe_token_info(token_info: Dict) -> Dict:
    safe = dict(token_info)
    safe["name"] = safe_api_token_name(token_info.get("name"))
    return safe


class APITokenManager:
    def __init__(self, sqlite_handler, secret_key: str):

        self.db = sqlite_handler
        self.secret_key = secret_key.encode("utf-8")

    def generate_api_token(self) -> str:
        return secrets.token_hex(32)

    def hash_token(self, token: str) -> str:
        return hmac.new(self.secret_key, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def create_token(self, name: str, scope: Optional[str] = None) -> tuple[int, str]:
        plaintext_token = self.generate_api_token()
        token_hash = self.hash_token(plaintext_token)

        token_id = self.db.create_api_token_strict(name, token_hash, scope=scope)

        logger.info("Created API token %r with ID %s", safe_api_token_name(name), token_id)
        return token_id, plaintext_token

    def verify_token(self, token: str) -> Optional[Dict]:
        token_hash = self.hash_token(token)
        token_info = self.db.verify_api_token_strict(token_hash)
        return None if token_info is None else _safe_token_info(token_info)

    def get_token(self, token_id: int) -> Optional[Dict]:
        """Read active token metadata by ID without retaining its credential."""

        token_info = self.db.get_api_token_by_id_strict(token_id)
        return None if token_info is None else _safe_token_info(token_info)

    def revoke_token(self, token_id: int) -> bool:
        deleted = self.db.revoke_api_token_strict(token_id)

        if deleted:
            logger.info(f"Revoked API token ID {token_id}")

        return deleted

    def list_tokens(self) -> List[Dict]:
        return [_safe_token_info(token_info) for token_info in self.db.list_api_tokens_strict()]
