"""Sidecar (specs 03/04): receive wire events on a Unix socket and trace them.

Hosts one `Tracer` per harness (routed on `WireEvent.harness`) sharing the turn
emitters, so concurrent harnesses trace side by side. Singleton via an advisory
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
    def __init__(self, project, socket_path, profiles_dir=None, idle_s=120.0,
                 redactor=None, session_rate=1.0, session_ttl=3600.0, sweep_interval=30.0,
                 project_per_repo=False, turn_emitters=None, turn_linger=120.0):
        self.project = project
        self.socket_path = socket_path
        self.profiles_dir = profiles_dir
        self.idle_s = idle_s
        self.redactor = redactor
        self.session_rate = session_rate
        self.project_per_repo = project_per_repo
        self.turn_emitters = turn_emitters or []
        self.session_ttl = session_ttl        # drop sessions idle past this (crash safety)
        self.turn_linger = turn_linger        # finalize a closed turn after this much quiet
        self.sweep_interval = sweep_interval
        self.tracers: dict = {}
        self._stop = threading.Event()
        self._lock_fd = None
        self._last = 0.0

    def _tracer_for(self, harness: str) -> Tracer:
        tr = self.tracers.get(harness)
        if tr is None:
            tr = Tracer(load_profile(harness, self.profiles_dir), self.project,
                        redactor=self.redactor, session_rate=self.session_rate,
                        project_per_repo=self.project_per_repo,
                        turn_emitters=self.turn_emitters)
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
        last_sweep = self._last
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    now = time.time()
                    if now - self._last > self.idle_s:
                        break            # idle: scale to zero
                    if now - last_sweep > self.sweep_interval:
                        self._sweep(now)
                        last_sweep = now
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
                now = time.time()
                self._last = now
                if now - last_sweep > self.sweep_interval:
                    self._sweep(now)
                    last_sweep = now
        finally:
            self._sweep(time.time(), ttl=0.0)   # finalize any still-open sessions on exit
            srv.close()
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)
            if self._lock_fd:
                self._lock_fd.close()

    def _sweep(self, now: float, ttl: float = None) -> None:
        ttl = self.session_ttl if ttl is None else ttl
        for tr in self.tracers.values():
            try:
                tr.finalize_idle_turns(now, self.turn_linger)
                tr.sweep(now, ttl)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
