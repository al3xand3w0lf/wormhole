"""The device's private UBX frames (class 0xF0).

0xF0 is unused in the public u-blox class range, so the device borrows it for its
own messages. Being a private protocol, we decode it here rather than leaning on
pyubx2's NOMINAL fallback for unknown classes. The frame *envelope* is standard
UBX, so it rides the same sync-byte scan as real UBX/RTCM3 and is checksum-protected.

**To adapt this to your own device, change the ids and `SENSOR_SPECS` below** —
the sensor set here is an example, not part of the transport. A new sensor needs
a new id here **and** an entry in `SENSOR_SPECS`, matching the firmware's
payload struct.

Wire format (little-endian, packed):

    0x01  SENSOR_INA219        dev->srv   <Iiii  rtc_unix, mV, mA, mW        (internal)
    0x02  SENSOR_ADXL345       dev->srv   <Iiii  rtc_unix, x_ug, y_ug, z_ug  (internal)
    0x03  IDENT                dev->srv   <IB3x  stationId, role (0=unset 1=base 2=rover
                                                  3=logger 4=stream 5=rover_ntrip)
                                                  + 3 reserved
                                                  + [name_len u8][station_name] (FW 1.69.x+,
                                                  RAW text, <=31 B, sanitised by stationdir.py)
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
    0x12  NMEA_GGA             dev->srv   raw NMEA-GGA line, ASCII, no CRLF
                                            (may EXCEED the NMEA-0183 82-char
                                            cap - see the note below)
    0x13  SENSOR_WN90LP        dev->srv   <Iiiiiiiiiiiii  rtc_unix, source,
                                            light_lux, uvi_d, temp_mC, rh_mpct,
                                            wind_mms, gust_mms, dir_deg,
                                            rain_um, press_Pa, rain_ctr_um,
                                            batt_mV, cap_mV  (a WN90LP weather sensor
                                            over Modbus RTU; 56-byte payload)
    0x14  GNSS_TUNNEL_DOWN     srv->dev   raw bytes for the ZED UART
    0x15  GNSS_TUNNEL_UP       dev->srv   raw bytes from the ZED UART
    0x16  SYSLOG_LINE          dev->srv   [yy mm dd hh mi ss level u8][text]
                                            one device syslog entry, yy = year-2000
    0x17  FILE_UP_ACK          srv->dev   [result i8][crc32 u32]  verdict on an
                                            upload; the device deletes its SD
                                            copy only on result 1

⚠️ `source` is provenance, not a measurement: 0 = a sample the producer took
itself at its own rate, 1 = a per-minute value recovered from the sensor's own
30-minute history after an outage. They are NOT the same quantity and must not
be mixed silently. In the history block `wind_mms` is a per-minute MEAN and
`gust_mms` a per-minute MAXIMUM, while a live row carries instantaneous
readings of both - three different statistics under two column names. Any
consumer computing rates, gusts or extremes has to split on this column first.
INT32_MIN here means the producer did not say.

⚠️ WN90LP fields use INT32_MIN as "no reading", NOT 0. The sensor reports
0xFFFF per register for an invalid measurement; a station forwarding that as 0
would turn a missing wind sample into recorded calm, and a missing pressure
into 0 hPa. Decoding keeps the sentinel verbatim - it is the sink's and the
consumer's business to render it as empty, and nothing on this path may
substitute a zero. The producer that reads your sensor must never invent a
value either.

⚠️ A NMEA_GGA payload is NOT bounded by the NMEA-0183 limit of 82 characters.
A rover running with CFG-NMEA-HIGHPREC sends such sentences, which
u-blox documents as breaking that limit on purpose: 7 decimals of minutes for
lat/lon (~0.2 mm) and 3 for altitude, against 5/2 (~1.8 cm) before. A fixed
rover sentence with age and reference-station ID measures 88 characters. The
sentence is stored and forwarded VERBATIM and nothing on this path may bound,
truncate or re-format it - such a check would drop exactly the sentences from
the stations that reached an RTK fix, and nowhere else. Regression tests:
tests/test_frames.py and tests/test_sinks.py, both on a real 88-char line.

RTK rover downlink (streaming/rover.py): **one RTCM3 frame per envelope, never
two and never half of one.** That contract is what lets the STM32 stay free of
any RTCM3 understanding — it strips the envelope and hands the payload to the
receiver unchanged; type, length and CRC-24Q are the receiver's business.
Breaking it does not produce an error anywhere, it produces a rover that
silently never fixes.

IDENT's role byte (see your device's firmware changelog) is what lets
streaming/rover_discovery.py auto-subscribe a rover with no hand-edited config.
In the reference firmware it is derived from the
device's own `device_mode_t` (not a new CONFIG.TXT key), and the mapping is not
1:1: `DEVICE_MODE_BATCH` (0) would collide with "unset" (old firmware always
sends 0 here) if cast directly, so the device maps
BATCH->3(logger), STREAM_PLAIN->4(stream), STREAM_BASE->1(base),
STREAM_ROVER->2(rover), BATCH_DUTY->3(logger),
NTRIP_ROVER->5(rover_ntrip). A missing/zero byte (old firmware, or a payload
too short to carry it) decodes as ROLE_UNSET, never as ROLE_BASE — an
auto-created correction source requires an explicit ROLE_BASE, not silence.

⚠️ ROLE_ROVER_NTRIP is a rover that fetches its corrections from an NTRIP
caster itself and DISCARDS anything we push it. It is deliberately not
ROLE_ROVER: given that value it would pass every candidacy test in
rover_discovery.py, occupy a subscription slot, and make /stream/rover report a
subscription that does nothing. Two things that must be treated differently
need different bytes, even when they are the same kind of station.

**Never test a role by exclusion.** Everything here matches an exact value, so
a role added on the device side is a non-candidate until someone decides
otherwise — which is the only reason ROLE_ROVER_NTRIP was handled correctly on
the day the firmware shipped it. `!= ROLE_BASE` or "anything rover-ish" would
have auto-subscribed it.

File transfer: after FILE_BEGIN the server writes
exactly `total` RAW bytes into the socket — no envelope, no per-chunk header.
The device does not need one: AT+QIRD is itself length-prefixed, so the byte
count already comes from its modem on every read. `total = 0` means "not found".

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
ROLE_ROVER_NTRIP = 5

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
ID_NMEA_GGA = 0x12
ID_SENSOR_WN90LP = 0x13
# GNSS maintenance tunnel (K-01 Etappe 2). Raw bytes to and from the receiver's
# UART - no structure, no interpretation, on purpose: u-blox' flash protocol is
# not public and does not need to be, because the tool that speaks it runs on the
# operator's PC and we only carry its bytes.
ID_GNSS_TUNNEL_DOWN = 0x14  # server -> device, bytes for the ZED
ID_GNSS_TUNNEL_UP = 0x15    # device -> server, bytes from the ZED
# One device system-log entry (the device's streaming syslog). Raw fields, not
# the formatted line: formatted it would exceed the device's 128 B payload cap.
ID_SYSLOG_LINE = 0x16
# Server verdict on one finished upload (srv->dev): [result i8][crc32 u32].
ID_FILE_UP_ACK = 0x17
UP_ACK_OK = 1
UP_ACK_SIZE_MISMATCH = -1
UP_ACK_CRC_MISMATCH = -2
UP_ACK_ABORTED = -3

# An RTCM3 frame is 3 B header + up to 1023 B payload + 3 B CRC-24Q. The device
# sizes its inbound buffer from the same ceiling (1032 bytes in the reference
# firmware), so anything above this could never be reassembled there.
RTCM3_MAX_FRAME = 1029

# Largest tunnel payload per envelope. The device reassembles inbound envelopes
# into 1032 bytes (reference firmware), so this is the
# receiving buffer's ceiling and not a throughput preference.
TUNNEL_MAX_PAYLOAD = 1024

# FILE_STATUS phases (as the device firmware numbers them)
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
    ID_SENSOR_WN90LP: (
        "wn90lp",
        "<I" + "i" * 13,
        ("source", "light_lux", "uvi_d", "temp_mC", "rh_mpct", "wind_mms",
         "gust_mms", "dir_deg", "rain_um", "press_Pa", "rain_ctr_um",
         "batt_mV", "cap_mV"),
    ),
}

SENSOR_STREAMS = tuple(spec[0] for spec in SENSOR_SPECS.values())


@dataclass(frozen=True)
class Ident:
    station_id: int
    # ROLE_UNSET for any firmware that doesn't send the byte (short payload) or
    # sends 0 explicitly — the two are indistinguishable on the wire and treated
    # identically: no auto-registration as a correction source either way.
    role: int = ROLE_UNSET
    # The device's own station_name (CONFIG.TXT), e.g. "A001" — the identity the
    # batch-mode file names carried. RAW as sent: sanitising for the file system
    # is stationdir.sanitize()'s job, not the decoder's. "" for firmware that
    # predates the field, which keeps the bare-id archive layout.
    name: str = ""


@dataclass(frozen=True)
class Heartbeat:
    pass


@dataclass(frozen=True)
class NmeaSentence:
    """One NMEA sentence as the RECEIVER emitted it (device -> server).

    Deliberately not a SENSOR_SPECS row: those are fixed binary structs with a
    device timestamp, this is text that carries its own UTC field. It is also
    kept verbatim - talker included, checksum included. The device has already
    verified that checksum; re-normalising the line here (say, rewriting $GNGGA
    to $GPGGA) would invalidate it and produce a file whose sentences no longer
    match what any receiver said.
    """

    text: str


_SYSLOG_LEVELS = {0: "ERROR", 1: "WARN ", 2: "INFO ", 3: "DEBUG"}


@dataclass(frozen=True)
class SyslogLine:
    """One entry of the device's daily system log (device -> server).

    Carries the raw SystemLogEntry_t fields; `formatted()` rebuilds the line
    byte for byte as the device's systemLog_formatEntry() writes it to SD, so the
    server copy and the SD copy of a log file can be compared with diff.
    The timestamp is the device RTC (UTC), not the GNSS clock.
    """

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    level: int
    text: str

    def formatted(self) -> str:
        return (
            f"{2000 + self.year:04d}-{self.month:02d}-{self.day:02d} "
            f"{self.hour:02d}:{self.minute:02d}:{self.second:02d} "
            f"[{_SYSLOG_LEVELS.get(self.level, '?????')}] {self.text}\r\n"
        )


@dataclass(frozen=True)
class GnssTunnelData:
    """Raw receiver bytes travelling up from the device (K-01 Etappe 2).

    No fields beyond the bytes, and that is the whole point: this frame exists so
    that u-center and ubxfwupdate can talk to a ZED through the device without
    either end of the link understanding the conversation.
    """

    data: bytes


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
    stream: str  # "ina219" | "ina219ext" | "accel" | "accelext" | "sht4x"
    #             | "lps28" | "wn90lp"
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


def encode_file_up_ack(result: int, crc32: int) -> bytes:
    """Build a FILE_UP_ACK frame: the server's verdict on one upload."""
    return build_ubx(PRIVATE_CLASS, ID_FILE_UP_ACK,
                     struct.pack("<bI", result, crc32 & 0xFFFFFFFF))


