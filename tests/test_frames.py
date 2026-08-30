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
    module docstring for how each of these gets chosen on the device side."""
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
    """Internal and external sensors must not collide on the server."""
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
