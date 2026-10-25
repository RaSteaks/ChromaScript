"""One bounded, idle-expiring native encoder; each returned chunk is independent."""

from __future__ import annotations

import atexit
import queue
import struct
import subprocess
import tempfile
import threading
import time
from typing import IO

from .errors import ChromaError


def _read(stream: IO[bytes], size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise EOFError("HDR encoder closed its stream")
        result.extend(part)
    return bytes(result)


class FragmentEncoder:
    """Reuse expensive VideoToolbox initialization without retaining scene source."""

    def __init__(self, args: list[str], count: int, frame_bytes: int):
        self.count = count
        # Measured single-frame low-delay requests need two drain pictures.
        # Playback batches still need twelve to empty the asynchronous pipeline;
        # using the single-frame budget there causes a five-second fallback.
        # Drain pictures never enter preview or its verified frame count.
        self.flush_frames = 2 if count == 1 else 12
        self.frame_bytes = frame_bytes
        self.parts: queue.Queue[bytes | Exception] = queue.Queue(maxsize=4)
        self.initialization = b""
        self.first = True
        self.closed = threading.Event()
        self.busy = False
        self.last_used = time.monotonic()
        self.stderr = tempfile.TemporaryFile()
        args = args.copy()
        if count == 1:
            args[-1:-1] = ["-flags", "+low_delay"]
        args[args.index("-g") + 1] = str(count + self.flush_frames)
        args[args.index("-movflags") + 1] = "+empty_moov+default_base_moof+frag_every_frame+write_colr"
        # Force an IDR at EVERY request boundary, even if the native encoder
        # independently inserts scene-change keyframes. Flush-only pictures drain
        # the preceding fragments and are never returned to a viewer.
        args[-1:] = ["-force_key_frames", f"expr:gte(n,n_forced*{count + self.flush_frames})",
                     "-flush_packets", "1", "-f", "mp4", "pipe:1"]
        self.process = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._idle, daemon=True).start()

    def _put(self, value: bytes | Exception) -> None:
        while not self.closed.is_set():
            try:
                self.parts.put(value, timeout=.2)
                return
            except queue.Full:
                continue

    def _reader(self) -> None:
        assert self.process.stdout
        fragment = bytearray()
        try:
            while not self.closed.is_set():
                header = _read(self.process.stdout, 8)
                size, kind = struct.unpack(">I4s", header)
                if size == 1:
                    extended = _read(self.process.stdout, 8)
                    size = struct.unpack(">Q", extended)[0]
                    header += extended
                if size < len(header) or size > 32 * 1024 * 1024:
                    raise ValueError("Invalid bounded MP4 box")
                box = header + _read(self.process.stdout, size - len(header))
                if kind == b"moof":
                    fragment = bytearray(box)
                elif kind == b"mdat" and fragment:
                    fragment.extend(box)
                    self._put(bytes(fragment))
                    fragment.clear()
                elif not fragment and kind in {b"ftyp", b"moov", b"free"}:
                    if len(self.initialization) + len(box) > 2 * 1024 * 1024:
                        raise ValueError("Oversized MP4 initialization")
                    self.initialization += box
        except Exception as exc:
            self._put(exc)

    def _idle(self) -> None:
        while not self.closed.wait(1):
            if not self.busy and time.monotonic() - self.last_used > 15:
                self.close()
                return

    def encode(self, pixels: bytes | bytearray) -> bytes:
        if self.closed.is_set() or len(pixels) != self.frame_bytes * self.count:
            raise ChromaError("hdr_preview_stream", "HDR 编码批次已失效。")
        self.busy = True
        failures: list[Exception] = []

        def write() -> None:
            try:
                assert self.process.stdin
                self.process.stdin.write(pixels)
                for _ in range(self.flush_frames):
                    self.process.stdin.write(memoryview(pixels)[-self.frame_bytes:])
                self.process.stdin.flush()
            except Exception as exc:
                failures.append(exc)
                self._put(exc)

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        try:
            chunks = []
            # On later calls skip all previous flush-only pictures.
            # Its following IDR has no dependency on that discarded picture.
            for index in range(self.count + (0 if self.first else self.flush_frames)):
                result = self.parts.get(timeout=5)
                if isinstance(result, Exception):
                    raise result
                if self.first or index >= self.flush_frames:
                    chunks.append(result)
            writer.join(timeout=2)
            if writer.is_alive() or failures:
                raise RuntimeError("HDR encoder input did not finish")
            self.first = False
            return self.initialization + b"".join(chunks)
        except Exception as exc:
            self.close()
            raise ChromaError("hdr_preview_stream", "HDR 硬件编码流中断，正在回退独立编码。",
                              fragments=len(chunks), initialization_bytes=len(self.initialization)) from exc
        finally:
            self.busy = False
            self.last_used = time.monotonic()

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self.process.terminate()
        try:
            # All requested fragments have already been returned, or this
            # private transient stream has failed. A pipe-blocked FFmpeg must
            # not add two seconds when switching between scrub/playback batches.
            self.process.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass  # A terminated encoder can already have closed its pipe.


_cached: tuple[tuple[str, ...], FragmentEncoder] | None = None


def encode_fragment(args: list[str], count: int, frame_bytes: int, pixels: bytes | bytearray) -> bytes:
    """Called under the shared preview encoder lease; at most one process lives."""
    global _cached
    key = tuple(args[:-1]) + (str(count), str(frame_bytes))
    if _cached is None or _cached[0] != key or _cached[1].closed.is_set():
        close_encoder()
        _cached = (key, FragmentEncoder(args, count, frame_bytes))
    return _cached[1].encode(pixels)


def close_encoder() -> None:
    global _cached
    if _cached:
        _cached[1].close()
        _cached = None


atexit.register(close_encoder)
