"""Link timing model and rate control.

The single most important correctness fix in this project: a batch's send time
is NOT proportional to its size. It is

    send_time = overhead + nbytes / tx_rate

where `overhead` (the mandatory ready-request handshake plus the completion ACK)
is roughly constant per batch and `tx_rate` is the chunk streaming rate.

Modelling those as one lumped "effective throughput" number is what caused
persistent underruns: a value fitted on large batches over-predicts throughput
for small ones and vice versa. Measured on a MiniToo:

    silent : overhead ~0.45 s, tx_rate ~160 KB/s
    audio  : overhead ~1.10 s, tx_rate ~ 12 KB/s   (sustained A2DP contention)

Because overhead is fixed per batch, the usable byte budget for a batch that
will play for `play_s` is whatever fits in the time left AFTER overhead. That is
also why a batch must play for meaningfully longer than the overhead to be
viable at all.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Timing:
    overhead_s: float = 0.45
    tx_rate: float = 150 * 1024.0
    alpha: float = 0.3           # EWMA weight for new observations

    def predict(self, nbytes: int) -> float:
        """Expected wall time to deliver a payload of nbytes."""
        return self.overhead_s + nbytes / max(self.tx_rate, 1.0)

    def budget(self, play_s: float, target_load: float = 0.9) -> int:
        """Bytes deliverable within play_s, after paying fixed overhead."""
        usable = play_s * target_load - self.overhead_s
        return max(0, int(usable * self.tx_rate))

    def min_play_s(self, headroom: float = 3.0) -> float:
        """Shortest batch worth sending: overhead must be a small fraction."""
        return self.overhead_s * headroom

    def observe(self, overhead_s: float, nbytes: int, tx_s: float) -> None:
        a = self.alpha
        self.overhead_s = (1 - a) * self.overhead_s + a * overhead_s
        if tx_s > 0.05 and nbytes > 4096:      # ignore buffer-absorbed sends
            self.tx_rate = (1 - a) * self.tx_rate + a * (nbytes / tx_s)


@dataclass
class RateController:
    """Walks a quality ladder to keep each batch inside its byte budget.

    Two inputs drive it: the hard byte budget (predictive, from Timing) and the
    observed delivery slack (reactive: positive means batches finish early, so
    there is headroom to spend on quality).
    """
    ladder_len: int
    rung: int = 2
    slack_up: float = 0.08       # seconds of spare time before raising quality
    slack_down: float = -0.02    # seconds late before dropping quality

    def bias(self, slack_s: float) -> None:
        if slack_s < self.slack_down and self.rung < self.ladder_len - 1:
            self.rung += 1
        elif slack_s > self.slack_up and self.rung > 0:
            self.rung -= 1

    def overshoot(self, nbytes: int, budget: int) -> bool:
        """Step down after a miss. Returns True if another attempt is worthwhile."""
        if self.rung >= self.ladder_len - 1:
            return False
        self.rung = min(self.ladder_len - 1,
                        self.rung + (2 if nbytes > 2 * max(budget, 1) else 1))
        return True
