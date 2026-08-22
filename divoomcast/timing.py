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
    err_hi: float = 0.25         # high-side estimate of predict() error

    def predict(self, nbytes: int) -> float:
        """Expected wall time to deliver a payload of nbytes."""
        return self.overhead_s + nbytes / max(self.tx_rate, 1.0)

    def budget(self, play_s: float, target_load: float = 0.9) -> int:
        """Bytes deliverable within play_s, after paying fixed overhead."""
        usable = play_s * target_load - self.overhead_s
        return max(0, int(usable * self.tx_rate))

    # Empirically the best silent-link operating point is ~1.6 s of playback per
    # batch (24 frames at 15fps), measured at 2 underruns in 57 batches.
    FLOOR_PLAY_S = 1.6

    def min_play_s(self, headroom: float = 2.5) -> float:
        """Shortest batch worth sending.

        MUST keep a constant floor. Driving batch length purely from measured
        overhead is unstable: the device's ready-request latency grows with the
        declared payload size, so longer batch -> larger payload -> higher
        overhead -> longer batch. That loop was measured running away from
        1.33 s to 2.05 s per batch and tripling the underrun rate.

        The adaptive term only takes over when overhead is genuinely large
        (A2DP contention pushes it from ~0.45 s to ~1.1 s), where long batches
        really are required to amortise the round trip.
        """
        return max(self.FLOOR_PLAY_S, self.overhead_s * headroom)

    def guard_s(self, lo: float = 0.30, hi: float = 1.2) -> float:
        """How early to aim, sized from measured prediction error.

        Truncation is structurally equal to how early a batch lands, so guard
        should be as large as observed under-prediction demands and no larger.

        The 0.30 s floor is measured, not arbitrary: it is the margin at the best
        point found on the underrun/truncation frontier (2 underruns in 57
        batches at 13% truncation). Below it underruns climb sharply without
        buying back much truncation.
        """
        return max(lo, min(hi, 2.0 * self.err_hi))

    def observe(self, overhead_s: float, nbytes: int, tx_s: float) -> None:
        """Unbiased update. Accuracy belongs here; safety margin belongs in guard."""
        a = self.alpha
        self.overhead_s = (1 - a) * self.overhead_s + a * overhead_s
        if tx_s > 0.05 and nbytes > 4096:      # ignore buffer-absorbed sends
            self.tx_rate = (1 - a) * self.tx_rate + a * (nbytes / tx_s)

    def observe_error(self, predicted_s: float, actual_s: float) -> None:
        """Track how badly predict() UNDER-estimates; guard is sized from this.

        Deliberately one-sided. Over-prediction is harmless to correctness (the
        batch simply lands early); under-prediction is what causes an underrun.
        Biasing predict() itself to be pessimistic also works but pays for the
        margin twice, which measured as 22% of content truncated.
        """
        err = max(0.0, actual_s - predicted_s)
        w = 0.5 if err > self.err_hi else 0.08     # adopt misses fast, forget slowly
        self.err_hi = (1 - w) * self.err_hi + w * err


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
