"""Command line interface."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from .codec import LADDER, encode, square_resize, to_raw
from .link import DEFAULT_ADDR, DivoomLink, pump
from .player import Player
from .source import FrameSource, fetch_audio, resolve

SIZES = [16, 32, 48, 64, 80, 96, 112, 128]


def _fmt_stats(st, elapsed, timing):
    out = [
        "-" * 60,
        f"batches {st.batches}  frames {st.frames}  {st.bytes/1024:.0f}KB in {elapsed:.1f}s",
        f"underruns {st.underruns}/{st.batches} ({st.underrun_pct:.0f}%)  overflows {st.overflows}",
        f"truncation {st.truncated_s:.2f}s ({st.truncation_pct:.1f}% of content)",
        f"avg encode {st.encode_ms/max(st.batches,1):.0f}ms/batch",
        f"link model: overhead {timing.overhead_s*1000:.0f}ms  tx {timing.tx_rate/1024:.0f}KB/s",
        f"rung usage {dict(sorted(st.rungs.items()))} (0=best quality)",
    ]
    return "\n".join(out)


def cmd_send(a) -> int:
    from PIL import Image
    img = square_resize(Image.open(a.image), a.size, smooth=(a.scale != "nearest"))
    payload, pkts = encode([to_raw(img, a.posterize)], a.size, a.speed, a.level)
    print(f"{a.size}px  payload {len(payload)}B  packets {len(pkts)}")
    if a.dry_run:
        return 0
    link = DivoomLink(a.addr).open(drop_audio=a.drop_audio)
    try:
        r = link.send(pkts)
        print(f"{'ACK received' if r.acked else 'NO ACK'}  "
              f"tx {r.tx_rate/1024:.0f}KB/s  total {r.total_s:.2f}s")
        return 0 if r.acked else 1
    finally:
        link.close()


def cmd_play(a) -> int:
    info = resolve(a.target, a.source_height, prefer_h264=not a.allow_vp9)
    print(f"source : {info.title or info.url}")
    if info.width:
        print(f"stream : {info.width}x{info.height} {info.vcodec} (fmt {info.format_id})")
    print(f"render : {a.size}px @{a.fps}fps  zstd L{a.level}  load {a.target_load:.0%}")

    apath = None
    if a.audio:
        apath = os.path.abspath(a.audio_file)
        if not os.path.exists(apath):
            print("fetching audio...")
            fetch_audio(info.webpage_url or a.target, apath)
        print(f"audio  : {apath}")
        print("  NOTE: sustained A2DP costs ~89% of display bandwidth. Expect a")
        print("        much smaller/slower picture, or route audio elsewhere.")

    src = FrameSource(info.url, a.size, a.fps, seconds=a.secs or None, start=a.start)
    src.start()
    link = DivoomLink(a.addr).open()
    print(f"link   : open mtu={link.mtu}\n")
    player = Player(link, a.size, a.fps, target_load=a.target_load,
                    guard=a.guard, level=a.level)

    aproc = None

    def on_start():
        nonlocal aproc
        if apath:
            aproc = subprocess.Popen(["afplay", apath],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if a.av_offset > 0:
                end = time.time() + a.av_offset / 1000.0
                while time.time() < end:
                    pump(0.01)

    def on_batch(b, r, tm):
        if a.quiet:
            return
        print(f"  {player.stats.batches:>3} {b.nframes:>3}f p{b.posterize}/{b.stride} "
              f"{len(b.payload)/1024:>6.1f}KB bud={b.budget/1024:>6.1f}KB "
              f"enc={b.encode_ms:>4.0f}ms x{b.attempts} tx={r.tx_rate/1024:>5.0f}KB/s "
              f"oh={r.overhead_s*1000:>4.0f}ms load={(r.total_s)/b.play_s*100:>4.0f}%"
              f"{'' if b.fits else ' OVER'}")

    t0 = time.time()
    try:
        st = player.play(src.q, on_batch=on_batch, on_start=on_start)
    except KeyboardInterrupt:
        st = player.stats
        print("\ninterrupted")
    finally:
        src.stop()
        if aproc and aproc.poll() is None:
            aproc.terminate()
        link.close()
    if src.error:
        print(f"decoder error: {src.error}", file=sys.stderr)
    print(_fmt_stats(st, time.time() - t0, player.timing))
    return 0


def cmd_serve(a) -> int:
    from .server import serve
    return serve(host=a.host, port=a.port, addr=a.addr)


def cmd_info(a) -> int:
    link = DivoomLink(a.addr).open()
    try:
        print(f"address : {a.addr}")
        print(f"channel : RFCOMM {link.channel}")
        print(f"mtu     : {link.mtu}")
        print(f"open    : {link.is_open}")
        print("listening 5s for unsolicited frames...")
        t0 = time.time()
        while time.time() - t0 < 5:
            pump(0.25)
            for ts, d in link.drain_events():
                print(f"  t+{ts-t0:4.1f}s  {d.hex(' ')}")
        return 0
    finally:
        link.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="divoomcast", description=__doc__)
    p.add_argument("--addr", default=DEFAULT_ADDR, help="Bluetooth address")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="send a still image")
    s.add_argument("image")
    s.add_argument("--size", type=int, default=128, choices=SIZES)
    s.add_argument("--scale", choices=["smooth", "nearest"], default="smooth")
    s.add_argument("--posterize", type=int, default=None)
    s.add_argument("--speed", type=int, default=1000)
    s.add_argument("--level", type=int, default=17)
    s.add_argument("--drop-audio", action="store_true", default=True)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_send)

    v = sub.add_parser("play", help="stream a video / YouTube URL")
    v.add_argument("target")
    v.add_argument("--size", type=int, default=128, choices=SIZES)
    v.add_argument("--fps", type=float, default=15)
    v.add_argument("--secs", type=float, default=0, help="0 = whole video")
    v.add_argument("--start", type=float, default=0.0)
    v.add_argument("--guard", type=float, default=None,
               help="seconds to land early; default adapts to measured overhead")
    v.add_argument("--target-load", type=float, default=0.90)
    v.add_argument("--level", type=int, default=9)
    v.add_argument("--source-height", type=int, default=144)
    v.add_argument("--allow-vp9", action="store_true", help="do not prefer avc1")
    v.add_argument("--audio", action="store_true")
    v.add_argument("--audio-file", default="track.m4a")
    v.add_argument("--av-offset", type=float, default=200)
    v.add_argument("--quiet", action="store_true")
    v.set_defaults(func=cmd_play)

    d = sub.add_parser("serve", help="run the local HTTP daemon")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8787)
    d.set_defaults(func=cmd_serve)

    i = sub.add_parser("info", help="probe the device link")
    i.set_defaults(func=cmd_info)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
