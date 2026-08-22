"""Local HTTP daemon: JSON API + web UI, shared with the Chrome extension.

Threading model is dictated by IOBluetooth: delegate callbacks are delivered on
the run loop of the thread that opened the RFCOMM channel. So the Bluetooth link
and the player own the MAIN thread, and the HTTP server runs on a worker. The
two communicate through a command queue.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .link import DivoomLink, pump
from .player import Player
from .source import FrameSource, resolve

STATIC = Path(__file__).parent / "static"


@dataclass
class Status:
    state: str = "idle"            # idle | resolving | playing | error
    title: str = ""
    url: str = ""
    size: int = 128
    fps: float = 15.0
    started_at: float | None = None
    error: str = ""
    batches: int = 0
    frames: int = 0
    kbytes: int = 0
    underruns: int = 0
    truncation_pct: float = 0.0
    overhead_ms: float = 0.0
    tx_kbps: float = 0.0
    rung: int = 0
    link_open: bool = False

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0


class Controller:
    """Owns the link and the player. Runs on the main thread."""

    def __init__(self, addr: str):
        self.addr = addr
        self.cmds: queue.Queue = queue.Queue()
        self.status = Status()
        self.lock = threading.Lock()
        self.link: DivoomLink | None = None
        self.player: Player | None = None
        self._running = True

    # --- called from HTTP worker threads ---
    def submit(self, cmd: dict) -> None:
        self.cmds.put(cmd)

    def snapshot(self) -> dict:
        with self.lock:
            d = asdict(self.status)
        d["elapsed"] = round(self.status.elapsed, 1)
        return d

    def request_stop(self) -> None:
        if self.player:
            self.player.stop()

    def shutdown(self) -> None:
        self._running = False
        self.request_stop()
        self.cmds.put({"action": "_quit"})

    # --- main thread ---
    def _set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self.status, k, v)

    def _ensure_link(self) -> DivoomLink:
        if self.link is None or not self.link.is_open:
            self.link = DivoomLink(self.addr).open()
        self._set(link_open=True)
        return self.link

    def _play(self, cmd: dict) -> None:
        size = int(cmd.get("size", 128))
        fps = float(cmd.get("fps", 15))
        start = float(cmd.get("start", 0) or 0)
        secs = float(cmd.get("seconds", 0) or 0)
        target = cmd["url"]

        self._set(state="resolving", url=target, size=size, fps=fps, title="",
                  error="", batches=0, frames=0, kbytes=0, underruns=0,
                  truncation_pct=0.0, started_at=time.time())
        info = resolve(target)
        self._set(title=info.title or target)

        link = self._ensure_link()
        src = FrameSource(info.url, size, fps, seconds=secs or None, start=start)
        src.start()
        self.player = Player(link, size, fps)
        self._set(state="playing")

        def on_batch(b, r, tm):
            st = self.player.stats
            self._set(batches=st.batches, frames=st.frames,
                      kbytes=int(st.bytes / 1024), underruns=st.underruns,
                      truncation_pct=round(st.truncation_pct, 1),
                      overhead_ms=round(tm.overhead_s * 1000),
                      tx_kbps=round(tm.tx_rate / 1024), rung=b.rung)

        try:
            self.player.play(src.q, on_batch=on_batch)
        finally:
            src.stop()
            self.player = None
            self._set(state="idle", started_at=None)

    def run(self) -> None:
        while self._running:
            try:
                cmd = self.cmds.get(timeout=0.2)
            except queue.Empty:
                if self.link is not None and self.link.is_open:
                    pump(0.05)
                continue
            action = cmd.get("action")
            if action == "_quit":
                break
            if action == "stop":
                self.request_stop()      # also handled inline by the HTTP layer
                continue
            if action == "play":
                # drain any queued plays so the newest request wins
                while not self.cmds.empty():
                    try:
                        nxt = self.cmds.get_nowait()
                        if nxt.get("action") == "play":
                            cmd = nxt
                    except queue.Empty:
                        break
                try:
                    self._play(cmd)
                except Exception as e:
                    self._set(state="error", error=str(e), started_at=None)
        if self.link:
            self.link.close()


def _handler(ctl: Controller):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):        # keep the console clean
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # the Chrome extension calls this from its own origin
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_OPTIONS(self):
            self._send(204, b"", "text/plain")

        def do_GET(self):
            if self.path.startswith("/api/status"):
                return self._json(200, ctl.snapshot())
            if self.path in ("/", "/index.html"):
                f = STATIC / "index.html"
                if f.exists():
                    return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                return self._send(404, b"ui not installed", "text/plain")
            if self.path == "/api/ping":
                return self._json(200, {"ok": True, "service": "divoomcast"})
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})
            if self.path.startswith("/api/play"):
                if not body.get("url"):
                    return self._json(400, {"error": "url required"})
                # If something is already playing the main thread is blocked
                # inside _play(), so interrupt it directly; the queued command
                # is picked up as soon as it unwinds.
                ctl.request_stop()
                ctl.submit({"action": "play", **body})
                return self._json(202, {"accepted": True})
            if self.path.startswith("/api/stop"):
                # MUST NOT go through the command queue: during playback the
                # main thread is inside _play() and never drains it, so a queued
                # stop would not take effect until playback ended by itself.
                # Player.stop() sets a threading.Event and is safe to call from
                # this worker thread.
                ctl.request_stop()
                return self._json(202, {"accepted": True})
            self._json(404, {"error": "not found"})
    return H


def serve(host: str = "127.0.0.1", port: int = 8787, addr: str | None = None) -> int:
    from .link import DEFAULT_ADDR
    ctl = Controller(addr or DEFAULT_ADDR)
    httpd = ThreadingHTTPServer((host, port), _handler(ctl))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"divoomcast serving on http://{host}:{port}")
    print("  UI      http://%s:%d/" % (host, port))
    print("  API     POST /api/play {url,size,fps,start}   POST /api/stop   GET /api/status")
    print("Ctrl-C to quit")
    try:
        ctl.run()
    except KeyboardInterrupt:
        print("\nshutting down")
        ctl.shutdown()
    finally:
        httpd.shutdown()
    return 0