def encode_gnss_tunnel(raw: bytes) -> bytes:
    """Wrap operator bytes for the device's receiver UART.

    The caller must chunk to at most TUNNEL_MAX_PAYLOAD. Refusing rather than
    splitting here is deliberate: splitting would be silent, and a tunnel whose
    chunking rules live in two places is one whose flow control lives in neither.
    """
    if not raw or len(raw) > TUNNEL_MAX_PAYLOAD:
        raise ValueError(f"tunnel chunk out of range: {len(raw)} bytes")
    return build_ubx(PRIVATE_CLASS, ID_GNSS_TUNNEL_DOWN, raw)


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
        # Byte 8 onwards is the length-prefixed station name (FW 1.69.x+). Same
        # rule as the role byte before it: a payload that stops short is older
        # firmware, not a malformed frame.
        name = ""
        if len(payload) >= 9:
            name_len = payload[8]
            # Trust the length only as far as the payload actually goes — a
            # truncated name is worth having, a struct.error that kills the
            # connection's frame loop is not.
            name = payload[9:9 + name_len].decode("utf-8", errors="replace").strip()
        return Ident(station_id, role, name)

    if msg_id == ID_NMEA_GGA:
        if not payload:
            return None
        # errors="replace" rather than a raise: a corrupted byte must not kill
        # the connection's frame loop over a sentence the device already
        # checksummed. A visible replacement char in the file is the honest
        # record of what arrived.
        return NmeaSentence(payload.decode("ascii", errors="replace").strip())

    if msg_id == ID_SYSLOG_LINE:
        if len(payload) < 7:
            return None
        y, mo, d, h, mi, se, lvl = payload[:7]
        return SyslogLine(y, mo, d, h, mi, se, lvl,
                          payload[7:].decode("ascii", errors="replace"))

    if msg_id == ID_GNSS_TUNNEL_UP:
        # Length zero is not malformed here, merely empty - but there is nothing
        # to hand on, so it is dropped like any other no-op frame.
        if not payload:
            return None
        return GnssTunnelData(bytes(payload))

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
