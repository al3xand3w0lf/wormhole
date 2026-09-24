"""Serving a file down the streaming socket .

The contract that matters here is byte-exactness: FILE_BEGIN announces a length
and a CRC32, and what follows on the wire must be exactly that many raw bytes
with no envelope around them. The device counts bytes rather than parsing frames,
so a single spurious byte in this stream would land in its file.
"""

import asyncio
import struct
import zlib

import pytest

from streaming import filetransfer, pipeline
from streaming.frames import (
    PRIVATE_CLASS,
    ID_FILE_BEGIN,
    ID_FILE_REQUEST,
    ID_FILE_STATUS,
    ID_FILE_UP_BEGIN,
    ID_FILE_UP_DATA,
    FILE_PHASE_ABORTED,
    FileRequest,
    FileStatus,
    FileUpBegin,
    FileUpData,
    build_ubx,
    decode_private,
    encode_file_begin,
    ubx_payload,
)
from streaming.sinks import Sink
from streaming.station import StationSession


class FakeWriter:
    def __init__(self):
        self.sent = bytearray()

    def write(self, data: bytes) -> None:
        self.sent.extend(data)

    async def drain(self) -> None:
        pass


def make_session() -> tuple[StationSession, FakeWriter]:
    session = StationSession(4711, [Sink()])
    writer = FakeWriter()
    session.bind(writer, "1.2.3.4:5000")
    return session, writer


def split_begin(sent: bytes) -> tuple[dict, bytes]:
    """Split the wire bytes into the decoded FILE_BEGIN and the raw remainder."""
    assert sent[:4] == bytes([0xB5, 0x62, PRIVATE_CLASS, ID_FILE_BEGIN])
    plen = sent[4] | (sent[5] << 8)
    frame_end = 6 + plen + 2
    payload = ubx_payload(sent[:frame_end])
    total, crc, name_len = struct.unpack_from("<IIB", payload, 0)
    name = payload[9 : 9 + name_len].decode()
    return {"total": total, "crc": crc, "name": name}, bytes(sent[frame_end:])


# --- frame round trips -----------------------------------------------------

def test_file_request_round_trip():
    raw = build_ubx(PRIVATE_CLASS, ID_FILE_REQUEST, bytes([0, 11]) + b"CONFIG.TXT\x00"[:11])
    msg = decode_private(raw)
    assert isinstance(msg, FileRequest)
    assert msg.flags == 0


def test_file_status_round_trip_negative_code():
    """The device reports its error codes as signed bytes (-6 = CRC mismatch)."""
    raw = build_ubx(PRIVATE_CLASS, ID_FILE_STATUS, struct.pack("<BbI", FILE_PHASE_ABORTED, -6, 4096))
    msg = decode_private(raw)
    assert msg == FileStatus(FILE_PHASE_ABORTED, -6, 4096)


def test_file_begin_carries_total_and_crc():
    body = b"x" * 1234
    frame = encode_file_begin(len(body), zlib.crc32(body), "device.bin")
    info, rest = split_begin(frame)
    assert rest == b""
    assert info == {"total": 1234, "crc": zlib.crc32(body), "name": "device.bin"}


# --- the transfer itself ---------------------------------------------------

@pytest.mark.asyncio
async def test_send_file_is_begin_plus_exact_payload(tmp_path, monkeypatch):
    body = bytes(range(256)) * 40          # 10240 B, spans several 8 KB chunks
    target = tmp_path / "device.bin"
    target.write_bytes(body)
    monkeypatch.setattr(filetransfer, "resolve_download",
                        lambda name: (name, tmp_path / name))

    session, writer = make_session()
    await filetransfer.send_file(session, "device.bin")

    info, rest = split_begin(bytes(writer.sent))
    assert info["total"] == len(body)
    assert info["crc"] == zlib.crc32(body)
    # Nothing but the file itself follows the announcement — the device consumes
    # this by byte count, so any extra byte would end up inside its file.
    assert rest == body
    assert session.file_transfer_active is False


@pytest.mark.asyncio
async def test_missing_file_answers_total_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(filetransfer, "resolve_download", lambda name: (name, None))

    session, writer = make_session()
    await filetransfer.send_file(session, "nope.bin")

    info, rest = split_begin(bytes(writer.sent))
    assert info["total"] == 0
    assert rest == b""     # a "not found" must not be followed by any payload


@pytest.mark.asyncio
async def test_empty_file_sends_no_payload(tmp_path, monkeypatch):
    """total=0 also means 'not found', so an empty file must not be offered as one."""
    (tmp_path / "empty.txt").write_bytes(b"")
    monkeypatch.setattr(filetransfer, "resolve_download",
                        lambda name: (name, tmp_path / name))

    session, writer = make_session()
    await filetransfer.send_file(session, "empty.txt")

    info, rest = split_begin(bytes(writer.sent))
    assert info["total"] == 0
    assert rest == b""


