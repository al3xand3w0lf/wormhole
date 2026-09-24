"""Private 0xF0 frame decode/encode (the device's own protocol)."""

import struct

import pytest
from pyubx2 import isvalid_checksum

from streaming.frames import (
    PRIVATE_CLASS,
    ID_CMD_REQUEST,
    ID_IDENT,
    ID_SENSOR_ADXL345,
    ID_SENSOR_ADXL345_EXT,
    ID_SENSOR_INA219,
    ID_SENSOR_INA219_EXT,
    ID_NMEA_GGA,
    ID_SENSOR_LPS28DFW,
    ID_SENSOR_SHT4X,
    ROLE_BASE,
    ROLE_LOGGER,
    ROLE_ROVER,
    ROLE_ROVER_NTRIP,
    ROLE_STREAM,
    ROLE_UNSET,
    CliResponse,
    Heartbeat,
    Ident,
    NmeaSentence,
    SensorReading,
    build_ubx,
    decode_private,
    encode_cmd_request,
    ubx_payload,
)

from .helpers import cli_response, heartbeat, ident, nmea_gga, sensor


def test_ident():
    msg = decode_private(ident(1001))
    assert msg == Ident(1001)
    assert msg.role == ROLE_UNSET


@pytest.mark.parametrize("role", [ROLE_UNSET, ROLE_BASE, ROLE_ROVER, ROLE_LOGGER,
                                  ROLE_STREAM, ROLE_ROVER_NTRIP])
def test_ident_role_byte(role):
    """The role byte a firmware that supports auto-discovery sends - see the
    role mapping in streaming/frames.py for how each of these gets chosen on
    the device side."""
    msg = decode_private(ident(1001, role))
    assert msg == Ident(1001, role)


def test_ident_without_a_role_byte_is_old_firmware_not_malformed():
    """Every firmware before this feature sends stationId + 4 zero
    reserved bytes and nothing else - payload length 8, no byte 4 to read in
    the new sense (byte 4 IS there, it is just always 0 from the old
    firmware's perspective). A genuinely SHORTER payload (< 5 bytes, so no
    byte 4 at all) must still decode, falling back to ROLE_UNSET rather than
    being rejected as malformed."""
    raw = build_ubx(PRIVATE_CLASS, ID_IDENT, struct.pack("<I", 1001))  # 4 bytes only
    msg = decode_private(raw)
    assert msg == Ident(1001, ROLE_UNSET)


def test_heartbeat():
    assert decode_private(heartbeat()) == Heartbeat()


def test_cli_response_flags():
    assert decode_private(cli_response("part", False)) == CliResponse("part", False)
    assert decode_private(cli_response("done\r\n", True)) == CliResponse("done\r\n", True)


@pytest.mark.parametrize(
    "msg_id,fmt,stream,values",
    [
        (ID_SENSOR_INA219, "<Iiii", "ina219", {"mV": 12345, "mA": -678, "mW": 9012}),
        (ID_SENSOR_INA219_EXT, "<Iiii", "ina219ext", {"mV": 3300, "mA": 15, "mW": 49}),
        (ID_SENSOR_ADXL345, "<Iiii", "accel", {"x_ug": -1000, "y_ug": 2000, "z_ug": 980000}),
        (ID_SENSOR_ADXL345_EXT, "<Iiii", "accelext", {"x_ug": 1, "y_ug": -2, "z_ug": 3}),
        (ID_SENSOR_SHT4X, "<Iii", "sht4x", {"temp_mC": 21500, "rh_mpct": 45300}),
        (ID_SENSOR_LPS28DFW, "<Iii", "lps28", {"press_Pa": 101325, "temp_mC": 20100}),
    ],
)
def test_sensor_decode(msg_id, fmt, stream, values):
    raw = sensor(msg_id, fmt, 1_783_000_000, *values.values())
    msg = decode_private(raw)
    assert isinstance(msg, SensorReading)
    assert msg.stream == stream
    assert msg.rtc_unix == 1_783_000_000
    assert msg.values == values


def test_internal_and_external_are_distinguishable():
    """Internal and external sensors must not collide."""
    a = decode_private(sensor(ID_SENSOR_INA219, "<Iiii", 1, 1, 2, 3))
    b = decode_private(sensor(ID_SENSOR_INA219_EXT, "<Iiii", 1, 1, 2, 3))
    assert a.stream != b.stream


GGA_FIXED = (
    "$GNGGA,123519.00,4712.34567,N,00832.45678,E,4,12,0.8,"
    "512.34,M,47.12,M,1.2,1001*4C"
)


