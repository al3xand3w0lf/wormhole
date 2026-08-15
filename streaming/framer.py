"""Byte-stream framer.

The device is a thin pipe: it tees the raw u-blox byte stream (UBX + RTCM3) and
interleaves its own private frames (private UBX class 0xF0). A device can damage that
stream — by dropping bytes when a buffer fills, or by letting a second producer write
between the chunks of a chunked send, which tears a large frame in two. The framer
must resync rather than desync: one bad frame must cost one frame, never the rest of
the session.

`resync_events` / `garbage_bytes` make such damage *visible* instead of silently
absorbing it. A healthy device holds them at **zero**, so any rise is a signal worth
chasing — they are what exposed the torn-frame case above.

Why our own framer instead of pyubx2's UBXReader:
  1. UBXReader reads from a *blocking* stream (`.read(n)`); this server is asyncio.
  2. We need the byte-exact raw frame bytes to write .ubx / .rtcm3 files.
  3. We want explicit resync/garbage counters and a bounded buffer.

Checksum and CRC validation are delegated to the libraries (pyubx2 / pyrtcm) —
there is deliberately no hand-rolled Fletcher or CRC-24Q here. The resync
semantics mirror UBXReader.read(): advance byte by byte, discard anything that is
not a plausible sync byte.
"""

from dataclasses import dataclass
from typing import Iterator

from pyubx2 import isvalid_checksum
from pyrtcm import calc_crc24q

UBX_SYNC1 = 0xB5
UBX_SYNC2 = 0x62
RTCM3_PREAMBLE = 0xD3

# A corrupt length field must never make us buffer without bound. The device's own
# u-blox RX buffer is 8 KB, so it cannot emit a larger single UBX frame.
MAX_UBX_PAYLOAD = 8192
# RTCM3 carries a 10-bit length field, so it is naturally bounded at 1023.

KIND_UBX = "ubx"
KIND_RTCM3 = "rtcm3"

# Internal sentinels for _try_* results.
_NEED_MORE = object()
_BAD = object()


@dataclass(frozen=True)
class Frame:
    """One validated frame, with its byte-exact raw bytes."""

    kind: str  # KIND_UBX | KIND_RTCM3
    raw: bytes
    cls_: int = -1  # UBX message class (UBX only)
    id_: int = -1  # UBX message id (UBX only)


class StreamFramer:
    """Incremental, async-friendly framer. Feed it bytes, get back whole frames."""

    def __init__(self, max_buffer: int = 65536):
        self._buf = bytearray()
        self._max_buffer = max_buffer
        # Diagnostics (surfaced by the admin API and the health log).
        self.resync_events = 0
        self.garbage_bytes = 0
        self.ubx_frames = 0
        self.rtcm3_frames = 0

    def take_pending(self) -> bytes:
        """Hand back (and clear) the bytes not yet consumed into a frame.

        For switching a connection from framed to RAW mode: after FILE_BEGIN the
        rest of the stream is unframed file payload, and anything buffered here is
        its first bytes. Used by the device side (firmware and fake_device.py);
        the server itself never receives raw payload.
        """
        pending = bytes(self._buf)
        self._buf.clear()
        return pending

    def feed(self, data: bytes) -> Iterator[Frame]:
        """Feed received bytes; yields every complete, checksum-valid frame."""
        self._buf.extend(data)
        while True:
            frame = self._extract()
            if frame is None:
                # Absolute bound. Payload lengths are capped, so this should never
                # trigger in practice — it exists so a pathological stream cannot
                # stall the framer forever.
                if len(self._buf) > self._max_buffer:
                    self._resync()
                    continue
                return
            yield frame

    def _extract(self):
        while True:
            if not self._buf:
                return None
            b0 = self._buf[0]
            if b0 == UBX_SYNC1:
                result = self._try_ubx()
            elif b0 == RTCM3_PREAMBLE:
                result = self._try_rtcm3()
            else:
                self._resync()
                continue

            if result is _NEED_MORE:
                return None
            if result is _BAD:
                self._resync()
                continue
            return result

    def _try_ubx(self):
        buf = self._buf
        if len(buf) < 2:
            return _NEED_MORE
        if buf[1] != UBX_SYNC2:
            return _BAD
        if len(buf) < 6:
            return _NEED_MORE
        plen = buf[4] | (buf[5] << 8)
        if plen > MAX_UBX_PAYLOAD:
            return _BAD
        total = 6 + plen + 2
        if len(buf) < total:
            return _NEED_MORE
        raw = bytes(buf[:total])
        if not isvalid_checksum(raw):
            return _BAD
        del buf[:total]
        self.ubx_frames += 1
        return Frame(KIND_UBX, raw, raw[2], raw[3])

    def _try_rtcm3(self):
        buf = self._buf
        if len(buf) < 3:
            return _NEED_MORE
        # The 6 bits after the preamble are reserved and must be zero.
        if buf[1] & 0xFC:
            return _BAD
        plen = ((buf[1] & 0x03) << 8) | buf[2]
        total = 3 + plen + 3
        if len(buf) < total:
            return _NEED_MORE
        raw = bytes(buf[:total])
        # calc_crc24q() returns 0 when the message includes a valid trailing CRC.
        if calc_crc24q(raw) != 0:
            return _BAD
        del buf[:total]
        self.rtcm3_frames += 1
        return Frame(KIND_RTCM3, raw)

    def _resync(self) -> None:
        """Discard bytes up to the next plausible sync byte.

        Always drops at least one byte (the buffer is non-empty and index 0 has
        already been rejected), so this can never spin.
        """
        buf = self._buf
        nxt = -1
        for candidate in (buf.find(UBX_SYNC1, 1), buf.find(RTCM3_PREAMBLE, 1)):
            if candidate != -1 and (nxt == -1 or candidate < nxt):
                nxt = candidate
        drop = len(buf) if nxt == -1 else nxt
        self.garbage_bytes += drop
        self.resync_events += 1
        del buf[:drop]
