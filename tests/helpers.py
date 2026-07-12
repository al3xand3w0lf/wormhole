"""Synthetic frame builders mirroring the device firmware."""

import struct

from pyrtcm import crc2bytes

from streaming.frames import (
    PRIVATE_CLASS,
    ID_CLI_RESPONSE,
    ID_HEARTBEAT,
    ID_IDENT,
    build_ubx,
)

RXM_CLASS = 0x02
RXM_RAWX_ID = 0x15


def ubx(cls_: int, id_: int, payload: bytes = b"") -> bytes:
    return build_ubx(cls_, id_, payload)


def rtcm3(payload: bytes) -> bytes:
    """Build a valid RTCM3 transport frame (CRC via pyrtcm)."""
    n = len(payload)
    assert n <= 1023
    body = bytes([0xD3, (n >> 8) & 0x03, n & 0xFF]) + payload
    return body + crc2bytes(body)


def rawx(week: int, rcv_tow: float, leap_s: int = 18) -> bytes:
    """UBX-RXM-RAWX with no measurement blocks (numMeas = 0).

    Header layout: rcvTow R8 | week U2 | leapS I1 | numMeas U1 | recStat X1 | reserved U3
    """
    payload = struct.pack("<dHbBB3s", rcv_tow, week, leap_s, 0, 0, b"\x00\x00\x00")
    assert len(payload) == 16
    return ubx(RXM_CLASS, RXM_RAWX_ID, payload)


def ident(station_id: int) -> bytes:
    return ubx(PRIVATE_CLASS, ID_IDENT, struct.pack("<I", station_id) + b"\x00" * 4)


def heartbeat() -> bytes:
    return ubx(PRIVATE_CLASS, ID_HEARTBEAT, b"")


def cli_response(text: str, last: bool) -> bytes:
    return ubx(PRIVATE_CLASS, ID_CLI_RESPONSE, bytes([1 if last else 0]) + text.encode())


def sensor(msg_id: int, fmt: str, rtc_unix: int, *values: int) -> bytes:
    return ubx(PRIVATE_CLASS, msg_id, struct.pack(fmt, rtc_unix, *values))