def test_nmea_gga_decodes_verbatim():
    msg = decode_private(nmea_gga(GGA_FIXED))
    assert isinstance(msg, NmeaSentence)
    # Verbatim matters: the talker stays GN and the checksum still covers the
    # line. Normalising it here would produce a sentence no receiver emitted.
    assert msg.text == GGA_FIXED


# A REAL sentence off a bench rover (RTK FIXED):
# CFG-NMEA-HIGHPREC gives 7 decimals of minutes and 3 for altitude, which takes
# the line to 88 characters - past the NMEA-0183 cap of 82 that u-blox breaks on
# purpose here (the interface description forbids combining HIGHPREC with
# LIMIT82 for exactly this reason).
GGA_HIGHPREC = (
    "$GNGGA,064416.00,4724.4986963,N,00830.3503494,E,4,12,0.54,"
    "526.634,M,47.343,M,1.0,1001*61"
)


def test_nmea_gga_highprec_survives_the_82_char_limit():
    """A high-precision GGA is longer than NMEA-0183 allows, and must pass anyway.

    The whole value of the high-precision mode is in the extra decimals: a
    length check anywhere on this path would silently drop the very sentences
    the rover exists to produce, and only for the stations that actually reach
    an RTK fix. Nothing here may bound a sentence at 82.
    """
    assert len(GGA_HIGHPREC) > 82
    msg = decode_private(nmea_gga(GGA_HIGHPREC))
    assert isinstance(msg, NmeaSentence)
    assert msg.text == GGA_HIGHPREC


def test_nmea_gga_is_not_a_sensor_reading():
    """It carries text and its own UTC field, so it must not enter the CSV path."""
    from streaming.frames import SENSOR_SPECS

    assert ID_NMEA_GGA not in SENSOR_SPECS
    assert not isinstance(decode_private(nmea_gga(GGA_FIXED)), SensorReading)


def test_nmea_gga_empty_payload_returns_none():
    from streaming.frames import build_ubx

    assert decode_private(build_ubx(PRIVATE_CLASS, ID_NMEA_GGA, b"")) is None


def test_unknown_id_returns_none():
    from streaming.frames import build_ubx

    assert decode_private(build_ubx(PRIVATE_CLASS, 0x7E, b"\x00")) is None


def test_truncated_sensor_payload_returns_none():
    from streaming.frames import build_ubx

    assert decode_private(build_ubx(PRIVATE_CLASS, ID_SENSOR_INA219, b"\x01\x02")) is None


class TestCmdRequest:
    def test_checksum_is_valid(self):
        raw = encode_cmd_request("sysinfo", "s3cret")
        assert isvalid_checksum(raw)
        assert raw[2] == PRIVATE_CLASS
        assert raw[3] == ID_CMD_REQUEST

    def test_token_layout(self):
        raw = encode_cmd_request("sysinfo", "s3cret")
        payload = ubx_payload(raw)
        assert payload[0] == 6  # tok_len
        assert payload[1:7] == b"s3cret"
        assert payload[7:] == b"sysinfo"

    def test_empty_token_still_sends_length_prefix(self):
        """The device always strips a tok_len byte, so it must always be present."""
        payload = ubx_payload(encode_cmd_request("whoami", ""))
        assert payload[0] == 0
        assert payload[1:] == b"whoami"

    def test_oversized_token_rejected(self):
        with pytest.raises(ValueError):
            encode_cmd_request("whoami", "x" * 256)


def test_ident_carries_the_station_name():
    """FW 1.69.x appends [name_len][station_name] after the reserved bytes. The
    name is what the batch-mode file names were built from ("A001"), and it is
    the only way a stream consumer can tell which project site station 2001 is.
    """
    msg = decode_private(ident(2001, ROLE_BASE, "A001"))
    assert msg == Ident(2001, ROLE_BASE, "A001")


def test_ident_name_is_decoded_raw():
    """The device sends station_name verbatim - spaces and non-ASCII included.
    Making it safe for a file system is stationdir.sanitize()'s job; a decoder
    that pre-mangled it would destroy the station's actual name."""
    assert decode_private(ident(2001, ROLE_BASE, "Zurich Nord")).name == "Zurich Nord"


def test_ident_without_a_name_is_older_firmware():
    """Same tolerance rule as the role byte: a payload that stops at 8 bytes is
    pre-1.69 firmware, not a malformed frame. Its archive keeps the bare-id
    layout."""
    assert decode_private(ident(2001, ROLE_BASE)).name == ""


def test_ident_name_length_longer_than_the_payload_is_truncated_not_fatal():
    """A name_len that overruns the payload must not raise: an exception here
    would kill the connection's whole frame loop over one bad IDENT byte. Take
    what is actually there."""
    raw = build_ubx(PRIVATE_CLASS, ID_IDENT,
                    struct.pack("<IB3xB", 2001, ROLE_BASE, 99) + b"A001")
    assert decode_private(raw) == Ident(2001, ROLE_BASE, "A001")

