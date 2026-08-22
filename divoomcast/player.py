"""Batch scheduler: turns a frame stream into timed uploads.

The device exposes NO playback-position feedback (its only unsolicited traffic
is a fixed 2 s keepalive), so scheduling is open loop, re-anchored on each ACK.
Batch N+1 is timed to land just before batch N finishes playing.

Batch length is chosen from the measured overhead rather than fixed: a batch
must play for meaningfully longer than the per-batch handshake cost or it can
never keep up. Under A2DP contention overhead rises ~2.5x, and the batch length
grows to match automatically.
"""
from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass, field

from PIL import Image

from .codec import LADDER, MAX_FRAMES, encode, to_raw
from .link import DivoomLink, pump
from .timing import RateController, Timing


@dataclass
class PlayStats:
    batches: int = 0
    frames: int = 0
    bytes: int = 0
    underruns: int = 0
    overflows: int = 0
    encode_ms: float = 0.0
    nominal_play_s: float = 0.0
    actual_cycle_s: float = 0.0
    truncated_s: float = 0.0
    rungs: dict = field(default_factory=dict)

    @property
    def truncation_pct(self) -> float:
        return 100.0 * self.truncated_s / self.nominal_play_s if self.nominal_play_s else 0.0

    @property
    def underrun_pct(self) -> float:
        return 100.0 * self.underruns / self.batches if self.batches else 0.0


@dataclass
class Batch:
    payload: bytes
    packets: list
    nframes: int
    speed_ms: int
    posterize: int
    stride: int
    rung: int
    budget: int
    attempts: int
    encode_ms: float

    @property
    def play_s(self) -> float:
        return self.nframes * self.speed_ms / 1000.0

    @property
    def fits(self) -> bool:
        return len(self.payload) <= self.budget


class _Encoder(threading.Thread):
    def __init__(self, src_q, out_q, size, base_speed_ms, timing, shared, level):
        super().__init__(daemon=True)
        self.src_q, self.out_q = src_q, out_q
        self.size, self.base_speed_ms = size, base_speed_ms
        self.timing, self.shared, self.level = timing, shared, level
        self.ctl = RateController(ladder_len=len(LADDER))
        self.error: Exception | None = None
        self._stop = threading.Event()

    def stop(self): self._stop.set()

    def _want_frames(self) -> int:
        """Batch length from measured overhead, clamped to protocol limits."""
        want = math.ceil(self.timing.min_play_s() / (self.base_speed_ms / 1000.0))
        return max(12, min(MAX_FRAMES, want))

    def run(self):
        try:
            eof = False
            while not eof and not self._stop.is_set():
                want = self._want_frames()
                imgs: list[Image.Image] = []
                while len(imgs) < want:
                    item = self.src_q.get()
                    if item is None:
                        eof = True
                        break
                    imgs.append(item)
                if not imgs:
                    break

                play_s = len(imgs) * self.base_speed_ms / 1000.0
                budget = self.timing.budget(play_s, self.shared["target_load"])
                self.ctl.bias(self.shared["slack"])

                t0 = time.perf_counter()
                attempts = 0
                while True:
                    post, stride = LADDER[self.ctl.rung]
                    sel = imgs[::stride]
                    raw = [to_raw(im, post) for im in sel]
                    speed = self.base_speed_ms * stride
                    payload, pkts = encode(raw, self.size, speed, self.level)
                    attempts += 1
                    if len(payload) <= budget or attempts >= 3:
                        break
                    if not self.ctl.overshoot(len(payload), budget):
                        break
                self.out_q.put(Batch(payload=payload, packets=pkts, nframes=len(sel),
                                     speed_ms=speed, posterize=post, stride=stride,
                                     rung=self.ctl.rung, budget=budget, attempts=attempts,
                                     encode_ms=(time.perf_counter() - t0) * 1000))
        except Exception as e:
            self.error = e
        finally:
            self.out_q.put(None)


class Player:
    """Drives a FrameSource onto a DivoomLink. Runs on the link's own thread."""

    def __init__(self, link: DivoomLink, size: int, fps: float, *,
                 target_load: float = 0.90, guard: float | None = None, level: int = 9):
        self.link, self.size, self.fps = link, size, fps
        self.base_speed_ms = max(1, round(1000.0 / fps))
        self.guard, self.level = guard, level   # guard=None -> adaptive from overhead
        self.timing = Timing()
        self.shared = {"slack": 0.0, "target_load": target_load}
        self.stats = PlayStats()
        self._stop = threading.Event()

    def stop(self): self._stop.set()

    def play(self, frame_q, *, on_batch=None, on_start=None) -> PlayStats:
        out_q: queue.Queue = queue.Queue(maxsize=2)
        enc = _Encoder(frame_q, out_q, self.size, self.base_speed_ms,
                       self.timing, self.shared, self.level)
        enc.start()

        deadline = prev_ack = prev_play = None
        started = False
        try:
            while not self._stop.is_set():
                batch = out_q.get()
                if batch is None:
                    break
                if not started:
                    started = True
                    if on_start:
                        on_start()

                est = self.timing.predict(len(batch.payload))
                if deadline is not None:
                    g = self.timing.guard_s() if self.guard is None else self.guard
                    wait = (deadline - est - g) - time.time()
                    if wait > 0:
                        end = time.time() + wait
                        while time.time() < end and not self._stop.is_set():
                            pump(0.02)
                if self._stop.is_set():
                    break

                r = self.link.send(batch.packets)
                self.timing.observe(r.overhead_s, r.nbytes, r.tx_s)
                self.timing.observe_error(est, r.total_s)

                if deadline is not None and r.t_ack > deadline:
                    self.stats.underruns += 1
                if prev_ack is not None:
                    cycle = r.t_ack - prev_ack
                    self.stats.actual_cycle_s += cycle
                    self.stats.nominal_play_s += prev_play
                    self.stats.truncated_s += max(0.0, prev_play - cycle)
                    self.shared["slack"] = 0.6 * self.shared["slack"] + 0.4 * (prev_play - cycle)
                prev_ack, prev_play = r.t_ack, batch.play_s
                deadline = r.t_ack + batch.play_s

                s = self.stats
                s.batches += 1
                s.frames += batch.nframes
                s.bytes += r.nbytes
                s.encode_ms += batch.encode_ms
                s.overflows += int(not batch.fits)
                s.rungs[batch.rung] = s.rungs.get(batch.rung, 0) + 1
                if on_batch:
                    on_batch(batch, r, self.timing)

            if deadline and not self._stop.is_set():
                while time.time() < deadline:
                    pump(0.05)
        finally:
            enc.stop()
        if enc.error:
            raise enc.error
        return self.stats
