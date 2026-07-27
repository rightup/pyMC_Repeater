"""
Discovery request/response handling helper for openHop Repeater.

This module handles the processing and response to discovery requests,
allowing other nodes to discover repeaters on the mesh network.
"""

import asyncio
import logging
import secrets
import threading
import time
import uuid
from typing import Any, Callable, Optional

from openhop_core.node.handlers.control import ControlHandler

logger = logging.getLogger("DiscoveryHelper")

# Default upper bound (ms) for the randomized pre-send jitter applied to node
# discovery responses. A node-discover request is a broadcast that every
# in-range repeater answers at once, so without jitter they all transmit at the
# same engine-scheduled instant and collide. Mirrors the firmware, which spreads
# these replies deliberately (MyMesh.cpp:797, sendZeroHop with
# getRetransmitDelay*4). Safe to be generous: the requester's discovery window is
# 60s (firmware pending_discover_until = futureMillis(60000)).
DEFAULT_DISCOVERY_RESPONSE_JITTER_MS = 2000

DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 10.0
DISCOVERY_EVENT_BACKLOG_LIMIT = 512

NODE_TYPE_NAMES = {
    1: "Chat Node",
    2: "Repeater",
    3: "Room Server",
}


class DiscoveryHelper:
    """Helper class for processing discovery requests in the repeater."""

    def __init__(
        self,
        local_identity,
        packet_injector=None,
        node_type: int = 2,
        log_fn=None,
        debug_log_fn=None,
        response_jitter_ms: int = DEFAULT_DISCOVERY_RESPONSE_JITTER_MS,
    ):
        """
        Initialize the discovery helper.

        Args:
            local_identity: The LocalIdentity instance for this repeater
            packet_injector: Callable to inject new packets into the router for sending
            node_type: Node type identifier (2 = Repeater)
            log_fn: Optional logging function for ControlHandler
            debug_log_fn: Optional logging for verbose ControlHandler messages (e.g. callback
                presence). Pass logger.debug to avoid INFO noise when forwarding to companions.
            response_jitter_ms: Upper bound (ms) for the randomized delay added before
                transmitting a discovery response, to avoid multiple repeaters colliding
                when answering the same broadcast. Set to 0 to disable (e.g. in tests).
        """
        self.local_identity = local_identity
        self.packet_injector = packet_injector  # Function to inject packets into router
        self.node_type = node_type
        self.response_jitter_ms = max(0, int(response_jitter_ms))

        # Create ControlHandler internally as a parsing utility
        self.control_handler = ControlHandler(
            log_fn=log_fn or logger.info,
            debug_log_fn=debug_log_fn,
        )
        self._pending_tasks = set()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._sessions_lock = threading.Lock()

        # Set up the request callback
        self.control_handler.set_request_callback(self._on_discovery_request)
        logger.debug("Discovery handler initialized")

    def create_session(
        self,
        *,
        timeout: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        filter_mask: int,
        since: int = 0,
        prefix_only: bool = False,
        result_enricher: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Create a new discovery session and return its public metadata."""
        session_id = uuid.uuid4().hex
        tag = secrets.randbits(32)
        created_at = time.time()
        session = {
            "session_id": session_id,
            "tag": tag,
            "timeout": max(1.0, float(timeout)),
            "filter_mask": int(filter_mask) & 0xFF,
            "since": max(0, int(since)),
            "prefix_only": bool(prefix_only),
            "created_at": created_at,
            "started_at": None,
            "completed_at": None,
            "status": "created",
            "results": {},
            "events": [],
            "next_event_id": 1,
            "error": None,
            "result_enricher": result_enricher,
        }
        with self._sessions_lock:
            self._sessions[session_id] = session
        return self.get_session_snapshot(session_id) or {}

    def get_session_snapshot(self, session_id: str) -> Optional[dict[str, Any]]:
        """Return a public snapshot for a discovery session."""
        with self._sessions_lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            return self._public_session_snapshot(session)

    def get_events_since(self, session_id: str, last_event_id: int = 0) -> Optional[dict[str, Any]]:
        """Return all session events newer than last_event_id."""
        with self._sessions_lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            events = [event for event in session["events"] if event["id"] > last_event_id]
            return {
                "events": events,
                "status": session["status"],
                "completed": session["status"] in {"completed", "timed_out", "error", "cancelled"},
                "latest_event_id": session["next_event_id"] - 1,
            }

    async def execute_session(self, session_id: str) -> None:
        """Send a discovery request and stream responses into the session."""
        session = self._get_session(session_id)
        if not session:
            raise ValueError(f"Unknown discovery session: {session_id}")

        if session["status"] != "created":
            return

        session["started_at"] = time.time()
        session["status"] = "running"
        self._emit_event(
            session_id,
            "started",
            {
                "session_id": session_id,
                "tag": session["tag"],
                "timeout": session["timeout"],
                "filter_mask": session["filter_mask"],
                "since": session["since"],
                "prefix_only": session["prefix_only"],
                "started_at": session["started_at"],
            },
        )

        try:
            from openhop_core.protocol.packet_builder import PacketBuilder

            packet = PacketBuilder.create_discovery_request(
                tag=session["tag"],
                filter_mask=session["filter_mask"],
                since=session["since"],
                prefix_only=session["prefix_only"],
            )

            def _response_callback(response_data: dict[str, Any]) -> None:
                self._record_response(session_id, response_data)

            self.control_handler.set_response_callback(session["tag"], _response_callback)

            if not self.packet_injector:
                raise RuntimeError("No packet injector available")

            success = await self.packet_injector(packet, wait_for_ack=False)
            if not success:
                raise RuntimeError("Failed to send discovery request")

            logger.info(
                "Discovery request sent for session %s tag 0x%08X filter=0x%02X",
                session_id,
                session["tag"],
                session["filter_mask"],
            )

            await asyncio.sleep(session["timeout"])
            self._finish_session(session_id, "completed")
        except asyncio.CancelledError:
            self._finish_session(session_id, "cancelled")
            raise
        except Exception as e:
            logger.error("Discovery session %s failed: %s", session_id, e, exc_info=True)
            self._finish_session(session_id, "error", error=str(e))
        finally:
            self.control_handler.clear_response_callback(session["tag"])

    def start_session_task(self, session_id: str) -> None:
        """Schedule a discovery session on the current event loop."""
        task = asyncio.create_task(self.execute_session(session_id))
        self._track_task(task)

    def cleanup_sessions(self, max_age_seconds: int = 120) -> None:
        """Remove old completed sessions to keep memory bounded."""
        cutoff = time.time() - max_age_seconds
        with self._sessions_lock:
            stale_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session["status"] in {"completed", "timed_out", "error", "cancelled"}
                and (session.get("completed_at") or session.get("created_at", 0)) < cutoff
            ]
            for session_id in stale_ids:
                self._sessions.pop(session_id, None)

    def _get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._sessions_lock:
            return self._sessions.get(session_id)

    def _record_response(self, session_id: str, response_data: dict[str, Any]) -> None:
        with self._sessions_lock:
            session = self._sessions.get(session_id)
            if not session or session["status"] != "running":
                return

            result = dict(response_data)
            result["node_type_name"] = NODE_TYPE_NAMES.get(
                result.get("node_type"), f"Unknown({result.get('node_type', 0)})"
            )
            result["discovered_at"] = time.time()

            enricher = session.get("result_enricher")
            if enricher:
                try:
                    result = enricher(result)
                except Exception as e:
                    logger.debug("Discovery result enrichment failed: %s", e)

            result_key = str(result.get("pub_key") or "")
            if not result_key:
                return

            existing = session["results"].get(result_key)
            session["results"][result_key] = result
            payload = {
                "session_id": session_id,
                "tag": session["tag"],
                "result": result,
                "is_update": existing is not None,
                "count": len(session["results"]),
            }
            self._append_event_unlocked(session, "discovery_result", payload)

    def _finish_session(self, session_id: str, status: str, error: Optional[str] = None) -> None:
        with self._sessions_lock:
            session = self._sessions.get(session_id)
            if not session or session["status"] in {"completed", "timed_out", "error", "cancelled"}:
                return

            session["completed_at"] = time.time()
            session["status"] = status
            session["error"] = error
            payload = {
                "session_id": session_id,
                "tag": session["tag"],
                "status": status,
                "error": error,
                "count": len(session["results"]),
                "duration_ms": round(
                    (
                        (session["completed_at"] or session["created_at"])
                        - (session["started_at"] or session["created_at"])
                    )
                    * 1000,
                    2,
                ),
                "completed_at": session["completed_at"],
                "results": list(session["results"].values()),
            }
            event_type = "error" if status == "error" else "completed"
            self._append_event_unlocked(session, event_type, payload)

    def _emit_event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._sessions_lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            self._append_event_unlocked(session, event_type, payload)

    def _append_event_unlocked(
        self, session: dict[str, Any], event_type: str, payload: dict[str, Any]
    ) -> None:
        event_id = session["next_event_id"]
        session["next_event_id"] += 1
        session["events"].append(
            {
                "id": event_id,
                "event": event_type,
                "data": payload,
            }
        )
        if len(session["events"]) > DISCOVERY_EVENT_BACKLOG_LIMIT:
            session["events"] = session["events"][-DISCOVERY_EVENT_BACKLOG_LIMIT:]

    def _public_session_snapshot(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": session["session_id"],
            "tag": session["tag"],
            "status": session["status"],
            "timeout": session["timeout"],
            "filter_mask": session["filter_mask"],
            "since": session["since"],
            "prefix_only": session["prefix_only"],
            "created_at": session["created_at"],
            "started_at": session["started_at"],
            "completed_at": session["completed_at"],
            "count": len(session["results"]),
            "error": session["error"],
        }

    def _track_task(self, task: asyncio.Task) -> None:
        self._pending_tasks.add(task)

        def _on_done(done_task: asyncio.Task) -> None:
            self._pending_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Background discovery task failed: {e}", exc_info=True)

        task.add_done_callback(_on_done)

    def _on_discovery_request(self, request_data: dict) -> None:
        """
        Handle incoming discovery request.

        Args:
            request_data: Dictionary containing the parsed discovery request
        """
        try:
            tag = request_data.get("tag", 0)
            filter_byte = request_data.get("filter", 0)
            prefix_only = request_data.get("prefix_only", False)
            snr = request_data.get("snr", 0.0)
            rssi = request_data.get("rssi", 0)

            logger.info(
                f"Request: tag=0x{tag:08X}, filter=0x{filter_byte:02X}, "
                f"SNR={snr:+.1f}dB, RSSI={rssi}dBm"
            )

            # Check if filter matches our node type (repeater = 2, filter_mask = 0x04)
            filter_mask = 1 << self.node_type  # 1 << 2 = 0x04
            if (filter_byte & filter_mask) == 0:
                logger.debug("Filter doesn't match, ignoring")
                return

            logger.info("Sending response...")

            if self.local_identity:
                self._send_discovery_response(tag, self.node_type, snr, prefix_only)
            else:
                logger.warning("No local identity available for response")

        except Exception as e:
            logger.error(f"Error handling request: {e}")

    def _send_discovery_response(
        self,
        tag: int,
        node_type: int,
        inbound_snr: float,
        prefix_only: bool,
    ) -> None:
        """
        Create and send a discovery response packet.

        Args:
            tag: The tag from the discovery request
            node_type: Node type identifier
            inbound_snr: SNR of the received request
            prefix_only: Whether to use prefix-only mode
        """
        try:
            our_pub_key = self.local_identity.get_public_key()

            from openhop_core.protocol.packet_builder import PacketBuilder

            response_packet = PacketBuilder.create_discovery_response(
                tag=tag,
                node_type=node_type,
                inbound_snr=inbound_snr,
                pub_key=our_pub_key,
                prefix_only=prefix_only,
            )

            # Send response via router injection
            if self.packet_injector:
                task = asyncio.create_task(self._send_packet_async(response_packet, tag))
                self._track_task(task)
            else:
                logger.warning("No packet injector available - discovery response not sent")

        except Exception as e:
            logger.error(f"Error creating discovery response: {e}")

    async def _send_packet_async(self, packet, tag: int) -> None:
        """
        Send a discovery response packet via router injection.

        Args:
            packet: The packet to send
            tag: The tag for logging purposes
        """
        try:
            # Randomized pre-send jitter so multiple repeaters answering the same
            # zero-hop discovery broadcast don't transmit at the same engine-scheduled
            # instant and collide (the engine's DIRECT delay is fixed, not random).
            # Mirrors firmware MyMesh.cpp:797. Uses secrets like the engine's TX jitter.
            if self.response_jitter_ms > 0:
                jitter_s = secrets.randbelow(self.response_jitter_ms + 1) / 1000.0
                if jitter_s > 0:
                    logger.debug(
                        f"Discovery response jitter {jitter_s * 1000:.0f}ms for tag 0x{tag:08X}"
                    )
                    await asyncio.sleep(jitter_s)

            success = await self.packet_injector(packet, wait_for_ack=False)
            if success:
                logger.info(f"Response sent for tag 0x{tag:08X}")
            else:
                logger.warning(f"Failed to send response for tag 0x{tag:08X}")
        except Exception as e:
            logger.error(f"Error sending response: {e}")