# --- WN90LP weather sensor (0x13) -------------------------------------------


def _wn90lp_frame(rtc, values):
    from streaming.frames import (ID_SENSOR_WN90LP, PRIVATE_CLASS, SENSOR_SPECS,
                                  build_ubx)
    _stream, fmt, names = SENSOR_SPECS[ID_SENSOR_WN90LP]
    payload = struct.pack(fmt, rtc, *[values[n] for n in names])
    return build_ubx(PRIVATE_CLASS, ID_SENSOR_WN90LP, payload)


def test_wn90lp_roundtrip():
    from streaming.frames import ID_SENSOR_WN90LP, SENSOR_SPECS, decode_private
    _stream, _fmt, names = SENSOR_SPECS[ID_SENSOR_WN90LP]
    values = {n: (i + 1) * 100 for i, n in enumerate(names)}
    reading = decode_private(_wn90lp_frame(1788480011, values))
    assert reading.stream == "wn90lp"
    assert reading.rtc_unix == 1788480011
    assert reading.values == values


def test_wn90lp_payload_is_56_bytes():
    """4 bytes rtc_unix plus thirteen int32 fields. A change here is a
    wire-format change and needs the producer and any firmware to move in
    step."""
    from streaming.frames import SENSOR_SPECS, ID_SENSOR_WN90LP
    _stream, fmt, names = SENSOR_SPECS[ID_SENSOR_WN90LP]
    assert struct.calcsize(fmt) == 56
    assert len(names) == 13


def test_wn90lp_carries_provenance():
    """`source` distinguishes a sample the producer took from a per-minute
    value recovered out of the sensor's history.

    Without it the two are indistinguishable in the archive, and they are not
    the same quantity: in the history block wind is a per-minute MEAN and gust
    a per-minute MAXIMUM, against instantaneous readings in a live row. A gust
    statistic computed across both would be wrong in a way nothing flags.
    """
    from streaming.frames import ID_SENSOR_WN90LP, SENSOR_SPECS, decode_private
    _stream, _fmt, names = SENSOR_SPECS[ID_SENSOR_WN90LP]
    assert names[0] == "source", "provenance leads the row, as in the producer CSV"
    for code in (0, 1):
        values = dict.fromkeys(names, 0)
        values["source"] = code
        reading = decode_private(_wn90lp_frame(1788480011, values))
        assert reading.values["source"] == code


def test_wn90lp_unknown_provenance_is_not_silently_live():
    """A producer that does not say must not be recorded as having said 0."""
    from streaming.frames import ID_SENSOR_WN90LP, SENSOR_SPECS, decode_private
    _stream, _fmt, names = SENSOR_SPECS[ID_SENSOR_WN90LP]
    values = dict.fromkeys(names, 0)
    values["source"] = -2147483648
    reading = decode_private(_wn90lp_frame(1788480011, values))
    assert reading.values["source"] == -2147483648


def test_wn90lp_keeps_the_invalid_sentinel():
    """INT32_MIN means "no reading" and must survive decoding untouched.

    Substituting 0 anywhere on this path turns a missing wind sample into
    recorded calm and a missing pressure into 0 hPa - a plausible-looking
    measurement that never happened.
    """
    from streaming.frames import ID_SENSOR_WN90LP, SENSOR_SPECS, decode_private
    _stream, _fmt, names = SENSOR_SPECS[ID_SENSOR_WN90LP]
    values = dict.fromkeys(names, -2147483648)
    values["temp_mC"] = 23500                      # one real reading among them
    reading = decode_private(_wn90lp_frame(1788480011, values))
    assert reading.values["wind_mms"] == -2147483648
    assert reading.values["press_Pa"] == -2147483648
    assert reading.values["temp_mC"] == 23500


def test_wn90lp_short_payload_is_rejected():
    from streaming.frames import (ID_SENSOR_WN90LP, PRIVATE_CLASS, build_ubx,
                                  decode_private)
    stumpf = build_ubx(PRIVATE_CLASS, ID_SENSOR_WN90LP, bytes(55))
    assert decode_private(stumpf) is None


def test_wn90lp_id_does_not_collide():
    from streaming.frames import (ID_SENSOR_WN90LP, ID_NMEA_GGA, ID_RTCM_INFO,
                                  SENSOR_SPECS)
    assert ID_SENSOR_WN90LP == 0x13
    assert ID_SENSOR_WN90LP not in (ID_NMEA_GGA, ID_RTCM_INFO)
    ids = list(SENSOR_SPECS)
    assert len(ids) == len(set(ids))