@pytest.mark.asyncio
async def test_second_transfer_is_refused_while_one_runs(tmp_path, monkeypatch):
    (tmp_path / "a.bin").write_bytes(b"payload")
    monkeypatch.setattr(filetransfer, "resolve_download",
                        lambda name: (name, tmp_path / name))

    session, writer = make_session()
    session.file_transfer_active = True     # pretend one is in flight
    await filetransfer.send_file(session, "a.bin")

    # The device runs the transfer on the task that owns the socket and has the
    # GNSS uplink paused; a second one has nothing to run on.
    assert bytes(writer.sent) == b""


@pytest.mark.asyncio
async def test_transfer_holds_the_write_lock(tmp_path, monkeypatch):
    """Nothing may be written into the socket while the payload is streaming.

    The device consumes the payload by byte count, so a CMD_REQUEST frame sent
    from the admin API mid-transfer would land *inside* the downloaded file —
    the §15 splice, in the server-to-device direction.
    """
    body = b"x" * 100
    (tmp_path / "a.bin").write_bytes(body)
    monkeypatch.setattr(filetransfer, "resolve_download",
                        lambda name: (name, tmp_path / name))

    session, writer = make_session()
    await session.write_lock.acquire()          # stand in for another writer

    task = asyncio.create_task(filetransfer.send_file(session, "a.bin"))
    await asyncio.sleep(0.05)
    assert bytes(writer.sent) == b"", "transfer wrote while the socket was held"

    session.write_lock.release()
    await task

    info, rest = split_begin(bytes(writer.sent))
    assert info["total"] == len(body)
    assert rest == body


@pytest.mark.asyncio
async def test_cli_command_waits_for_a_running_transfer(tmp_path, monkeypatch):
    """send_cli() must queue behind a transfer instead of splicing into it."""
    session, writer = make_session()
    await session.write_lock.acquire()          # a transfer is streaming

    task = asyncio.create_task(session.send_cli("sysinfo", "", timeout=5))
    await asyncio.sleep(0.05)
    assert bytes(writer.sent) == b"", "CLI request was written during a transfer"

    session.write_lock.release()
    await asyncio.sleep(0.05)
    assert bytes(writer.sent) != b""            # released -> the request goes out

    task.cancel()
    with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
        await task


@pytest.mark.asyncio
async def test_disconnect_while_waiting_for_the_write_lock(tmp_path, monkeypatch):
    """The writer can go away while send_cli() waits for write_lock.

    Checking self.writer before taking the lock is not enough: the wait is
    unbounded (a file transfer, or since rover.py a correction stream, holds
    the lock) and unbind() clears the writer in that window. Live on
    2026-08-06 this raised AttributeError out of the admin API as a 500.
    It must surface as ConnectionError - and must not leave cli_pending stuck.
    """
    session, writer = make_session()
    await session.write_lock.acquire()          # something owns the socket

    task = asyncio.create_task(session.send_cli("sysinfo", "", timeout=5))
    await asyncio.sleep(0.05)                   # parked on write_lock

    session.unbind(writer)                      # device drops meanwhile
    session.write_lock.release()

    with pytest.raises(ConnectionError):
        await task

    assert bytes(writer.sent) == b"", "wrote into a socket that was already gone"
    assert session.status()["cli_pending"] is False, "cli_pending stuck after the raise"


@pytest.mark.asyncio
async def test_not_connected_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(filetransfer, "resolve_download",
                        lambda name: (name, tmp_path / name))
    session = StationSession(4711, [Sink()])   # never bound
    await filetransfer.send_file(session, "a.bin")
    assert session.file_transfer_active is False


# --- server-detected failure must not leave the CLI caller hanging ---------
#
# A download-class command (downloadcf/downloadfw) is only supposed to finish
# when the device sends a CLI_RESPONSE — but on a failure the server already
# knows the outcome itself (it answered FILE_BEGIN(total=0), or saw
# FILE_STATUS(ABORTED)) and must not wait out the full CLI timeout for a
# response that a failed transfer may never produce.

@pytest.mark.asyncio
async def test_missing_file_resolves_a_pending_download_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(filetransfer, "resolve_download", lambda name: (name, None))

    session, writer = make_session()
    task = asyncio.create_task(session.send_cli("downloadfw", "", timeout=5))
    await asyncio.sleep(0.05)   # let send_cli register as awaiting-deferred

    await filetransfer.send_file(session, "nope.bin")

    result = await asyncio.wait_for(task, timeout=1)
    assert "not found" in result


@pytest.mark.asyncio
async def test_aborted_transfer_resolves_a_pending_download_cli():
    session, writer = make_session()
    task = asyncio.create_task(session.send_cli("downloadcf", "", timeout=5))
    await asyncio.sleep(0.05)

    pipeline._log_file_status(session, FileStatus(FILE_PHASE_ABORTED, -9, 0))

    result = await asyncio.wait_for(task, timeout=1)
    assert "aborted" in result
    assert "-9" in result


