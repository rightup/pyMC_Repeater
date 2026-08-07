"""Transport-independent persistence for inbound companion messages."""

from __future__ import annotations

import logging
import time

from repeater.companion.correlation import await_to_thread_outcome

logger = logging.getLogger("CompanionInboundHistory")


def message_dict_from_event(event_name: str, event) -> dict:
    """Return the canonical durable-message shape for one Core receive event."""
    if event_name == "message_event":
        return {
            "sender_key": event.sender_key,
            "text": event.text,
            "timestamp": event.timestamp,
            "txt_type": event.txt_type,
            "is_channel": False,
            "channel_idx": 0,
            "path_len": event.path_len,
            "packet_hash": event.packet_hash,
            "snr": event.snr,
            "rssi": event.rssi,
            "sender_prefix": event.sender_prefix,
        }
    if event_name == "channel_message_event":
        return {
            "sender_key": b"",
            "text": event.text,
            "timestamp": event.timestamp,
            "txt_type": 0,
            "is_channel": True,
            "channel_idx": event.channel_idx,
            "path_len": event.path_len,
            "packet_hash": event.packet_hash,
            "snr": event.snr,
            "rssi": event.rssi,
        }
    if event_name == "channel_data_event":
        return {
            "sender_key": b"",
            "text": "",
            "timestamp": 0,
            "txt_type": 0,
            "is_channel": True,
            "channel_idx": event.channel_idx,
            "path_len": event.path_len,
            "packet_hash": event.packet_hash,
            "snr": event.snr,
            "rssi": event.rssi,
            "channel_data_type": event.data_type,
            "channel_data_payload": bytes(event.payload or b""),
        }
    raise ValueError(f"unsupported inbound companion event: {event_name}")


def remove_queue_entry(bridge, companion_hash: str, queue_entry) -> None:
    """Remove one persisted Core queue entry without disturbing later receives."""
    if queue_entry is None:
        return
    remove = getattr(bridge.message_queue, "remove", None)
    if not callable(remove):
        logger.warning(
            "Companion %s: core message queue lacks identity removal; "
            "leaving the in-memory entry untouched",
            companion_hash,
        )
        return
    try:
        removed = remove(queue_entry)
    except Exception:
        logger.exception(
            "Companion %s: persisted queue entry removal failed",
            companion_hash,
        )
        return
    if not removed:
        logger.debug(
            "Companion %s: persisted queue entry was already consumed",
            companion_hash,
        )


async def persist_inbound_message(
    *,
    bridge,
    sqlite_handler,
    companion_hash: str,
    msg_dict: dict,
    queue_entry=None,
    journal=None,
    tracker=None,
) -> None:
    """Commit one inbound message before making it visible to API clients."""
    if sqlite_handler is None:
        return

    packet_hash = msg_dict.get("packet_hash")
    provisional = tracker is not None and bool(packet_hash)
    registration_token = None
    if provisional:
        registration_token = tracker.register_inbound(
            packet_hash,
            companion_hash,
            None,
            initial_hit={
                "direction": "in",
                "companion_hash": companion_hash,
                "message_id": None,
                "packet_hash": packet_hash,
                "path": list(msg_dict.get("original_path") or ()),
                "rssi": msg_dict.get("rssi"),
                "snr": msg_dict.get("snr"),
                "observed_at": time.time(),
                "observation_count": 1,
                "unique_path_count": 1,
            },
        )
        provisional = registration_token is not None

    retention = getattr(
        bridge.message_queue,
        "max_size",
        getattr(bridge.message_queue, "_max_size", None),
    )
    cancellation = None
    try:
        if journal is not None:
            result, cancellation = await await_to_thread_outcome(
                journal.store_inbound_message,
                msg_dict,
                retention,
            )
        else:
            result, cancellation = await await_to_thread_outcome(
                sqlite_handler.companion_store_inbound_message,
                companion_hash,
                msg_dict,
                retention,
            )
    except BaseException:
        if provisional:
            tracker.discard_registration(registration_token)
        raise

    # SQLite is now the pending-delivery source of truth. Remove the exact
    # in-memory copy even after deduplication so Frame cannot deliver it twice.
    remove_queue_entry(bridge, companion_hash, queue_entry)
    if provisional:
        buffered_hits = (
            tracker.promote_inbound(
                packet_hash,
                companion_hash,
                result.get("message_id"),
                registration_token=registration_token,
                existing_message=(result.get("message") if not result.get("inserted") else None),
            )
            or ()
        )
        for hit in buffered_hits:
            if journal is not None:
                _, worker_cancellation = await await_to_thread_outcome(
                    journal.record_inbound_reception,
                    hit,
                )
            else:
                _, worker_cancellation = await await_to_thread_outcome(
                    sqlite_handler.companion_record_inbound_reception,
                    companion_hash,
                    hit["message_id"],
                    hit,
                )
            tracker.acknowledge(hit)
            if cancellation is None:
                cancellation = worker_cancellation
    if cancellation is not None:
        raise cancellation
