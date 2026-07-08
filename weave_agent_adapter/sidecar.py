"""Sidecar (specs 03/04): receive wire events on a Unix socket and trace them.

Hosts one `Tracer` per harness (routed on `WireEvent.harness`) sharing a single
sink, so concurrent harnesses trace side by side. Singleton via an advisory
`flock` (extra spawns fail the lock and exit); scales to zero after `idle_s`
with no events.
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import threading
import time

from .core.model import WireEvent
from .profile import load_profile
from .tracer import Tracer

ACCEPT_TIMEOUT_S = 0.5


class Sidecar:
    def __init__(self, sink, project, socket_path, profiles_dir=None, idle_s=120.0):
        self.sink = sink
        self.project = project
        self.socket_path = socket_path
        self.profiles_dir = profiles_dir
        self.idle_s = idle_s
        self.tracers: dict = {}
        self._stop = threading.Event()
        self._lock_fd = None
        self._last = 0.0

    def _tracer_for(self, harness: str) -> Tracer:
        tr = self.tracers.get(harness)
        if tr is None:
            tr = Tracer(load_profile(harness, self.profiles_dir), self.project, self.sink)
            self.tracers[harness] = tr
        return tr

    def _handle_line(self, raw: bytes) -> None:
        try:
            d = json.loads(raw)
            wire = WireEvent(
                v=d.get("v", 1), harness=d["harness"], event=d["event"],
                captured_at=float(d["captured_at"]),
                payload=d.get("payload") or {}, pid=int(d.get("pid", 0)),
            )
        except Exception:
            return
        # one bad event or profile must never take down the sidecar
        try:
            self._tracer_for(wire.harness).handle(wire)
        except Exception:
            pass

    def _acquire_singleton_lock(self) -> bool:
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        fd = open(self.socket_path + ".lock", "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            return False
        self._lock_fd = fd
        return True

    def serve(self) -> None:
        if not self._acquire_singleton_lock():
            return                       # another sidecar already owns this socket
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        srv.listen(64)
        srv.settimeout(ACCEPT_TIMEOUT_S)
        self._last = time.time()
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    if time.time() - self._last > self.idle_s:
                        break            # idle: scale to zero
                    continue
                except OSError:
                    break
                with conn:
                    conn.settimeout(1.0)
                    buf = b""
                    try:
                        while True:
                            chunk = conn.recv(65536)
                            if not chunk:
                                break
                            buf += chunk
                    except OSError:
                        pass
                    for line in buf.split(b"\n"):
                        if line.strip():
                            self._handle_line(line)
                self._last = time.time()
        finally:
            srv.close()
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)
            if self._lock_fd:
                self._lock_fd.close()

    def stop(self) -> None:
        self._stop.set()
