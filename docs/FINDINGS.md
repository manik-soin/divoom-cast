# Measured findings

Every number here came from this device. Where a result contradicts an earlier
assumption, the earlier assumption is recorded too, because several of them were
wrong in instructive ways.

## 1. A2DP audio and the display compete, badly

The MiniToo is a speaker and a panel, sharing one ACL link. Controlled A/B with
a fixed incompressible payload:

| phase | tx rate | effective | ACK latency |
|---|---|---|---|
| silent | 156 KB/s | 92 KB/s | 191 ms |
| sustained audio | **12 KB/s** | **10 KB/s** | **1216 ms** |
| audio still playing | 12 KB/s | 10 KB/s | 1196 ms |
| silent again | 156 KB/s | 90 KB/s | 198 ms |

**Sustained A2DP costs ~89% of display bandwidth**, and it recovers completely
once audio stops.

An earlier version of this test reported only a 24% penalty and concluded the
two coexist fine. That test was wrong: the tone was 5 s but the measurement
burst took ~8 s, so ~40% of the "audio playing" phase actually ran in silence.
**Design consequence:** route audio somewhere other than the panel.

## 2. Effective throughput is not the link rate

A batch costs `overhead + nbytes/tx_rate`, where overhead (mandatory ready
handshake + ACK) is ~450 ms silent and ~1100 ms under contention. Budgeting
against the 160 KB/s raw rate rather than the ~90 KB/s effective rate was the
single largest source of underruns.

`req` latency **scales with declared payload size** (the device sizes a buffer
before signalling ready), so larger batches do not amortise it away when silent.
Under contention, where latency dominates instead, larger batches do help. The
two regimes want opposite things.

## 3. Posterisation beats compression level

Per batch of 24 frames at 128px, real video:

| zstd level | ratio | encode time |
|---|---|---|
| 6 | 4.4:1 | 9 ms |
| 9 | 4.6:1 | 12 ms |
| 17 | 5.2:1 | 81 ms |
| 19 | 5.5:1 | 142 ms |

Level 17 buys 12% size for 7x the CPU. Only `window_log` is constrained by the
device; level is free. **Use 9.**

Posterisation at level 6, same batch:

| bits | ratio | bytes/frame |
|---|---|---|
| 7 | 2.0 | 24932 |
| 5 | 4.4 | 11151 |
| 4 | 6.7 | 7319 |
| 3 | 9.7 | 5051 |

Costs ~1 ms per batch. Visually p5 and p4 are indistinguishable from the source
on a 128px panel, p3 bands in dark areas, **p2 is broken** and is excluded from
the quality ladder in favour of dropping frames.

## 4. Prefer avc1 over vp9

yt-dlp's plain `worstvideo` often selects VP9. At 256x144, decode measured
~10 ms/frame for vp09 versus **1.3 ms/frame** for avc1. Also: request the
smallest available stream, output is at most 128px.

Profiling also showed a libswscale filter graph was *slower* than PIL
(2.0 vs 1.3 ms/frame) because of Python-level push/pull overhead per frame.

## 5. Scheduling: an underrun/truncation frontier

No playback-position feedback exists, so scheduling is open loop, re-anchored on
each ACK. Batch N+1 is timed to land `guard` before batch N ends. Two artifacts
trade off directly, and **truncation is structurally equal to `guard`**:

- land early -> the tail of the current batch is replaced before it plays
- land late  -> the panel repeats or freezes

Measured on identical 90 s content, 128px @ 15fps:

| configuration | underruns | truncation |
|---|---|---|
| lumped over-predicting model, guard 0.30 | 2/57 | 13.0% |
| affine accurate model, guard 0.25 | 7/62 | 11.6% |
| batch length driven purely by overhead | 15/44 | 11.1% |
| pessimistic (asymmetric) estimator | 4/57 | 22.0% |
| accurate predict + error-sized guard | 9/57 | 11.6% |
| shipped: accurate predict, guard floor 0.30 | 8/57 | 15.4% |

These runs are single samples on varying content, so differences of a few
batches are inside the noise. Treat the frontier as real and the ordering within
it as provisional.

### Adaptive batch length is unstable without a floor

Setting batch length from measured overhead alone runs away: longer batch ->
larger payload -> higher `req` latency -> longer batch. Observed climbing from
1.33 s to 2.05 s per batch and tripling underruns. `Timing.FLOOR_PLAY_S` breaks
the loop; the adaptive term only engages under real contention.

### An accurate predictor is not what a scheduler wants

The affine model predicts mean send time correctly, which means it is late about
half the time, because overhead swings 150-900 ms batch to batch. The margin has
to live somewhere. Putting it in `predict()` (pessimistic estimator) pays for it
twice and truncated 22% of content; sizing `guard` from observed one-sided
prediction error is the better split.