@pytest.mark.asyncio
async def test_missing_file_does_not_resolve_an_unrelated_cli_command(tmp_path, monkeypatch):
    """Only a *download-class* command is waiting for the transfer outcome — an
    ordinary command (e.g. sysinfo) must keep waiting for its own CLI_RESPONSE."""
    monkeypatch.setattr(filetransfer, "resolve_download", lambda name: (name, None))

    session, writer = make_session()
    task = asyncio.create_task(session.send_cli("sysinfo", "", timeout=5))
    await asyncio.sleep(0.05)

    await filetransfer.send_file(session, "nope.bin")
    await asyncio.sleep(0.05)
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- upload direction (Stage 2) --------------------------------------------

def up_begin(name: str, body: bytes) -> FileUpBegin:
    return FileUpBegin(len(body), zlib.crc32(body) & 0xFFFFFFFF, name)


def feed_upload(rx, name: str, body: bytes, chunk: int = 1000) -> None:
    rx.begin(up_begin(name, body))
    for seq, off in enumerate(range(0, len(body), chunk)):
        rx.data(FileUpData(seq, body[off : off + chunk]))


def test_upload_stores_the_file(tmp_path):
    rx = filetransfer.UploadReceiver(tmp_path, 1001)
    body = b"log line\r\n" * 500
    feed_upload(rx, "STATION_slog.txt", body)

    stored = list((tmp_path / "1001" / "uploads").glob("*"))
    assert len(stored) == 1
    assert stored[0].suffix == ".txt"            # .part was renamed away
    assert stored[0].read_bytes() == body
    assert rx.active is False


def test_upload_with_wrong_crc_is_discarded(tmp_path):
    rx = filetransfer.UploadReceiver(tmp_path, 1001)
    body = b"x" * 2500
    rx.begin(FileUpBegin(len(body), 0xDEADBEEF, "bad.bin"))    # CRC that cannot match
    for seq, off in enumerate(range(0, len(body), 1000)):
        rx.data(FileUpData(seq, body[off : off + 1000]))

    # Nothing published and nothing left behind — a file that fails its checksum
    # must not be mistakable for a good one.
    assert list((tmp_path / "1001" / "uploads").glob("*")) == []


def test_upload_sequence_gap_aborts(tmp_path):
    rx = filetransfer.UploadReceiver(tmp_path, 1001)
    body = b"y" * 3000
    rx.begin(up_begin("gap.bin", body))
    rx.data(FileUpData(0, body[:1000]))
    rx.data(FileUpData(2, body[2000:3000]))      # frame 1 never arrived

    assert rx.active is False
    assert list((tmp_path / "1001" / "uploads").glob("*")) == []


def test_upload_data_without_begin_is_ignored(tmp_path):
    rx = filetransfer.UploadReceiver(tmp_path, 1001)
    rx.data(FileUpData(0, b"orphan"))
    assert rx.active is False
    assert not (tmp_path / "1001").exists()


def test_second_upload_supersedes_an_unfinished_one(tmp_path):
    rx = filetransfer.UploadReceiver(tmp_path, 1001)
    rx.begin(up_begin("first.bin", b"a" * 5000))
    rx.data(FileUpData(0, b"a" * 1000))          # left hanging

    body = b"b" * 1500
    feed_upload(rx, "second.bin", body)

    stored = list((tmp_path / "1001" / "uploads").glob("*"))
    assert len(stored) == 1 and stored[0].read_bytes() == body


def test_file_up_frames_round_trip():
    body = b"payload bytes"
    raw = build_ubx(PRIVATE_CLASS, ID_FILE_UP_BEGIN,
                    struct.pack("<IIB", len(body), zlib.crc32(body), 7) + b"log.txt")
    assert decode_private(raw) == FileUpBegin(len(body), zlib.crc32(body), "log.txt")

    raw = build_ubx(PRIVATE_CLASS, ID_FILE_UP_DATA, struct.pack("<H", 42) + body)
    assert decode_private(raw) == FileUpData(42, body)


def test_upload_ack_is_sent_after_the_file_is_stored(tmp_path):
    """The device deletes its SD copy on this ACK - it must say OK only for a
    stored file with matching CRC, and a rejection otherwise."""
    import zlib as _z
    from streaming.filetransfer import UploadReceiver
    from streaming.frames import (FileUpBegin, FileUpData, UP_ACK_CRC_MISMATCH,
                                  UP_ACK_OK, encode_file_up_ack)

    data = b"x" * 1500
    crc = _z.crc32(data) & 0xFFFFFFFF
    sent = []
    rx = UploadReceiver(tmp_path, 1001, sent.append)
    rx.begin(FileUpBegin(len(data), crc, "a_log.txt"))
    rx.data(FileUpData(0, data[:1000]))
    assert sent == []
    rx.data(FileUpData(1, data[1000:]))
    assert sent == [encode_file_up_ack(UP_ACK_OK, crc)]
    assert list((tmp_path / "1001" / "uploads").glob("a_log_*.txt"))

    sent.clear()
    rx.begin(FileUpBegin(3, 0xDEADBEEF, "b.txt"))
    rx.data(FileUpData(0, b"abc"))
    assert sent == [encode_file_up_ack(UP_ACK_CRC_MISMATCH, _z.crc32(b"abc"))]
