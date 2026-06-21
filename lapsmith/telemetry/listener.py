"""One-way UDP listener for the FH6 Data Out feed.

Read-only: opens a single UDP socket bound to 127.0.0.1:<port> and decodes
frames into Packet objects on a background thread, keeping a ring buffer of the
most recent frames. No packets are ever sent. This touches nothing the game
protects - it is exactly what any telemetry dashboard does.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from collections import deque
from typing import Deque, Optional, Callable

from .parser import parse, ParseError, Packet


class TelemetryListener:
    def __init__(self, port: int, host: str = "127.0.0.1", ring_size: int = 20000):
        self.port = port
        self.host = host
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self.buffer: Deque[Packet] = deque(maxlen=ring_size)
        self.packet_count = 0
        self.error_count = 0
        self.last_packet: Optional[Packet] = None
        self.last_packet_time: float = 0.0
        self.observed_lengths: dict[int, int] = {}   # datagram length -> count
        self.short_count = 0                          # datagrams below the FH6 base
        self._on_packet: Optional[Callable[[Packet], None]] = None

    # lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self._running.is_set():
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # On Windows, claim the port EXCLUSIVELY. SO_REUSEADDR there lets a second
        # socket bind the SAME UDP port and silently steal/split the datagrams - so
        # a relaunch while a stale instance lingers would "bind OK" yet receive
        # NOTHING ("no telemetry"). Exclusive use makes a clean quit free the port
        # immediately (UDP has no TIME_WAIT) and turns a real conflict into a loud
        # bind error instead of silent starvation. Elsewhere keep REUSEADDR.
        if sys.platform.startswith("win"):
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(0.5)
        self._sock.bind((self.host, self.port))
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="fh6-udp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the loop and RELEASE the UDP port. Idempotent - safe to call from
        the quit handler and again from the post-loop cleanup."""
        self._running.clear()
        thread, self._thread = self._thread, None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "TelemetryListener":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # internals --------------------------------------------------------------
    def _loop(self) -> None:
        assert self._sock is not None
        while self._running.is_set():
            try:
                data, _ = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            self.observed_lengths[len(data)] = self.observed_lengths.get(len(data), 0) + 1
            try:
                pkt = parse(data)
            except ParseError:
                self.error_count += 1
                if len(data) < 323:
                    self.short_count += 1
                continue
            with self._lock:
                self.buffer.append(pkt)
                self.packet_count += 1
                self.last_packet = pkt
                self.last_packet_time = time.time()
            if self._on_packet:
                self._on_packet(pkt)

    # access -----------------------------------------------------------------
    def set_callback(self, fn: Optional[Callable[[Packet], None]]) -> None:
        self._on_packet = fn

    def is_receiving(self, within_s: float = 1.0) -> bool:
        return self.last_packet is not None and (time.time() - self.last_packet_time) <= within_s

    def snapshot(self) -> Optional[Packet]:
        with self._lock:
            return self.last_packet

    def drain_since(self, marker_index: int) -> list[Packet]:
        """Return packets captured since a previous packet_count marker."""
        with self._lock:
            count = self.packet_count
            # The ring buffer may have rolled; clamp to what we still hold.
            want = count - marker_index
            if want <= 0:
                return []
            want = min(want, len(self.buffer))
            return list(self.buffer)[-want:]

    @property
    def mark(self) -> int:
        with self._lock:
            return self.packet_count


def wait_for_feed(listener: TelemetryListener, timeout_s: float = 20.0) -> bool:
    """Block until packets arrive (or timeout). Returns True if feed is live."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if listener.is_receiving(within_s=1.5):
            return True
        time.sleep(0.2)
    return listener.is_receiving(within_s=2.0)
