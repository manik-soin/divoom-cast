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


def test_min_play_scales_with_overhead():
    assert Timing(overhead_s=1.1).min_play_s() > Timing(overhead_s=0.45).min_play_s()


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
