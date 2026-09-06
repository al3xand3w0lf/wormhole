"""RTCM3 transport-frame surgery — reading and rewriting DF003.

DF003 is the reference station id: the number a base stamps on every correction
message so a rover can say which base its solution came from. A receiver running
old u-blox firmware ignores the id configured on it and emits 0, which RTCM reads
as "no station named". One such base is harmless bookkeeping. Two are not: a rover
switching between them (Millipede's NEAR mountpoint does exactly that) detects the
switch by watching this number change, and two bases both claiming 0 make the
switch invisible — so it carries stale ambiguities into the new baseline.

The streaming server already knows who the station is: the session is keyed on the
id from its IDENT frame, so every frame that reaches `pipeline.route_frame()`
belongs to a station whose number is known. Filling it in there is a 12-bit write
and a new CRC, and it costs nothing to leave a correct id alone.

Two things worth knowing before touching a frame:

* DF003 lives in payload bits 12..23 — but only for the message types listed in
  `TYPES_WITH_REF_ID`. In an ephemeris message (1019, 1020, 1042, 1044-1046) those
  same bits are a *satellite* id, and a blind rewrite would corrupt it. Hence a
  positive list; anything not on it is passed through untouched.
* The id is 12 bits, so 0..4095. Every station number in use here fits, but the
  streaming server's own ids are not bounded by that, so a number too large to
  encode is a case to report rather than to truncate into a wrong one.
"""

from pyrtcm import crc2bytes

# Widest id DF003 can carry (12 bits).
MAX_REF_ID = 0xFFF

# Message types carrying DF003 in payload bits 12..23:
#   1001-1013  legacy RTK observables, station ARP, antenna descriptors,
#              GLONASS observables, system parameters
#   1029       Unicode text string
#   1033       receiver and antenna descriptors
#   1071-1137  MSM1-MSM7 for GPS/GLONASS/Galileo/SBAS/QZSS/BeiDou/NavIC
#   1230       GLONASS code-phase biases
# Deliberately NOT here: the ephemeris messages (1019, 1020, 1042, 1044-1046),
# whose bits 12..23 are a satellite id, and u-blox's proprietary 4072.
TYPES_WITH_REF_ID = frozenset(
    list(range(1001, 1014)) + [1029, 1033] + list(range(1071, 1138)) + [1230]
)

# D3 | 6 bits reserved + 10 bits length | payload | CRC-24Q (3 bytes).
_HEADER = 3
_CRC = 3
# Shortest frame that still has a full DF002 + DF003: 3 payload bytes.
_MIN_LEN = _HEADER + 3 + _CRC


def message_type(raw: bytes) -> int:
    """DF002, the message number — payload bits 0..11. -1 if the frame is too short."""
    if len(raw) < _HEADER + 2:
        return -1
    return (raw[_HEADER] << 4) | (raw[_HEADER + 1] >> 4)


def reference_station_id(raw: bytes) -> int | None:
    """DF003 — payload bits 12..23, or None if this type does not carry one."""
    if len(raw) < _MIN_LEN or message_type(raw) not in TYPES_WITH_REF_ID:
        return None
    return ((raw[_HEADER + 1] & 0x0F) << 8) | raw[_HEADER + 2]


def set_reference_station_id(raw: bytes, ref_id: int) -> bytes:
    """The same frame with DF003 replaced and the CRC recomputed.

    Only the 12 id bits move; every other field, and the frame length, stay as
    they were. Raises ValueError for an id DF003 cannot hold, rather than
    silently writing a truncated — and therefore wrong — station number.
    """
    if not 0 <= ref_id <= MAX_REF_ID:
        raise ValueError(f"reference station id {ref_id} does not fit in 12 bits")
    if len(raw) < _MIN_LEN:
        raise ValueError("frame too short to carry DF003")

    body = bytearray(raw[:-_CRC])
    body[_HEADER + 1] = (body[_HEADER + 1] & 0xF0) | ((ref_id >> 8) & 0x0F)
    body[_HEADER + 2] = ref_id & 0xFF
    body = bytes(body)
    return body + crc2bytes(body)
