"""Python bindings for sx1302_hal C library using ctypes"""

import ctypes
import os
from ctypes import (
    Structure,
    POINTER,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_int8,
    c_int16,
    c_int32,
    c_float,
    c_bool,
    c_char,
)

# Load the shared library
_lib_path = os.path.join(
    os.path.dirname(__file__), "../../sx1302_hal/libloragw/libloragw.so"
)
_lib = ctypes.CDLL(os.path.abspath(_lib_path))

# Constants
LGW_HAL_SUCCESS = 0
LGW_HAL_ERROR = -1

# Modulation types
MOD_UNDEFINED = 0
MOD_LORA = 0x10
MOD_FSK = 0x20

# Bandwidth
BW_125KHZ = 0x04
BW_250KHZ = 0x05
BW_500KHZ = 0x06

# Spreading factors
DR_LORA_SF5 = 5
DR_LORA_SF6 = 6
DR_LORA_SF7 = 7
DR_LORA_SF8 = 8
DR_LORA_SF9 = 9
DR_LORA_SF10 = 10
DR_LORA_SF11 = 11
DR_LORA_SF12 = 12

# Coding rates
CR_LORA_4_5 = 0x01
CR_LORA_4_6 = 0x02
CR_LORA_4_7 = 0x03
CR_LORA_4_8 = 0x04

# TX modes
IMMEDIATE = 0
TIMESTAMPED = 1

# Radio types
LGW_RADIO_TYPE_SX1250 = 5

# COM types (from loragw_com.h)
LGW_COM_SPI = 0
LGW_COM_USB = 1

# RX packet CRC status (from loragw_hal.h)
STAT_NO_CRC  = 0x01  # CRC not present
STAT_CRC_BAD = 0x11  # CRC present but failed
STAT_CRC_OK  = 0x10  # CRC present and valid

# Spectral scan (SX1261 companion chip)
LGW_SPECTRAL_SCAN_RESULT_SIZE  = 33

SPECTRAL_SCAN_STATUS_NONE      = 0
SPECTRAL_SCAN_STATUS_ONGOING   = 1
SPECTRAL_SCAN_STATUS_ABORTED   = 2
SPECTRAL_SCAN_STATUS_COMPLETED = 3


# Structures
class lgw_conf_board_s(Structure):
    _pack_ = 1  # Prevent automatic padding
    _fields_ = [
        ("lorawan_public", c_bool),
        ("clksrc", c_uint8),
        ("full_duplex", c_bool),
        ("_pad1", c_uint8),  # Padding before enum
        ("com_type", c_uint32),  # lgw_com_type_t is enum (4 bytes)
        ("com_path", c_char * 64),
    ]


class lgw_rssi_tcomp_s(Structure):
    _fields_ = [
        ("coeff_a", c_float),
        ("coeff_b", c_float),
        ("coeff_c", c_float),
        ("coeff_d", c_float),
        ("coeff_e", c_float),
    ]


class lgw_conf_rxrf_s(Structure):
    _pack_ = 1  # Prevent automatic padding
    _fields_ = [
        ("enable", c_bool),
        ("_pad1", c_uint8 * 3),  # Explicit padding for alignment
        ("freq_hz", c_uint32),
        ("rssi_offset", c_float),
        ("rssi_tcomp", lgw_rssi_tcomp_s),
        ("type", c_uint32),  # enum is 4 bytes
        ("tx_enable", c_bool),
        ("single_input_mode", c_bool),
        ("_pad2", c_uint8 * 2),  # Explicit padding at end
    ]


class lgw_conf_rxif_s(Structure):
    _fields_ = [
        ("enable", c_bool),
        ("rf_chain", c_uint8),
        ("freq_hz", c_int32),
        ("bandwidth", c_uint8),
        ("datarate", c_uint32),
        ("sync_word_size", c_uint8),
        ("sync_word", c_uint64),
        ("implicit_hdr", c_bool),
        ("implicit_payload_length", c_uint8),
        ("implicit_crc_en", c_bool),
        ("implicit_coderate", c_uint8),
    ]


class lgw_pkt_rx_s(Structure):
    _fields_ = [
        ("freq_hz", c_uint32),
        ("freq_offset", c_int32),
        ("if_chain", c_uint8),
        ("status", c_uint8),
        ("count_us", c_uint32),
        ("rf_chain", c_uint8),
        ("modem_id", c_uint8),
        ("modulation", c_uint8),
        ("bandwidth", c_uint8),
        ("datarate", c_uint32),
        ("coderate", c_uint8),
        ("rssic", c_float),
        ("rssis", c_float),
        ("snr", c_float),
        ("snr_min", c_float),
        ("snr_max", c_float),
        ("crc", c_uint16),
        ("size", c_uint16),
        ("payload", c_uint8 * 256),
        ("ftime_received", c_bool),
        ("ftime", c_uint32),
    ]


class lgw_pkt_tx_s(Structure):
    _fields_ = [
        ("freq_hz", c_uint32),
        ("tx_mode", c_uint8),
        ("count_us", c_uint32),
        ("rf_chain", c_uint8),
        ("rf_power", c_int8),
        ("modulation", c_uint8),
        ("freq_offset", c_int8),
        ("bandwidth", c_uint8),
        ("datarate", c_uint32),
        ("coderate", c_uint8),
        ("invert_pol", c_bool),
        ("f_dev", c_uint8),
        ("preamble", c_uint16),
        ("no_crc", c_bool),
        ("no_header", c_bool),
        ("size", c_uint16),
        ("payload", c_uint8 * 256),
    ]


