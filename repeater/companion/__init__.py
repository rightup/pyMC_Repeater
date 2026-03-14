"""Companion identity support for pyMC Repeater.

Exposes the MeshCore companion frame protocol over TCP for standard clients.
"""

from .bridge import RepeaterCompanionBridge
from .constants import (
    CMD_APP_START,
    CMD_GET_CONTACTS,
    CMD_SEND_LOGIN,
    CMD_SEND_TXT_MSG,
    CMD_SYNC_NEXT_MESSAGE,
    PUSH_CODE_MSG_WAITING,
    RESP_CODE_ERR,
    RESP_CODE_OK,
)
from .frame_server import CompanionFrameServer

__all__ = [
    "CompanionFrameServer",
    "RepeaterCompanionBridge",
    "CMD_APP_START",
    "CMD_GET_CONTACTS",
    "CMD_SEND_TXT_MSG",
    "CMD_SYNC_NEXT_MESSAGE",
    "CMD_SEND_LOGIN",
    "RESP_CODE_OK",
    "RESP_CODE_ERR",
    "PUSH_CODE_MSG_WAITING",
]
