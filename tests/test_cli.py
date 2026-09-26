"""CLI request/response over the stream.

A download-class command is an ordinary command since the transfer rides the
stream: one answer, sent once the file has arrived, completes the request.
"""

import asyncio

import pytest

from streaming.frames import CliResponse, ubx_payload
from streaming.sinks import Sink
from streaming.station import StationSession, is_download_class


class FakeWriter:
    def __init__(self):
        self.sent = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


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
async def test_download_resolves_on_its_single_answer():
    session, _ = make_session()
    task = asyncio.create_task(session.send_cli("download CONFIG.TXT", "", timeout=5))
    await asyncio.sleep(0)

    # The transfer rides the stream; the device answers once, when it is done.
    session.handle_cli_response(CliResponse("CONFIG.TXT: 1234 bytes\r\n", last=False))
    session.handle_cli_response(CliResponse("download OK\r\n", last=True))
    assert await task == "CONFIG.TXT: 1234 bytes\r\ndownload OK\r\n"


@pytest.mark.asyncio
async def test_stale_connection_cleanup_must_not_unbind_the_live_one():
    """A re-dialling device opens the new socket before the old one is reaped.

    The stale socket then hits its idle timeout *after* the new one is bound. Its
    cleanup must not clear the live binding — that would report a happily streaming
    station as disconnected and make the remote CLI unusable.
    """
    session, old_writer = make_session()

    new_writer = FakeWriter()
    session.bind(new_writer, "1.2.3.4:5001")          # device re-dials
    assert old_writer.closed, "the superseded socket must be closed at once"

    session.unbind(old_writer)                        # stale handler finally runs

    assert session.connected is True
    assert session.writer is new_writer
    assert session.peer == "1.2.3.4:5001"

    # And the CLI still works over the live connection.
    task = asyncio.create_task(session.send_cli("sysinfo", "", timeout=5))
    await asyncio.sleep(0)
    assert new_writer.sent, "command must go out over the new socket"
    session.handle_cli_response(CliResponse("ok\n", last=True))
    assert await task == "ok\n"

    # The live connection's own cleanup does unbind it.
    session.unbind(new_writer)
    assert session.connected is False


@pytest.mark.asyncio
async def test_disconnect_during_command_times_out():
    session, _ = make_session()
    task = asyncio.create_task(session.send_cli("sysinfo", "", timeout=0.2))
    await asyncio.sleep(0)

    session.unbind()

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
async def test_unencodable_command_does_not_strand_the_pending_future():
    """A command the frame cannot carry must not wedge the session.

    encode_cmd_request() is ascii-only. Encoding after the future was created meant
    the raise skipped the cleanup: _done() stayed False and status() reported
    cli_pending forever — one emoji from a chat client poisoned the station's
    reported state until some later command happened to overwrite the future.
    """
    session, writer = make_session()

    with pytest.raises(UnicodeEncodeError):
        await session.send_cli("sysinfo ☃", "", timeout=5)

    assert session.status()["cli_pending"] is False
    assert not writer.sent, "a command that cannot be encoded must not go out"

    # The lock is released and the session still works.
    task = asyncio.create_task(session.send_cli("sysinfo", "", timeout=5))
    await asyncio.sleep(0)
    session.handle_cli_response(CliResponse("ok\n", last=True))
    assert await task == "ok\n"


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
