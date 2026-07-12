"""The device's private UBX frames (class 0xF0).

0xF0 is unused in the public u-blox class range, so the device borrows it for its
own messages. Being a private protocol, we decode it here rather than leaning on
pyubx2's NOMINAL fallback for unknown classes. The frame *envelope* is standard
UBX, so it rides the same sync-byte scan as real UBX/RTCM3 and is checksum-protected.

**To adapt this to your own device, change the ids and `SENSOR_SPECS` below** —
the sensor set here is an example (two current monitors, two accelerometers, a
humidity and a pressure sensor), not part of the transport.

Wire format (little-endian, packed):

    0x01  SENSOR_INA219        dev->srv   <Iiii  rtc_unix, mV, mA, mW        (internal)
    0x02  SENSOR_ADXL345       dev->srv   <Iiii  rtc_unix, x_ug, y_ug, z_ug  (internal)
    0x03  IDENT                dev->srv   <I4x   stationId + 4 reserved
    0x04  HEARTBEAT            dev->srv   (empty)
    0x05  CLI_RESPONSE         dev->srv   [more_flag u8][text]  0=more, 1=last
    0x06  CMD_REQUEST          srv->dev   [tok_len u8][token][cmd]
    0x07  SENSOR_SHT4X         dev->srv   <Iii   rtc_unix, temp_mC, rh_mpct
    0x08  SENSOR_LPS28DFW      dev->srv   <Iii   rtc_unix, press_Pa, temp_mC
    0x09  SENSOR_INA219_EXT    dev->srv   <Iiii  (external pod)
    0x0A  SENSOR_ADXL345_EXT   dev->srv   <Iiii  (external pod)
"""

import struct
from dataclasses import dataclass, field

from pyubx2 import calc_checksum

PRIVATE_CLASS = 0xF0

ID_SENSOR_INA219 = 0x01
ID_SENSOR_ADXL345 = 0x02
ID_IDENT = 0x03
ID_HEARTBEAT = 0x04
ID_CLI_RESPONSE = 0x05
ID_CMD_REQUEST = 0x06
ID_SENSOR_SHT4X = 0x07
ID_SENSOR_LPS28DFW = 0x08
ID_SENSOR_INA219_EXT = 0x09
ID_SENSOR_ADXL345_EXT = 0x0A

# id -> (stream name used for filenames/CSV, struct format, value field names)
SENSOR_SPECS = {
    ID_SENSOR_INA219: ("ina219", "<Iiii", ("mV", "mA", "mW")),
    ID_SENSOR_INA219_EXT: ("ina219ext", "<Iiii", ("mV", "mA", "mW")),
    ID_SENSOR_ADXL345: ("accel", "<Iiii", ("x_ug", "y_ug", "z_ug")),
    ID_SENSOR_ADXL345_EXT: ("accelext", "<Iiii", ("x_ug", "y_ug", "z_ug")),
    ID_SENSOR_SHT4X: ("sht4x", "<Iii", ("temp_mC", "rh_mpct")),
    ID_SENSOR_LPS28DFW: ("lps28", "<Iii", ("press_Pa", "temp_mC")),
}

SENSOR_STREAMS = tuple(spec[0] for spec in SENSOR_SPECS.values())


@dataclass(frozen=True)
class Ident:
    station_id: int


@dataclass(frozen=True)
class Heartbeat:
    pass


@dataclass(frozen=True)
class CliResponse:
    text: str
    last: bool  # True = final chunk of this response


@dataclass(frozen=True)
class SensorReading:
    stream: str  # "ina219" | "ina219ext" | "accel" | "accelext" | "sht4x" | "lps28"
    rtc_unix: int
    values: dict = field(default_factory=dict)


def ubx_payload(raw: bytes) -> bytes:
    """Extract the payload from a complete, already-validated UBX frame."""
    plen = raw[4] | (raw[5] << 8)
    return raw[6 : 6 + plen]


def build_ubx(cls_: int, id_: int, payload: bytes) -> bytes:
    """Wrap a payload in a UBX envelope with a valid checksum."""
    n = len(payload)
    body = bytes([cls_, id_, n & 0xFF, (n >> 8) & 0xFF]) + payload
    return b"\xb5\x62" + body + calc_checksum(body)


def encode_cmd_request(cmd: str, token: str = "") -> bytes:
    """Build a CMD_REQUEST frame: [tok_len][token][cmd].

    The tok_len prefix is mandatory even when no secret is configured (tok_len = 0),
    so the wire format stays uniform — the device always strips it.
    """
    tok = token.encode("ascii")
    if len(tok) > 255:
        raise ValueError("CLI token too long (max 255 bytes)")
    payload = bytes([len(tok)]) + tok + cmd.encode("ascii")
    return build_ubx(PRIVATE_CLASS, ID_CMD_REQUEST, payload)


def decode_private(raw: bytes):
    """Decode a private 0xF0 frame. Returns None for unknown/malformed frames."""
    msg_id = raw[3]
    payload = ubx_payload(raw)

    if msg_id == ID_HEARTBEAT:
        return Heartbeat()

    if msg_id == ID_IDENT:
        if len(payload) < 4:
            return None
        (station_id,) = struct.unpack_from("<I", payload, 0)
        return Ident(station_id)

    if msg_id == ID_CLI_RESPONSE:
        if len(payload) < 1:
            return None
        last = payload[0] == 1
        text = payload[1:].decode("ascii", errors="replace")
        return CliResponse(text, last)

    spec = SENSOR_SPECS.get(msg_id)
    if spec is not None:
        stream, fmt, names = spec
        if len(payload) < struct.calcsize(fmt):
            return None
        parts = struct.unpack_from(fmt, payload, 0)
        return SensorReading(stream, parts[0], dict(zip(names, parts[1:])))

    return None
