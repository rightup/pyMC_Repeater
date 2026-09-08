from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Optional

from openhop_core.protocol.constants import PAYLOAD_TYPE_GRP_DATA, PAYLOAD_TYPE_GRP_TXT
from openhop_core.protocol.crypto import CryptoUtils

logger = logging.getLogger("PolicyEngine")


SUPPORTED_ACTIONS = {
    "allow",
    "drop",
    "log_only",
}


def default_policy_engine_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "default_action": "allow",
        "rules": [],
        "objects": {},
    }


@dataclass
class PolicyDecision:
    action: str = "allow"
    matched: bool = False
    rule_id: Optional[Any] = None
    reason: Optional[str] = None


class PolicyEngine:
    """Readable top-down rule evaluator for repeater policy decisions."""

    def __init__(self, policy_config: Optional[dict] = None):
        cfg = default_policy_engine_config()
        if isinstance(policy_config, dict):
            cfg.update(policy_config)

        self.enabled = bool(cfg.get("enabled", False))
        self.default_action = str(cfg.get("default_action", "allow"))
        if self.default_action not in SUPPORTED_ACTIONS:
            logger.warning(
                "Policy default_action '%s' is not supported, using 'allow'",
                self.default_action,
            )
            self.default_action = "allow"

        rules = cfg.get("rules")
        self.rules: list[dict[str, Any]] = rules if isinstance(rules, list) else []

        objects = cfg.get("objects")
        self.objects: dict[str, Any] = objects if isinstance(objects, dict) else {}
        self._channel_decrypt_cache: dict[str, dict[str, Any]] = {}
        self._inline_channel_secrets = self._collect_inline_rule_channel_secrets(self.rules)

    @classmethod
    def from_runtime_config(cls, runtime_config: Optional[dict]) -> "PolicyEngine":
        if not isinstance(runtime_config, dict):
            return cls()
        return cls(runtime_config.get("policy_engine", {}))

    def evaluate(self, packet, context: dict) -> PolicyDecision:
        if not self.enabled:
            return PolicyDecision(action="allow", matched=False, reason="policy_disabled")

        for rule in self.rules:
            if not isinstance(rule, dict):
                continue
            if not bool(rule.get("enabled", True)):
                continue

            if not self._rule_matches(rule, packet, context):
                continue

            action = self._resolve_action(rule)
            rule_id = rule.get("id")
            rule_name = rule.get("name") or "unnamed"
            reason = f"Policy rule matched: id={rule_id}, name={rule_name}, action={action}"
            return PolicyDecision(action=action, matched=True, rule_id=rule_id, reason=reason)

        return PolicyDecision(action=self.default_action, matched=False, reason="default_action")

    def _resolve_action(self, rule: dict) -> str:
        then_block = rule.get("then", {})
        action = None

        if isinstance(then_block, dict):
            action = then_block.get("action")
        elif isinstance(then_block, str):
            action = then_block

        if not action:
            action = rule.get("action")

        action = str(action or "allow")
        if action not in SUPPORTED_ACTIONS:
            logger.warning("Unsupported policy action '%s', coercing to 'allow'", action)
            return "allow"
        return action

    def _rule_matches(self, rule: dict, packet, context: dict) -> bool:
        cond = rule.get("if", {})

        # Support implicit single-condition form.
        if isinstance(cond, dict) and "field" in cond:
            return self._condition_matches(cond, packet, context)

        if not isinstance(cond, dict):
            return False

        all_conds = cond.get("all")
        any_conds = cond.get("any")

        if isinstance(all_conds, list):
            return all(self._condition_matches(c, packet, context) for c in all_conds)

        if isinstance(any_conds, list):
            return any(self._condition_matches(c, packet, context) for c in any_conds)

        return False

    def _condition_matches(self, condition: dict, packet, context: dict) -> bool:
        if not isinstance(condition, dict):
            return False

        field = condition.get("field")
        op = condition.get("op", "equals")
        try:
            expected = self._resolve_value(condition.get("value"))

            actual = self._get_field_value(field, packet, context)
            if field == "path_hashes":
                actual = self._normalize_path_hash_values(actual)
                expected = self._normalize_path_hash_values(expected)
            if field == "channel_hash":
                actual = self._normalize_channel_hash_values(actual)
                expected = self._normalize_channel_hash_values(expected)
            result = self._compare(actual, op, expected)
            logger.debug(
                "Condition eval: field=%s op=%s expected=%r actual=%r -> %s",
                field,
                op,
                expected,
                actual,
                "MATCH" if result else "no match",
            )
            return result
        except ValueError as exc:
            logger.debug("Condition eval: field=%s raised ValueError: %s -> no match", field, exc)
            return False

    def _resolve_value(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("@"):
            # Object reference format: @group.name
            ref = value[1:]
            parts = ref.split(".", 1)
            if len(parts) == 2:
                group, key = parts
                group_obj = self.objects.get(group, {})
                if isinstance(group_obj, dict):
                    return group_obj.get(key)
        return value

    def _get_field_value(self, field: Any, packet, context: dict) -> Any:
        if not isinstance(field, str):
            return None

        # Existing packet/context fields only.
        if field in context:
            return context.get(field)

        if field == "payload_hex":
            payload = getattr(packet, "payload", None) or b""
            return bytes(payload).hex()

        if field == "channel_hash":
            decrypted = getattr(packet, "decrypted", None)
            if isinstance(decrypted, dict):
                group_text = decrypted.get("group_text_data", {})
                if isinstance(group_text, dict):
                    candidate = group_text.get("channel_hash")
                    if candidate is not None:
                        return candidate

            try:
                payload_type = (
                    packet.get_payload_type() if hasattr(packet, "get_payload_type") else None
                )
            except Exception:
                payload_type = None
            if payload_type in (PAYLOAD_TYPE_GRP_TXT, PAYLOAD_TYPE_GRP_DATA):
                payload = (
                    packet.get_payload()
                    if hasattr(packet, "get_payload")
                    else getattr(packet, "payload", None)
                )
                if payload and len(payload) >= 1:
                    return payload[0]

        if field == "channel_message_body":
            channel_info = self._get_channel_decrypt_info(packet)
            return channel_info.get("message_body")

        if field == "channel_sender":
            channel_info = self._get_channel_decrypt_info(packet)
            return channel_info.get("sender")

        if field == "channel_decryptable":
            channel_info = self._get_channel_decrypt_info(packet)
            return bool(channel_info.get("decryptable", False))

        if field == "path_hashes":
            if hasattr(packet, "get_path_hashes_hex"):
                return packet.get_path_hashes_hex()
            return []

        if field == "transport_code_0":
            if hasattr(packet, "transport_codes") and packet.transport_codes:
                return packet.transport_codes[0]
            return None

        if field == "transport_code_1":
            if hasattr(packet, "transport_codes") and len(packet.transport_codes) > 1:
                return packet.transport_codes[1]
            return None

        return None

    def _extract_channel_message_body(self, packet) -> Optional[str]:
        channel_info = self._compute_channel_decrypt_info(packet)
        return channel_info.get("message_body")

    def _get_channel_decrypt_info(self, packet) -> dict[str, Any]:
        try:
            packet_key = packet.calculate_packet_hash().hex().upper()
        except Exception:
            packet_key = "id-" + str(id(packet))
        cached = self._channel_decrypt_cache.get(packet_key)
        if isinstance(cached, dict):
            return cached

        computed = self._compute_channel_decrypt_info(packet)
        self._channel_decrypt_cache[packet_key] = computed
        return computed

    def _compute_channel_decrypt_info(self, packet) -> dict[str, Any]:
        decrypted = getattr(packet, "decrypted", None)
        if isinstance(decrypted, dict):
            group_text = decrypted.get("group_text_data", {})
            if isinstance(group_text, dict):
                sender = group_text.get("sender")
                text = group_text.get("text")
                if isinstance(text, str):
                    if not isinstance(sender, str) or not sender.strip():
                        sender, text = self._extract_sender_from_message(text)
                    return {
                        "decryptable": True,
                        "sender": sender,
                        "message_body": text,
                    }

        try:
            payload_type = (
                packet.get_payload_type() if hasattr(packet, "get_payload_type") else None
            )
        except Exception:
            return {
                "decryptable": False,
                "message_body": None,
            }

        if payload_type != PAYLOAD_TYPE_GRP_TXT:
            return {
                "decryptable": False,
                "message_body": None,
            }

        payload = (
            packet.get_payload()
            if hasattr(packet, "get_payload")
            else getattr(packet, "payload", None)
        )
        if not payload or len(payload) < 4:
            return {
                "decryptable": False,
                "message_body": None,
            }

        channel_hash = payload[0]
        cipher_mac = bytes(payload[1:3])
        ciphertext = bytes(payload[3:])

        secrets_tried = 0
        for secret in self._iter_policy_channel_secrets():
            secrets_tried += 1
            derived = self._derive_channel_hash(secret)
            secret_preview = secret[:8] + "..." if len(secret) > 8 else secret
            if derived != channel_hash:
                logger.debug(
                    "Channel decrypt: secret %s derived hash 0x%02X != packet hash 0x%02X, skipping",
                    secret_preview,
                    derived,
                    channel_hash,
                )
                continue

            logger.debug(
                "Channel decrypt: secret %s hash matches 0x%02X, attempting MAC+decrypt",
                secret_preview,
                channel_hash,
            )
            plaintext = self._decrypt_channel_message(secret, cipher_mac, ciphertext)
            if plaintext is None:
                logger.debug(
                    "Channel decrypt: secret %s MAC/decrypt failed",
                    secret_preview,
                )
                continue

            parsed = self._parse_channel_plaintext(plaintext)
            if not isinstance(parsed, dict):
                logger.debug(
                    "Channel decrypt: secret %s parse failed",
                    secret_preview,
                )
                continue

            content = parsed.get("content")
            if not isinstance(content, str):
                continue

            sender, message_body = self._extract_sender_from_message(content)
            logger.debug(
                "Channel decrypt: SUCCESS with secret %s, sender=%r, message_body=%r",
                secret_preview,
                sender,
                message_body[:40] if message_body else "",
            )
            return {
                "decryptable": True,
                "sender": sender.rstrip("\x00").rstrip(),
                "message_body": message_body.rstrip("\x00").rstrip(),
            }

        if secrets_tried == 0:
            logger.debug(
                "Channel decrypt: no policy channel secrets configured "
                "(objects.channels / objects.channel_hash_groups and inline rule secrets are empty/missing); "
                "decryptable=False",
            )
        else:
            logger.debug(
                "Channel decrypt: no matching secret found (tried %d), decryptable=False",
                secrets_tried,
            )
        return {
            "decryptable": False,
            "sender": None,
            "message_body": None,
        }

    def _iter_policy_channel_secrets(self):
        channels = self.objects.get("channels", {})
        if isinstance(channels, dict):
            channel_items = channels.values()
        elif isinstance(channels, (list, tuple)):
            channel_items = channels
        else:
            logger.debug(
                "Channel decrypt: objects.channels has unsupported type %s",
                type(channels).__name__,
            )
            return

        for channel_cfg in channel_items:
            if isinstance(channel_cfg, str):
                yield channel_cfg
                continue

            if isinstance(channel_cfg, dict):
                # Accept common schema variations used across policy/companion exports.
                secret = (
                    channel_cfg.get("secret")
                    or channel_cfg.get("key")
                    or channel_cfg.get("psk")
                    or channel_cfg.get("channel_secret")
                )
                if secret:
                    yield str(secret)
                else:
                    logger.debug(
                        "Channel decrypt: channel entry missing secret/key/psk/channel_secret keys",
                    )
                continue

            logger.debug(
                "Channel decrypt: skipping unsupported channel entry type %s",
                type(channel_cfg).__name__,
            )

        # Also accept full channel secrets provided via policy object groups.
        channel_hash_groups = self.objects.get("channel_hash_groups", {})
        if isinstance(channel_hash_groups, dict):
            for group_values in channel_hash_groups.values():
                values = (
                    group_values if isinstance(group_values, (list, tuple, set)) else [group_values]
                )
                for candidate in values:
                    secret = self._extract_channel_secret_literal(candidate)
                    if secret:
                        yield secret
        elif channel_hash_groups not in ({}, None):
            logger.debug(
                "Channel decrypt: objects.channel_hash_groups has unsupported type %s",
                type(channel_hash_groups).__name__,
            )

        for inline_secret in self._inline_channel_secrets:
            yield inline_secret

    @staticmethod
    def _secret_bytes_for_hash(channel_secret: str) -> bytes:
        try:
            secret_bytes = bytes.fromhex(channel_secret)
        except ValueError:
            secret_bytes = channel_secret.encode("utf-8")
        if len(secret_bytes) >= 32 and secret_bytes[16:32] == b"\x00" * 16:
            return secret_bytes[:16]
        if len(secret_bytes) > 32:
            return secret_bytes[:32]
        return secret_bytes

    def _derive_channel_hash(self, channel_secret: str) -> int:
        secret_bytes = self._secret_bytes_for_hash(channel_secret)
        return hashlib.sha256(secret_bytes).digest()[0]

    @staticmethod
    def _decrypt_channel_message(
        channel_secret: str, mac: bytes, ciphertext: bytes
    ) -> Optional[bytes]:
        try:
            try:
                secret_bytes = bytes.fromhex(channel_secret)
            except ValueError:
                secret_bytes = channel_secret.encode("utf-8")

            if len(secret_bytes) < 32:
                secret_bytes = secret_bytes + b"\x00" * (32 - len(secret_bytes))
            elif len(secret_bytes) > 32:
                secret_bytes = secret_bytes[:32]

            expected_mac = CryptoUtils._hmac_sha256(secret_bytes, ciphertext)[:2]
            if mac != expected_mac:
                return None

            return CryptoUtils._aes_decrypt(secret_bytes[:16], ciphertext)
        except Exception:
            return None

    @staticmethod
    def _parse_channel_plaintext(plaintext: bytes) -> Optional[dict]:
        if len(plaintext) < 5:
            return None

        try:
            timestamp = int.from_bytes(plaintext[:4], "little")
            flags = plaintext[4]
            raw = plaintext[5:].decode("utf-8", errors="replace")
            message_content = raw.rstrip("\x00")

            message_type = "unknown"
            if flags == 0x00:
                message_type = "plain_text"
            elif flags == 0x01:
                message_type = "cli_command"
            elif flags == 0x02:
                message_type = "signed_text"
                if len(plaintext) >= 7:
                    raw = plaintext[7:].decode("utf-8", errors="replace")
                    message_content = raw.rstrip("\x00")

            return {
                "timestamp": timestamp,
                "flags": flags,
                "message_type": message_type,
                "content": message_content,
            }
        except Exception:
            return None

    @staticmethod
    def _extract_sender_from_message(message_content: str) -> tuple[str, str]:
        if ": " in message_content:
            parts = message_content.split(": ", 1)
            if len(parts) == 2:
                return parts[0], parts[1]
        return "Unknown", message_content

    @staticmethod
    def _extract_channel_secret_literal(value: Any) -> Optional[str]:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            normalized_hex = raw[2:] if raw.lower().startswith("0x") else raw
            if len(normalized_hex) in (32, 64) and all(
                ch in "0123456789abcdefABCDEF" for ch in normalized_hex
            ):
                return normalized_hex
        return None

    @classmethod
    def _collect_inline_rule_channel_secrets(cls, rules: list) -> list[str]:
        secrets: list[str] = []

        def _consume_condition(cond: Any):
            if not isinstance(cond, dict):
                return
            if cond.get("field") != "channel_hash":
                return
            raw_value = cond.get("value")
            values = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
            for candidate in values:
                secret = cls._extract_channel_secret_literal(candidate)
                if secret:
                    secrets.append(secret)

        for rule in rules:
            if not isinstance(rule, dict):
                continue
            cond = rule.get("if", {})
            if isinstance(cond, dict) and "field" in cond:
                _consume_condition(cond)
                continue
            if not isinstance(cond, dict):
                continue
            for key in ("all", "any"):
                branch = cond.get(key)
                if isinstance(branch, list):
                    for item in branch:
                        _consume_condition(item)

        return list(dict.fromkeys(secrets))

    @staticmethod
    def _normalize_path_hash_value(value: Any) -> Optional[str]:
        if value is None:
            return None

        if isinstance(value, int):
            parsed = value
            if parsed < 0:
                return None
            if parsed <= 0xFF:
                width = 2
            elif parsed <= 0xFFFF:
                width = 4
            elif parsed <= 0xFFFFFF:
                width = 6
            else:
                raise ValueError("path hash exceeds 3 bytes")
            return f"{parsed:0{width}X}"
        else:
            raw = str(value).strip()
            if not raw:
                return None
            if raw.lower().startswith("0x"):
                raw = raw[2:]
            if not raw:
                return None
            if len(raw) % 2 != 0:
                raise ValueError("path hash hex length must be even")
            if len(raw) not in (2, 4, 6):
                raise ValueError("path hash must be 1, 2, or 3 bytes")
            if not all(ch in "0123456789abcdefABCDEF" for ch in raw):
                raise ValueError("path hash must be hex")
            return raw.upper()

    @classmethod
    def _normalize_path_hash_values(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple, set)):
            normalized = []
            for item in value:
                normalized_item = cls._normalize_path_hash_value(item)
                if normalized_item is not None:
                    normalized.append(normalized_item)
            lengths = {len(item) for item in normalized}
            if len(lengths) > 1:
                raise ValueError("path hashes cannot mix byte lengths")
            return normalized

        return cls._normalize_path_hash_value(value)

    @staticmethod
    def _normalize_channel_hash_value(value: Any) -> Optional[str]:
        if value is None:
            return None

        if isinstance(value, int):
            parsed = value
        else:
            raw = str(value).strip()
            if not raw:
                return None
            normalized_hex = raw[2:] if raw.lower().startswith("0x") else raw
            if len(normalized_hex) in (32, 64) and all(
                ch in "0123456789abcdefABCDEF" for ch in normalized_hex
            ):
                secret_bytes = PolicyEngine._secret_bytes_for_hash(normalized_hex)
                parsed = hashlib.sha256(secret_bytes).digest()[0]
                return f"0x{parsed:02X}"
            if raw.lower().startswith("0x"):
                parsed = int(raw, 16)
            elif raw.isdigit():
                parsed = int(raw, 10)
            else:
                parsed = int(raw, 16)

        if parsed < 0:
            raise ValueError("channel hash must be non-negative")
        if parsed > 0xFF:
            raise ValueError("channel hash must be one byte (0x00-0xFF)")
        return f"0x{parsed:02X}"

    @classmethod
    def _normalize_channel_hash_values(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple, set)):
            normalized = []
            for item in value:
                normalized_item = cls._normalize_channel_hash_value(item)
                if normalized_item is not None:
                    normalized.append(normalized_item)
            return normalized

        return cls._normalize_channel_hash_value(value)

    @staticmethod
    def _compare(actual: Any, op: Any, expected: Any) -> bool:
        op_name = str(op or "equals").lower()

        try:
            if op_name in ("equals", "eq", "=="):
                return actual == expected
            if op_name in ("not_equals", "ne", "!="):
                return actual != expected
            if op_name in ("greater_than", "gt", ">"):
                return actual is not None and expected is not None and actual > expected
            if op_name in ("greater_or_equal", "gte", ">="):
                return actual is not None and expected is not None and actual >= expected
            if op_name in ("less_than", "lt", "<"):
                return actual is not None and expected is not None and actual < expected
            if op_name in ("less_or_equal", "lte", "<="):
                return actual is not None and expected is not None and actual <= expected
            if op_name == "contains":
                if isinstance(actual, (list, tuple, set)):
                    return expected in actual
                if isinstance(actual, str) and expected is not None:
                    return str(expected) in actual
                return False
            if op_name in ("in", "is_in"):
                if isinstance(expected, (list, tuple, set)):
                    return actual in expected
                if isinstance(expected, str) and actual is not None:
                    return str(actual) in expected
                return False
            if op_name in ("intersects", "overlaps"):
                if isinstance(actual, (list, tuple, set)) and isinstance(
                    expected, (list, tuple, set)
                ):
                    return len(set(actual).intersection(set(expected))) > 0
                return False
            if op_name == "starts_with":
                return (
                    isinstance(actual, str)
                    and isinstance(expected, str)
                    and actual.startswith(expected)
                )
            if op_name == "ends_with":
                return (
                    isinstance(actual, str)
                    and isinstance(expected, str)
                    and actual.endswith(expected)
                )
        except Exception:
            return False

        return False
