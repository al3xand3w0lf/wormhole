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


def _int_bits(value: int, width: int) -> str:
    if value < 0:
        value = (1 << width) + value
    return format(value, f"0{width}b")


def rtcm3_1005(station_id: int, x: float, y: float, z: float) -> bytes:
    """A real RTCM 1005 (Stationary RTK Reference Station ARP), valid CRC,
    with a decodable DF003 and DF025/026/027 (ECEF, metres) - the field
    layout streaming/rover_discovery.py's _decode_arp() reads.

    Field widths from pyrtcm.rtcmtypes_core.RTCM_DATA_FIELDS (DF025/026/027
    are signed 38-bit integers, scale 0.0001 - i.e. units of 0.1 mm, the same
    kind of unit trap streaming/geo.py's docstring warns about for NAV-PVT).
    Every field this test does not care about (DF021/022/023/024/141/142/
    DF001_1/DF364) is zeroed - 1005 is fixed-length regardless.
    """
    bits = (
        format(1005, "012b") + format(station_id, "012b")
        + "0" * 6 + "0" * 4                       # DF021, DF022-DF141
        + _int_bits(round(x * 10000), 38)         # DF025
        + "00"                                     # DF142, DF001_1
        + _int_bits(round(y * 10000), 38)         # DF026
        + "00"                                     # DF364
        + _int_bits(round(z * 10000), 38)         # DF027
    )
    assert len(bits) == 152
    payload = int(bits, 2).to_bytes(19, "big")
    body = bytes([0xD3, 0x00, 19]) + payload
    return body + crc2bytes(body)


def rawx(week: int, rcv_tow: float, leap_s: int = 18) -> bytes:
    """UBX-RXM-RAWX with no measurement blocks (numMeas = 0).

    Header layout: rcvTow R8 | week U2 | leapS I1 | numMeas U1 | recStat X1 | reserved U3
    """
    payload = struct.pack("<dHbBB3s", rcv_tow, week, leap_s, 0, 0, b"\x00\x00\x00")
    assert len(payload) == 16
    return ubx(RXM_CLASS, RXM_RAWX_ID, payload)


def ident(station_id: int, role: int = 0) -> bytes:
    # Matches the firmware layout: <I station_id><B role><3x reserved>. role
    # defaults to 0 (ROLE_UNSET) so every pre-existing caller stays unchanged.
    return ubx(PRIVATE_CLASS, ID_IDENT, struct.pack("<IB3x", station_id, role))


def heartbeat() -> bytes:
    return ubx(PRIVATE_CLASS, ID_HEARTBEAT, b"")


def cli_response(text: str, last: bool) -> bytes:
    return ubx(PRIVATE_CLASS, ID_CLI_RESPONSE, bytes([1 if last else 0]) + text.encode())


def sensor(msg_id: int, fmt: str, rtc_unix: int, *values: int) -> bytes:
    return ubx(PRIVATE_CLASS, msg_id, struct.pack(fmt, rtc_unix, *values))
