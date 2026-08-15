"""The device's private UBX frames (class 0xF0).

0xF0 is unused in the public u-blox class range, so the device borrows it for its
own messages. Being a private protocol, we decode it here rather than leaning on
pyubx2's NOMINAL fallback for unknown classes. The frame *envelope* is standard
UBX, so it rides the same sync-byte scan as real UBX/RTCM3 and is checksum-protected.

**To adapt this to your own device, change the ids and `SENSOR_SPECS` below** —
the sensor set here is an example (two current monitors, two accelerometers, a
humidity and a pressure sensor), not part of the transport. A new sensor needs
a new id here **and** an entry in `SENSOR_SPECS`, matching the firmware's
payload struct.

Wire format (little-endian, packed):

    0x01  SENSOR_INA219        dev->srv   <Iiii  rtc_unix, mV, mA, mW        (internal)
    0x02  SENSOR_ADXL345       dev->srv   <Iiii  rtc_unix, x_ug, y_ug, z_ug  (internal)
    0x03  IDENT                dev->srv   <IB3x  stationId, role (0=unset 1=base 2=rover
                                                  3=logger 4=stream) + 3 reserved
    0x04  HEARTBEAT            dev->srv   (empty)
    0x05  CLI_RESPONSE         dev->srv   [more_flag u8][text]  0=more, 1=last
    0x06  CMD_REQUEST          srv->dev   [tok_len u8][token][cmd]
    0x07  SENSOR_SHT4X         dev->srv   <Iii   rtc_unix, temp_mC, rh_mpct
    0x08  SENSOR_LPS28DFW      dev->srv   <Iii   rtc_unix, press_Pa, temp_mC
    0x09  SENSOR_INA219_EXT    dev->srv   <Iiii  (external pod)
    0x0A  SENSOR_ADXL345_EXT   dev->srv   <Iiii  (external pod)
    0x0B  FILE_REQUEST         dev->srv   [flags u8][name_len u8][name]
    0x0C  FILE_BEGIN           srv->dev   [total u32][crc32 u32][name_len u8][name]
    0x0D  FILE_STATUS          dev->srv   [phase u8][code i8][bytes u32]
    0x0E  FILE_UP_BEGIN        dev->srv   [total u32][crc32 u32][name_len u8][name]
    0x0F  FILE_UP_DATA         dev->srv   [seq u16][bytes]
    0x10  RTCM_DATA            srv->dev   exactly ONE whole RTCM3 frame
    0x11  RTCM_INFO            srv->dev   [base_id u16][flags u8]

RTK rover downlink (streaming/rover.py): **one RTCM3 frame per envelope, never
two and never half of one.** That contract is what lets the device stay free of
any RTCM3 understanding — it strips the envelope and hands the payload to the
receiver unchanged; type, length and CRC-24Q are the receiver's business.
Breaking it does not produce an error anywhere, it produces a rover that
silently never fixes.

IDENT's role byte is what lets streaming/rover_discovery.py auto-subscribe a
rover with no hand-edited config — see that module's docstring. On the device
side it derives naturally from whatever mode selector your firmware already
has (batch vs. streaming, base vs. rover); it does not need a new config key of
its own. A missing/zero byte (old firmware, or a payload too short to carry
it) decodes as ROLE_UNSET, never as ROLE_BASE — an auto-created correction
source requires an explicit ROLE_BASE, not silence.

File transfer: after FILE_BEGIN the server writes exactly `total` RAW bytes
into the socket — no envelope, no per-chunk header. The device does not need
one if its own modem read call is itself length-prefixed, so the byte count
already comes from there on every read. `total = 0` means "not found".

The upload direction is mirrored, not symmetric: it stays in ordinary framed
0x0F messages, because here the *receiver* is this server, which already has a
framer. Each side does the job it can already do.
"""

import struct
from dataclasses import dataclass, field

from pyubx2 import calc_checksum

PRIVATE_CLASS = 0xF0

# IDENT role byte (payload offset 4). 0 is deliberately "unset", not "base": a
# short/old-firmware payload must never be mistaken for an explicit base
# declaration. See the module docstring above for the device_mode_t mapping.
ROLE_UNSET = 0
ROLE_BASE = 1
ROLE_ROVER = 2
ROLE_LOGGER = 3
ROLE_STREAM = 4

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
ID_FILE_REQUEST = 0x0B
ID_FILE_BEGIN = 0x0C
ID_FILE_STATUS = 0x0D
ID_FILE_UP_BEGIN = 0x0E
ID_FILE_UP_DATA = 0x0F
ID_RTCM_DATA = 0x10
ID_RTCM_INFO = 0x11

