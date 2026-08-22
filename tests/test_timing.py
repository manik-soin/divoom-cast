from divoomcast.timing import RateController, Timing


def test_predict_is_affine_not_proportional():
    """send_time = overhead + bytes/rate. A pure ratio model cannot fit both."""
    t = Timing(overhead_s=0.45, tx_rate=160 * 1024)
    small, large = t.predict(1024), t.predict(160 * 1024)
    assert small == 0.45 + 1024 / (160 * 1024)
    assert large == 0.45 + 1.0
    # a proportional model would predict 160x the time; the affine one does not
    assert large / small < 4


def test_budget_subtracts_fixed_overhead_first():
    t = Timing(overhead_s=0.5, tx_rate=100 * 1024)
    assert t.budget(2.0, 1.0) == int(1.5 * 100 * 1024)


def test_budget_is_zero_when_overhead_exceeds_playback():
    t = Timing(overhead_s=1.2, tx_rate=12 * 1024)
    assert t.budget(1.0, 0.9) == 0


def test_min_play_has_a_constant_floor():
    """Regression: purely-adaptive batch length is unstable, overhead grows with
    payload which grows with batch length. The floor breaks that loop."""
    assert Timing(overhead_s=0.45).min_play_s() == Timing.FLOOR_PLAY_S
    assert Timing(overhead_s=0.10).min_play_s() == Timing.FLOOR_PLAY_S


def test_min_play_adapts_only_under_heavy_overhead():
    assert Timing(overhead_s=1.1).min_play_s() > Timing.FLOOR_PLAY_S


def test_observe_moves_estimates_toward_observation():
    t = Timing(overhead_s=0.45, tx_rate=160 * 1024, alpha=0.5)
    t.observe(overhead_s=1.5, nbytes=100 * 1024, tx_s=10.0)
    assert 0.45 < t.overhead_s < 1.5
    assert t.tx_rate < 160 * 1024


def test_observe_ignores_buffer_absorbed_sends():
    """Tiny/instant writes sit in the RFCOMM buffer and report absurd rates."""
    t = Timing(tx_rate=150 * 1024)
    before = t.tx_rate
    t.observe(overhead_s=0.4, nbytes=512, tx_s=0.0001)
    assert t.tx_rate == before


def test_controller_drops_quality_when_late():
    c = RateController(ladder_len=8, rung=2)
    c.bias(-0.5)
    assert c.rung == 3


def test_controller_raises_quality_when_early():
    c = RateController(ladder_len=8, rung=3)
    c.bias(0.5)
    assert c.rung == 2


def test_controller_clamps_at_ends():
    c = RateController(ladder_len=3, rung=2)
    c.bias(-1.0)
    assert c.rung == 2
    c = RateController(ladder_len=3, rung=0)
    c.bias(1.0)
    assert c.rung == 0


def test_overshoot_jumps_two_rungs_when_far_over():
    c = RateController(ladder_len=8, rung=0)
    c.overshoot(nbytes=1000, budget=100)
    assert c.rung == 2


def test_overshoot_returns_false_at_last_rung():
    c = RateController(ladder_len=4, rung=3)
    assert c.overshoot(nbytes=1000, budget=1) is False


def test_guard_is_sized_from_prediction_error():
    t = Timing(err_hi=0.0)
    assert t.guard_s() == 0.30                       # measured floor
    t.observe_error(predicted_s=1.0, actual_s=1.8)   # under-predicted by 0.8
    assert t.guard_s() > 0.60
    assert Timing(err_hi=99.0).guard_s() == 1.2      # ceiling


def test_over_prediction_does_not_inflate_guard():
    """Landing early is harmless; only misses should widen the margin."""
    t = Timing(err_hi=0.0)
    for _ in range(5):
        t.observe_error(predicted_s=2.0, actual_s=0.5)
    assert t.guard_s() == 0.30


def test_min_play_headroom_keeps_overhead_a_minority_of_the_batch():
    for oh in (0.3, 0.45, 1.1, 1.5):
        t = Timing(overhead_s=oh)
        assert t.overhead_s / t.min_play_s() <= 0.40, oh


def test_guard_adopts_misses_fast_and_forgets_slowly():
    t = Timing(err_hi=0.0)
    t.observe_error(1.0, 2.0)
    spiked = t.err_hi
    assert spiked > 0.4
    for _ in range(4):
        t.observe_error(1.0, 1.0)
    assert t.err_hi > spiked * 0.4, "must not forget a miss immediately"
