"""CLI request/response over the stream, including the download reconnect dance.

The download case is the subtle one: the device answers *twice* and closes the
socket in between. If we resolved the request on the first answer, the caller would
get an ack instead of the result; if we dropped the request on the disconnect, the
real answer would be lost entirely.
"""

import asyncio

import pytest

from streaming.frames import CliResponse, ubx_payload
from streaming.sinks import Sink
from streaming.station import StationSession, is_download_class


class FakeWriter:
    def __init__(self):
        self.sent = bytearray()

    def write(self, data: bytes) -> None:
        self.sent.extend(data)

    async def drain(self) -> None:
        pass


def make_session() -> tuple[StationSession, FakeWriter]:
    session = StationSession(1001, [Sink()])
    writer = FakeWriter()
    session.bind(writer, "1.2.3.4:5000")
    return session, writer


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("download CONFIG.TXT", True),
        ("downloadfw", True),
        ("sysinfo", False),
        ("listfiles", False),
        ("reboot", False),
    ],
)
def test_download_class_detection(cmd, expected):
    assert is_download_class(cmd) is expected


@pytest.mark.asyncio
async def test_simple_command_reassembles_chunks():
    session, writer = make_session()
    task = asyncio.create_task(session.send_cli("sysinfo", "tok", timeout=5))
    await asyncio.sleep(0)

    # The command must have gone out with the token prefix.
    payload = ubx_payload(bytes(writer.sent))
    assert payload[0] == 3
    assert payload[1:4] == b"tok"
    assert payload[4:] == b"sysinfo"

    session.handle_cli_response(CliResponse("line1\n", last=False))
    session.handle_cli_response(CliResponse("line2\n", last=True))

    assert await task == "line1\nline2\n"


@pytest.mark.asyncio
async def test_download_ack_does_not_resolve_the_request():
    session, _ = make_session()
    task = asyncio.create_task(session.send_cli("download CONFIG.TXT", "", timeout=5))
    await asyncio.sleep(0)

    # 1) The device acks over the still-open socket, with last=True.
    session.handle_cli_response(CliResponse("stream paused for transfer\r\n", last=True))
    await asyncio.sleep(0)
    assert not task.done(), "the ack must not complete the request"

    # 2) The device closes the socket to run the transfer.
    session.unbind()
    assert session.transfer_in_progress is True

    # 3) It reconnects with a fresh IDENT ...
    session.bind(FakeWriter(), "1.2.3.4:5001")
    assert session.transfer_in_progress is False

    # 4) ... and only now sends the deferred output.
    session.handle_cli_response(CliResponse("CONFIG.TXT downloaded\r\n", last=True))
    assert await task == "CONFIG.TXT downloaded\r\n"


@pytest.mark.asyncio
async def test_disconnect_during_normal_command_is_not_a_transfer():
    session, _ = make_session()
    task = asyncio.create_task(session.send_cli("sysinfo", "", timeout=0.2))
    await asyncio.sleep(0)

    session.unbind()
    assert session.transfer_in_progress is False

    with pytest.raises(asyncio.TimeoutError):
        await task


@pytest.mark.asyncio
async def test_command_on_disconnected_station_raises():
    session = StationSession(1001, [Sink()])
    with pytest.raises(ConnectionError):
        await session.send_cli("sysinfo", "", timeout=1)


@pytest.mark.asyncio
async def test_late_response_after_timeout_is_ignored():
    session, _ = make_session()
    task = asyncio.create_task(session.send_cli("sysinfo", "", timeout=0.05))
    with pytest.raises(asyncio.TimeoutError):
        await task

    # Must not raise (no pending future to resolve).
    session.handle_cli_response(CliResponse("too late\r\n", last=True))


@pytest.mark.asyncio
async def test_commands_are_serialised_per_station():
    session, _ = make_session()
    first = asyncio.create_task(session.send_cli("sysinfo", "", timeout=5))
    await asyncio.sleep(0)
    second = asyncio.create_task(session.send_cli("whoami", "", timeout=5))
    await asyncio.sleep(0)

    # The second command must be queued behind the first, not interleaved.
    assert not second.done()
    session.handle_cli_response(CliResponse("one\n", last=True))
    assert await first == "one\n"

    await asyncio.sleep(0)
    session.handle_cli_response(CliResponse("two\n", last=True))
    assert await second == "two\n"