# An RTCM3 frame is 3 B header + up to 1023 B payload + 3 B CRC-24Q. Size your
# device's inbound buffer to the same ceiling, or anything above this could
# never be reassembled there.
RTCM3_MAX_FRAME = 1029

# FILE_STATUS phases (mirror whatever enum your firmware uses for this)
FILE_PHASE_ACCEPTED = 0
FILE_PHASE_DONE = 1
FILE_PHASE_ABORTED = 2

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
    # ROLE_UNSET for any firmware that doesn't send the byte (short payload) or
    # sends 0 explicitly — the two are indistinguishable on the wire and treated
    # identically: no auto-registration as a correction source either way.
    role: int = ROLE_UNSET


@dataclass(frozen=True)
class Heartbeat:
    pass


@dataclass(frozen=True)
class CliResponse:
    text: str
    last: bool  # True = final chunk of this response


@dataclass(frozen=True)
class FileRequest:
    name: str
    flags: int = 0


@dataclass(frozen=True)
class FileUpBegin:
    total: int
    crc32: int
    name: str


@dataclass(frozen=True)
class FileUpData:
    seq: int
    data: bytes


@dataclass(frozen=True)
class FileStatus:
    phase: int   # FILE_PHASE_*
    code: int    # 1 = ok, negative = the device's error code
    bytes_: int


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


def encode_rtcm_data(raw: bytes) -> bytes:
    """Wrap ONE whole RTCM3 frame in an RTCM_DATA envelope.

    Refuses an oversized frame rather than truncating it: the device would
    reassemble a half frame, hand it to the receiver, and get a CRC failure that
    looks like a link fault instead of a sizing bug. Nothing legitimate can
    exceed the RTCM3 ceiling, so this firing at all is a defect in the caller.
    """
    if not raw or len(raw) > RTCM3_MAX_FRAME:
        raise ValueError(f"not one RTCM3 frame: {len(raw)} bytes")
    return build_ubx(PRIVATE_CLASS, ID_RTCM_DATA, raw)


def encode_rtcm_info(base_id: int, flags: int = 0) -> bytes:
    """Tell the device which base its corrections are coming from."""
    return build_ubx(PRIVATE_CLASS, ID_RTCM_INFO,
                     struct.pack("<HB", base_id & 0xFFFF, flags & 0xFF))


def encode_file_begin(total: int, crc32: int, name: str) -> bytes:
    """Build a FILE_BEGIN frame. total=0 tells the device the file is missing."""
    nm = name.encode("ascii", errors="replace")[:255]
    payload = struct.pack("<IIB", total, crc32 & 0xFFFFFFFF, len(nm)) + nm
    return build_ubx(PRIVATE_CLASS, ID_FILE_BEGIN, payload)


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
        # Byte 4 (role) is new; a shorter payload is old firmware, not malformed
        # — fall back to ROLE_UNSET rather than rejecting the whole IDENT.
        role = payload[4] if len(payload) >= 5 else ROLE_UNSET
        return Ident(station_id, role)

    if msg_id == ID_CLI_RESPONSE:
        if len(payload) < 1:
            return None
        last = payload[0] == 1
        text = payload[1:].decode("ascii", errors="replace")
        return CliResponse(text, last)

    if msg_id == ID_FILE_REQUEST:
        if len(payload) < 2:
            return None
        flags = payload[0]
        name_len = payload[1]
        if len(payload) < 2 + name_len:
            return None
        name = payload[2 : 2 + name_len].decode("ascii", errors="replace")
        return FileRequest(name, flags)

    if msg_id == ID_FILE_UP_BEGIN:
        if len(payload) < 9:
            return None
        total, crc, name_len = struct.unpack_from("<IIB", payload, 0)
        if len(payload) < 9 + name_len:
            return None
        name = payload[9 : 9 + name_len].decode("ascii", errors="replace")
        return FileUpBegin(total, crc, name)

    if msg_id == ID_FILE_UP_DATA:
        if len(payload) < 2:
            return None
        (seq,) = struct.unpack_from("<H", payload, 0)
        return FileUpData(seq, bytes(payload[2:]))

    if msg_id == ID_FILE_STATUS:
        if len(payload) < 6:
            return None
        phase, code, nbytes = struct.unpack_from("<BbI", payload, 0)
        return FileStatus(phase, code, nbytes)

    spec = SENSOR_SPECS.get(msg_id)
    if spec is not None:
        stream, fmt, names = spec
        if len(payload) < struct.calcsize(fmt):
            return None
        parts = struct.unpack_from(fmt, payload, 0)
        return SensorReading(stream, parts[0], dict(zip(names, parts[1:])))

    return None
