# divoom-cast

Cast images and video (including YouTube) to a **Divoom MiniToo** 128x128 pixel
panel from macOS, over Bluetooth.

Everything here was derived by measurement against real hardware. See
[docs/PROTOCOL.md](docs/PROTOCOL.md) for the wire format and
[docs/FINDINGS.md](docs/FINDINGS.md) for the performance results that shaped the
design.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/divoomcast info                      # probe the link
.venv/bin/divoomcast send picture.png          # a still image
.venv/bin/divoomcast play "https://www.youtube.com/watch?v=..."
```

## Status

| Capability | State |
|---|---|
| Still images | working |
| Video / YouTube streaming, no audio | working, 128px @ 15fps |
| Video with audio through the panel | severely limited, see below |
| Chrome extension | see `extension/` |

## The one thing to know

The MiniToo is a speaker as well as a display, and the two compete. Sustained
A2DP audio costs about **89% of the display bandwidth** (measured 156 KB/s down
to 12 KB/s, with round-trip latency rising ~6x). It recovers fully the moment
audio stops.

So: play your audio through a different output and send only video to the panel.
That keeps the full 128px @ 15fps picture. The Chrome extension is built around
this, your browser keeps the sound, the panel gets the picture.