# SX1261 / LBT structures (verify alignment against loragw_hal.h if upgrading sx1302_hal)
class lgw_conf_lbt_chan_s(Structure):
    _fields_ = [
        ("freq_hz",          c_uint32),
        ("bandwidth",        c_uint8),
        ("_pad",             c_uint8 * 3),
        ("scan_time_us",     c_uint32),
        ("transmit_time_ms", c_uint16),
        ("_pad2",            c_uint8 * 2),
    ]


class lgw_conf_lbt_s(Structure):
    _fields_ = [
        ("enable",      c_bool),
        ("rssi_target", c_int8),
        ("nb_channel",  c_uint8),
        ("channels",    lgw_conf_lbt_chan_s * 8),
    ]


class lgw_conf_sx1261_s(Structure):
    _fields_ = [
        ("enable",      c_bool),
        ("spi_path",    c_char * 64),
        ("rssi_offset", c_int8),
        ("lbt_conf",    lgw_conf_lbt_s),
    ]


# Function prototypes
_lib.lgw_board_setconf.argtypes = [POINTER(lgw_conf_board_s)]
_lib.lgw_board_setconf.restype = c_int32

_lib.lgw_rxrf_setconf.argtypes = [c_uint8, POINTER(lgw_conf_rxrf_s)]
_lib.lgw_rxrf_setconf.restype = c_int32

_lib.lgw_rxif_setconf.argtypes = [c_uint8, POINTER(lgw_conf_rxif_s)]
_lib.lgw_rxif_setconf.restype = c_int32

_lib.lgw_start.argtypes = []
_lib.lgw_start.restype = c_int32

_lib.lgw_stop.argtypes = []
_lib.lgw_stop.restype = c_int32

_lib.lgw_receive.argtypes = [c_uint8, POINTER(lgw_pkt_rx_s)]
_lib.lgw_receive.restype = c_int32

_lib.lgw_send.argtypes = [POINTER(lgw_pkt_tx_s)]
_lib.lgw_send.restype = c_int32

_lib.lgw_status.argtypes = [c_uint8, c_uint8, POINTER(c_uint8)]
_lib.lgw_status.restype = c_int32

_lib.lgw_sx1261_setconf.argtypes = [POINTER(lgw_conf_sx1261_s)]
_lib.lgw_sx1261_setconf.restype = c_int32

_lib.lgw_spectral_scan_start.argtypes = [c_uint32, c_uint16]
_lib.lgw_spectral_scan_start.restype = c_int32

_lib.lgw_spectral_scan_get_status.argtypes = [POINTER(c_int32)]
_lib.lgw_spectral_scan_get_status.restype = c_int32

_lib.lgw_spectral_scan_get_results.argtypes = [
    POINTER(c_int16 * LGW_SPECTRAL_SCAN_RESULT_SIZE),
    POINTER(c_uint16 * LGW_SPECTRAL_SCAN_RESULT_SIZE),
]
_lib.lgw_spectral_scan_get_results.restype = c_int32

_lib.lgw_spectral_scan_abort.argtypes = []
_lib.lgw_spectral_scan_abort.restype = c_int32


# Wrapper functions
def lgw_board_setconf(conf):
    return _lib.lgw_board_setconf(ctypes.byref(conf))


def lgw_rxrf_setconf(rf_chain, conf):
    return _lib.lgw_rxrf_setconf(rf_chain, ctypes.byref(conf))


def lgw_rxif_setconf(if_chain, conf):
    return _lib.lgw_rxif_setconf(if_chain, ctypes.byref(conf))


def lgw_start():
    return _lib.lgw_start()


def lgw_stop():
    return _lib.lgw_stop()


def lgw_receive(max_pkt=8):
    pkt_data = (lgw_pkt_rx_s * max_pkt)()
    nb_pkt = _lib.lgw_receive(max_pkt, pkt_data)
    if nb_pkt < 0:
        return []
    return [pkt_data[i] for i in range(nb_pkt)]


def lgw_send(pkt):
    return _lib.lgw_send(ctypes.byref(pkt))


def lgw_status(rf_chain, select):
    code = c_uint8()
    result = _lib.lgw_status(rf_chain, select, ctypes.byref(code))
    return result, code.value


def lgw_sx1261_setconf(conf):
    return _lib.lgw_sx1261_setconf(ctypes.byref(conf))


def lgw_spectral_scan_start(freq_hz, nb_scan):
    return _lib.lgw_spectral_scan_start(freq_hz, nb_scan)


def lgw_spectral_scan_get_status(status_ptr):
    return _lib.lgw_spectral_scan_get_status(status_ptr)


def lgw_spectral_scan_get_results(levels_ptr, counts_ptr):
    return _lib.lgw_spectral_scan_get_results(levels_ptr, counts_ptr)


def lgw_spectral_scan_abort():
    return _lib.lgw_spectral_scan_abort()
