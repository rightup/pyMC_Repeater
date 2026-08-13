"""
Protocol request (REQ) handling helper for openHop Repeater.

Provides repeater-specific callbacks for status and telemetry requests.
"""

import asyncio
import logging
import struct
import time

from openhop_core.node.handlers.protocol_request import (
    REQ_TYPE_GET_ACCESS_LIST,
    REQ_TYPE_GET_NEIGHBOURS,
    REQ_TYPE_GET_OWNER_INFO,
    REQ_TYPE_GET_STATUS,
    REQ_TYPE_GET_TELEMETRY_DATA,
    SERVER_RESPONSE_DELAY_MS,
    ProtocolRequestHandler,
)
from openhop_core.protocol.cayenne_lpp import (
    TELEM_CHANNEL_SELF,
    encode_barometric_pressure,
    encode_current,
    encode_power,
    encode_relative_humidity,
    encode_temperature,
    encode_voltage,
)
from openhop_core.protocol.constants import TELEM_PERM_ENVIRONMENT

logger = logging.getLogger("ProtocolRequestHelper")


class ProtocolRequestHelper:
    """Provides repeater-specific protocol request handlers."""

    def __init__(
        self,
        identity_manager,
        packet_injector=None,
        acl_dict=None,
        radio=None,
        engine=None,
        neighbor_tracker=None,
        config=None,
        sensor_manager=None,
    ):

        self.identity_manager = identity_manager
        self.packet_injector = packet_injector
        self.acl_dict = acl_dict or {}
        self.radio = radio
        self.engine = engine
        self.neighbor_tracker = neighbor_tracker
        self.config = config or {}
        self.sensor_manager = sensor_manager

        # Dictionary of core handlers keyed by dest_hash
        self.handlers = {}

    def register_identity(self, name: str, identity, identity_type: str = "repeater"):

        hash_byte = identity.get_public_key()[0]

        # Get ACL for this identity
        identity_acl = self.acl_dict.get(hash_byte)
        if not identity_acl:
            logger.warning(f"Cannot register identity '{name}': no ACL for hash 0x{hash_byte:02X}")
            return

        # Create ACL contacts wrapper
        acl_contacts = self._create_acl_contacts_wrapper(identity_acl)

        # Build request handlers dict
        request_handlers = {
            REQ_TYPE_GET_STATUS: self._handle_get_status,
            REQ_TYPE_GET_TELEMETRY_DATA: self._handle_get_telemetry,
            REQ_TYPE_GET_ACCESS_LIST: self._make_handle_get_access_list(identity_acl),
            REQ_TYPE_GET_NEIGHBOURS: self._handle_get_neighbours,
            REQ_TYPE_GET_OWNER_INFO: self._handle_get_owner_info,
        }

        # Create core handler
        handler = ProtocolRequestHandler(
            local_identity=identity,
            contacts=acl_contacts,
            get_client_fn=lambda src_hash: self._get_client_from_acl(identity_acl, src_hash),
            get_clients_fn=lambda src_hash: self._get_clients_from_acl(identity_acl, src_hash),
            request_handlers=request_handlers,
            log_fn=logger.info,
        )

        self.handlers[hash_byte] = {
            "handler": handler,
            "identity": identity,
            "name": name,
            "type": identity_type,
        }

        logger.info(f"Registered protocol request handler for '{name}': hash=0x{hash_byte:02X}")

    def _create_acl_contacts_wrapper(self, acl):
        """Create contacts wrapper from ACL."""

        class ACLContactsWrapper:
            def __init__(self, identity_acl):
                self._acl = identity_acl

            @property
            def contacts(self):
                return self._acl.get_all_clients()

        return ACLContactsWrapper(acl)

    def _get_client_from_acl(self, acl, src_hash: int):
        """Get client from ACL by source hash."""
        clients = self._get_clients_from_acl(acl, src_hash)
        return clients[0] if clients else None

    def _get_clients_from_acl(self, acl, src_hash: int):
        """Get all ACL clients whose public-key first byte matches source hash."""
        matches = []
        for client_info in acl.get_all_clients():
            if client_info.id.get_public_key()[0] == src_hash:
                matches.append(client_info)
        return matches

    async def process_request_packet(self, packet):

        try:
            if len(packet.payload) < 2:
                return False

            dest_hash = packet.payload[0]

            handler_info = self.handlers.get(dest_hash)
            if not handler_info:
                return False

            # Let the core handler decrypt and build a response. It returns a
            # HandlerResult: authenticated is False when the one-byte dest hash
            # collided with ours but the REQ did not decrypt for a local client —
            # do not consume it, so the engine can still forward/re-flood it.
            result = await handler_info["handler"](packet)
            if not result.authenticated:
                logger.debug(
                    f"REQ dest 0x{dest_hash:02X} did not decrypt for a local identity "
                    f"(hash collision), allowing forward"
                )
                return False

            # Send response after delay
            if result.response and self.packet_injector:
                await asyncio.sleep(SERVER_RESPONSE_DELAY_MS / 1000.0)
                await self.packet_injector(result.response, wait_for_ack=False)

            packet.mark_do_not_retransmit()
            return True

        except Exception as e:
            logger.error(f"Error processing protocol request: {e}", exc_info=True)
            return False

    def _handle_get_status(self, client, timestamp: int, req_data: bytes):
        """Build 56-byte RepeaterStats (firmware layout from MeshCore simple_repeater/MyMesh.h)."""
        # RepeaterStats: uint16 batt, uint16 curr_tx_queue_len, int16 noise_floor, int16 last_rssi,
        # uint32 n_packets_recv, n_packets_sent, total_air_time_secs, total_up_time_secs,
        # n_sent_flood, n_sent_direct, n_recv_flood, n_recv_direct,
        # uint16 err_events, int16 last_snr (×4), uint16 n_direct_dups, n_flood_dups,
        # uint32 total_rx_air_time_secs, n_recv_errors  → 56 bytes

        # Battery Readings: uses first configured sensor that reports bus/pack voltage.
        readings = self._get_sensor_readings()
        batt = int(min(max(self._battery_voltage(readings) * 1000, 0), 0xFFFF))

        # Uptime: use engine start_time when available (fixes wrong "20521 days" from time.time())
        if self.engine and hasattr(self.engine, "start_time"):
            total_up_time_secs = int(time.time() - self.engine.start_time)
        else:
            total_up_time_secs = 0

        # Radio: noise floor, last RSSI, last SNR (firmware stores SNR × 4)
        if self.radio:
            noise_floor = int(getattr(self.radio, "get_noise_floor", lambda: 0)() or 0)
            if callable(getattr(self.radio, "get_last_rssi", None)):
                last_rssi = int(self.radio.get_last_rssi() or -120)
            else:
                last_rssi = int(getattr(self.radio, "last_rssi", -120) or -120)
            if callable(getattr(self.radio, "get_last_snr", None)):
                last_snr = int((self.radio.get_last_snr() or 0) * 4)
            else:
                last_snr = int((getattr(self.radio, "last_snr", 0) or 0) * 4)
        else:
            noise_floor = 0
            last_rssi = -120
            last_snr = 0

        # Packet counts: prefer engine (rx_count, forwarded_count); fall back to radio if present
        if self.engine:
            n_packets_recv = getattr(self.engine, "rx_count", 0)
            n_packets_sent = getattr(self.engine, "forwarded_count", 0)
        elif self.radio:
            n_packets_recv = getattr(self.radio, "packets_received", 0) or 0
            n_packets_sent = getattr(self.radio, "packets_sent", 0) or 0
        else:
            n_packets_recv = 0
            n_packets_sent = 0

        # Airtime (AirtimeManager uses total_airtime_ms for TX; total_rx_airtime_ms if we track RX)
        total_air_time_secs = 0
        total_rx_air_time_secs = 0
        if self.engine:
            am = getattr(self.engine, "airtime_mgr", None) or getattr(
                self.engine, "airtime_manager", None
            )
            if am is not None:
                total_air_time_secs = int(getattr(am, "total_airtime_ms", 0) or 0) // 1000
                total_rx_air_time_secs = int(getattr(am, "total_rx_airtime_ms", 0) or 0) // 1000

        # Routing stats (flood/direct and dups - from engine when available)
        n_sent_flood = getattr(self.engine, "sent_flood_count", 0) if self.engine else 0
        n_sent_direct = getattr(self.engine, "sent_direct_count", 0) if self.engine else 0
        n_recv_flood = getattr(self.engine, "recv_flood_count", 0) if self.engine else 0
        n_recv_direct = getattr(self.engine, "recv_direct_count", 0) if self.engine else 0
        n_direct_dups = getattr(self.engine, "direct_dup_count", 0) if self.engine else 0
        n_flood_dups = getattr(self.engine, "flood_dup_count", 0) if self.engine else 0
        n_recv_errors = int(getattr(self.radio, "crc_error_count", 0) or 0) if self.radio else 0

        # Pack 56-byte RepeaterStats (layout matches firmware)
        stats = struct.pack(
            "<HHhhIIIIIIIIHhHHII",
            batt,  # battery in mV
            0,  # curr_tx_queue_len (TODO)
            noise_floor,
            last_rssi,
            n_packets_recv,
            n_packets_sent,
            total_air_time_secs,
            total_up_time_secs,
            n_sent_flood,
            n_sent_direct,
            n_recv_flood,
            n_recv_direct,
            0,  # err_events
            last_snr,
            n_direct_dups,
            n_flood_dups,
            total_rx_air_time_secs,
            n_recv_errors,
        )

        logger.debug(
            "GET_STATUS: uptime=%ds, noise=%ddBm, rssi=%ddBm, snr=%.1fdB, rx=%s, tx=%s",
            total_up_time_secs,
            noise_floor,
            last_rssi,
            last_snr / 4.0,
            n_packets_recv,
            n_packets_sent,
        )

        return stats

    def _handle_get_telemetry(self, client, timestamp: int, req_data: bytes):
        """Return CayenneLPP telemetry: base voltage + configured sensors.

        Matches C++ simple_repeater/simple_room_server handleRequest
        REQ_TYPE_GET_TELEMETRY_DATA. req_data[0] is the inverse permission mask
        (firmware payload[1]); guests are restricted to base telemetry. The base
        voltage entry on TELEM_CHANNEL_SELF is always emitted. Environment
        sensors are appended only when the mask grants environment access.

        The firmware's MCU-temperature append is intentionally not mirrored: only
        configured sensors are reported.
        """
        # Inverse mask -> permission mask (firmware: perm_mask = ~payload[1]).
        inverse_mask = req_data[0] if len(req_data) >= 1 else 0
        perm_mask = (~inverse_mask) & 0xFF

        # Guests get base telemetry only (firmware: PERM_ACL_GUEST -> perm_mask 0).
        if hasattr(client, "is_guest") and client.is_guest():
            perm_mask = 0x00

        readings = self._get_sensor_readings()

        # Base voltage entry: configured UPS/battery voltage when available,
        # else 0.0 V (voltage-only floor, mirroring the companion self-telemetry).
        lpp = bytearray(encode_voltage(TELEM_CHANNEL_SELF, self._battery_voltage(readings)))

        # One channel per sensor reading (matches firmware channel assignment).
        if perm_mask & TELEM_PERM_ENVIRONMENT:
            channel = TELEM_CHANNEL_SELF + 1
            for reading in readings:
                entry = self._encode_power_reading(
                    channel, reading
                ) + self._encode_environment_reading(channel, reading)
                if entry:
                    lpp.extend(entry)
                    channel += 1

        logger.debug(
            "GET_TELEMETRY: perm_mask=0x%02X, %d LPP bytes",
            perm_mask,
            len(lpp),
        )
        return bytes(lpp)

    def _get_sensor_readings(self):
        """Return the latest cached sensor readings (empty list if unavailable)."""
        if not self.sensor_manager:
            return []
        try:
            summary = self.sensor_manager.get_summary()
            return summary.get("readings", []) or []
        except Exception as exc:
            logger.debug("Could not read sensor summary: %s", exc)
            return []

    @staticmethod
    def _battery_voltage(readings) -> float:
        """First configured sensor's pack/bus voltage, else 0.0 V."""
        for reading in readings:
            if not reading.get("ok"):
                continue
            data = reading.get("data") or {}
            voltage = data.get("bus_voltage_v")
            if voltage is not None:
                try:
                    return float(voltage)
                except (TypeError, ValueError):
                    continue
        return 0.0

    @staticmethod
    def _encode_power_reading(channel: int, reading) -> bytes:
        """Encode a voltage/current/power reading as CayenneLPP, or b"" if none.

        TODO: a multi-rail sensor (e.g. INA3221) would need one channel per
        rail here, not one channel for the whole reading.
        """
        if not reading.get("ok"):
            return b""
        data = reading.get("data") or {}
        out = bytearray()
        voltage = data.get("bus_voltage_v")
        if voltage is not None:
            try:
                out.extend(encode_voltage(channel, float(voltage)))
            except (TypeError, ValueError):
                pass
        current = data.get("current_ma")
        if current is not None:
            try:
                out.extend(encode_current(channel, float(current) / 1000.0))
            except (TypeError, ValueError):
                pass
        power = data.get("power_mw")
        if power is not None:
            try:
                out.extend(encode_power(channel, int(float(power) / 1000.0)))
            except (TypeError, ValueError):
                pass
        return bytes(out)

    @staticmethod
    def _encode_environment_reading(channel: int, reading) -> bytes:
        """Encode a temperature/humidity/pressure reading as CayenneLPP, or b"" if none.

        Follows the firmware SHT/BME query order: temperature, relative
        humidity, then barometric pressure, all on the same channel.
        """
        if not reading.get("ok"):
            return b""
        data = reading.get("data") or {}
        out = bytearray()
        temperature = data.get("temperature_c")
        if temperature is not None:
            try:
                out.extend(encode_temperature(channel, float(temperature)))
            except (TypeError, ValueError):
                pass
        humidity = data.get("humidity_pct")
        if humidity is not None:
            try:
                out.extend(encode_relative_humidity(channel, float(humidity)))
            except (TypeError, ValueError):
                pass
        pressure = data.get("pressure_hpa")
        if pressure is not None:
            try:
                out.extend(encode_barometric_pressure(channel, float(pressure)))
            except (TypeError, ValueError):
                pass
        return bytes(out)

    def _make_handle_get_access_list(self, identity_acl):
        """Create a closure for GET_ACCESS_LIST bound to a specific identity ACL."""

        def _handler(client, timestamp: int, req_data: bytes):
            return self._handle_get_access_list(client, timestamp, req_data, identity_acl)

        return _handler

    def _handle_get_access_list(self, client, timestamp: int, req_data: bytes, identity_acl):
        """Return ACL entries: [pub_key_prefix(6) + permissions(1)] per client.

        Admin-only. Matches C++ simple_repeater handleRequest REQ_TYPE_GET_ACCESS_LIST.
        """
        if not hasattr(client, "is_admin") or not client.is_admin():
            logger.debug("GET_ACCESS_LIST rejected: client is not admin")
            return None

        # req_data[0] and req_data[1] are reserved bytes; must both be 0
        if len(req_data) >= 2 and (req_data[0] != 0 or req_data[1] != 0):
            logger.debug("GET_ACCESS_LIST: reserved bytes non-zero, ignoring")
            return None

        result = bytearray()
        for ci in identity_acl.get_all_clients():
            if ci.permissions == 0:
                continue  # skip deleted entries
            pubkey = ci.id.get_public_key()
            result.extend(pubkey[:6])  # 6-byte pub_key prefix
            result.append(ci.permissions & 0xFF)

        logger.debug("GET_ACCESS_LIST: returning %d entries", len(result) // 7)
        return bytes(result)

    def _handle_get_neighbours(self, client, timestamp: int, req_data: bytes):
        """Return paginated, sorted neighbour list.

        Matches C++ simple_repeater handleRequest REQ_TYPE_GET_NEIGHBOURS.
        Request: version(1) + count(1) + offset(2 LE) + order_by(1) + pubkey_prefix_len(1) + random(4)
        Response: total_count(2 LE) + results_count(2 LE) + entries
        Each entry: pubkey_prefix(N) + heard_seconds_ago(4 LE) + snr(1 signed)
        """
        if len(req_data) < 7:
            logger.debug("GET_NEIGHBOURS: req_data too short (%d bytes)", len(req_data))
            return None

        request_version = req_data[0]
        if request_version != 0:
            logger.debug("GET_NEIGHBOURS: unsupported version %d", request_version)
            return None

        count = req_data[1]
        offset = struct.unpack_from("<H", req_data, 2)[0]
        order_by = req_data[4]
        pubkey_prefix_len = min(req_data[5], 32)

        # Fetch neighbours from storage
        storage = getattr(self.neighbor_tracker, "storage", None) if self.neighbor_tracker else None
        if not storage or not hasattr(storage, "get_neighbors"):
            logger.debug("GET_NEIGHBOURS: no storage available")
            # Return empty result
            return struct.pack("<HH", 0, 0)

        raw_neighbors = storage.get_neighbors()
        now = time.time()

        # Build sortable list from zero-hop repeaters only: (pubkey_hex, heard_seconds_ago, snr)
        entries = []
        for pubkey_hex, info in raw_neighbors.items():
            is_repeater = bool(info.get("is_repeater", False))
            zero_hop = bool(info.get("zero_hop", False))
            if not (is_repeater and zero_hop):
                continue

            last_seen = info.get("last_seen", 0) or 0
            heard_ago = max(0, int(now - last_seen))
            snr_raw = info.get("snr", 0) or 0
            # Store SNR as int8 (firmware stores snr * 4 as int8)
            snr_int = max(-128, min(127, int(snr_raw * 4)))
            entries.append((pubkey_hex, heard_ago, snr_int))

        # Sort (matches C++ order_by values)
        if order_by == 0:
            entries.sort(key=lambda e: e[1])  # newest first (smallest heard_ago)
        elif order_by == 1:
            entries.sort(key=lambda e: e[1], reverse=True)  # oldest first
        elif order_by == 2:
            entries.sort(key=lambda e: e[2], reverse=True)  # strongest SNR first
        elif order_by == 3:
            entries.sort(key=lambda e: e[2])  # weakest SNR first

        total_count = len(entries)

        # Paginate
        entry_size = pubkey_prefix_len + 4 + 1
        max_results_bytes = 130  # firmware buffer limit
        results = bytearray()
        results_count = 0

        for i in range(count):
            idx = i + offset
            if idx >= total_count:
                break
            if len(results) + entry_size > max_results_bytes:
                break

            pubkey_hex, heard_ago, snr_int = entries[idx]
            try:
                pubkey_bytes = bytes.fromhex(pubkey_hex)
            except (ValueError, TypeError):
                continue
            results.extend(pubkey_bytes[:pubkey_prefix_len])
            results.extend(struct.pack("<I", heard_ago))
            results.append(snr_int & 0xFF)
            results_count += 1

        header = struct.pack("<HH", total_count, results_count)

        logger.debug(
            "GET_NEIGHBOURS: total=%d, returned=%d, offset=%d, order=%d, requested=%d",
            total_count,
            results_count,
            offset,
            order_by,
            count,
        )
        return header + bytes(results)

    def _handle_get_owner_info(self, client, timestamp: int, req_data: bytes):
        """Return firmware version, node name, and owner info.

        Matches C++ simple_repeater: sprintf("%s\\n%s\\n%s", FIRMWARE_VERSION, node_name, owner_info)
        """
        repeater_cfg = self.config.get("repeater", {})
        node_name = repeater_cfg.get("node_name", "openhop-repeater")
        owner_info = repeater_cfg.get("owner_info", "")

        # Version: use package version if available, fallback to "pyMC"
        try:
            from importlib.metadata import version as pkg_version

            fw_version = pkg_version("openhop-repeater")
        except Exception:
            fw_version = "pyMC"

        result = f"{fw_version}\n{node_name}\n{owner_info}".encode("utf-8")
        logger.debug("GET_OWNER_INFO: %s", result.decode("utf-8", errors="replace"))
        return result
