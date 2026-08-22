"""Divoom SPP wire format and frame encoding.

Pure functions only, no Bluetooth or CoreFoundation imports, so this module is
unit-testable without hardware.

Wire format (from the Android app's bluetooth serialiser):

    01 <declared_len_le16> <cmd> <body...> <checksum_le16> 02

`declared_len` is the total frame length minus 4. The checksum is the sum of
bytes [1 : len-3] masked to 16 bits. There is no byte stuffing; binary payloads
containing 0x01/0x02 pass through unescaped.
"""
from __future__ import annotations

import zstandard as zstd
from PIL import Image, ImageOps

CMD_NEW_GIF = 0x8B          # SPP_APP_NEW_GIF_CMD2020: photo + animation transfer
CHUNK = 256                 # app-level chunk size, fixed by the device firmware
ACK = bytes.fromhex("01090004bd5513010500380102")
READY = b"\x8b\x55\x00"     # substring of the device's "send me the data" frame
MAX_FRAMES = 255            # frame count is a single byte
WINDOW_LOG = 17             # 128 KiB. Larger windows glitch the device decoder.

u16le = lambda n: int(n).to_bytes(2, "little")
u32le = lambda n: int(n).to_bytes(4, "little")
u16be = lambda n: int(n).to_bytes(2, "big")
u32be = lambda n: int(n).to_bytes(4, "big")


def frame(cmd: int, body: bytes = b"") -> bytes:
    """Wrap a command + body in the Divoom SPP framing."""
    out = bytearray(7 + len(body))
    out[0], out[-1] = 0x01, 0x02
    out[1:3] = u16le(len(out) - 4)
    out[3] = cmd & 0xFF
    out[4:4 + len(body)] = body
    out[-3:-1] = u16le(sum(out[1:len(out) - 3]) & 0xFFFF)
    return bytes(out)


def check_frame(buf: bytes) -> bool:
    """Validate framing + checksum of an encoded frame."""
    if len(buf) < 7 or buf[0] != 0x01 or buf[-1] != 0x02:
        return False
    if int.from_bytes(buf[1:3], "little") != len(buf) - 4:
        return False
    return int.from_bytes(buf[-3:-1], "little") == sum(buf[1:len(buf) - 3]) & 0xFFFF


_CCTX: dict[tuple[int, int], zstd.ZstdCompressor] = {}


def _compressor(level: int, window_log: int = WINDOW_LOG) -> zstd.ZstdCompressor:
    key = (level, window_log)
    if key not in _CCTX:
        _CCTX[key] = zstd.ZstdCompressor(
            compression_params=zstd.ZstdCompressionParameters.from_level(
                level, window_log=window_log, write_content_size=True))
    return _CCTX[key]


def valid_size(size: int) -> int:
    if size <= 0 or size % 16 or size > 128:
        raise ValueError("size must be a positive multiple of 16, max 128")
    return size


def build_payload(frames_raw: list[bytes], size: int, speed_ms: int, level: int = 9) -> bytes:
    """Concatenate raw RGB frames into one zstd stream + the 10-byte header.

    All frames go into a SINGLE zstd stream, so the compressor's 128 KiB window
    does inter-frame matching. That, not the per-frame entropy, is where most of
    the compression comes from.
    """
    valid_size(size)
    if not frames_raw:
        raise ValueError("need at least one frame")
    if len(frames_raw) > MAX_FRAMES:
        raise ValueError(f"frame count must be <= {MAX_FRAMES}")
    if not 0 <= speed_ms <= 0xFFFF:
        raise ValueError("speed_ms must fit in uint16")
    expect = size * size * 3
    for i, f in enumerate(frames_raw):
        if len(f) != expect:
            raise ValueError(f"frame {i}: {len(f)} bytes, expected {expect}")
    z = _compressor(level).compress(b"".join(frames_raw))
    blocks = size // 16
    header = bytes([0x25, len(frames_raw)]) + u16be(speed_ms) + bytes([blocks, blocks]) + u32be(len(z))
    return header + z


def build_packets(payload: bytes) -> list[bytes]:
    """Start packet followed by sequenced 256-byte chunk packets."""
    pkts = [frame(CMD_NEW_GIF, b"\x00" + u32le(len(payload)))]
    for seq, off in enumerate(range(0, len(payload), CHUNK)):
        pkts.append(frame(CMD_NEW_GIF,
                          b"\x01" + u32le(len(payload)) + u16le(seq) + payload[off:off + CHUNK]))
    return pkts


def encode(frames_raw: list[bytes], size: int, speed_ms: int, level: int = 9):
    payload = build_payload(frames_raw, size, speed_ms, level)
    return payload, build_packets(payload)


# --- image preparation -------------------------------------------------------

def square_resize(img: Image.Image, size: int, smooth: bool = True) -> Image.Image:
    """Center-crop to square then resize. NEAREST keeps pixel art crisp."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    s = min(img.size)
    img = img.crop(((img.width - s) // 2, (img.height - s) // 2,
                    (img.width - s) // 2 + s, (img.height - s) // 2 + s))
    f = Image.Resampling.LANCZOS if smooth else Image.Resampling.NEAREST
    return img.resize((size, size), f)


def to_raw(img: Image.Image, posterize: int | None = None) -> bytes:
    """RGB888 bytes, optionally posterized.

    Posterisation is the strongest rate lever available: it costs ~0.05 ms per
    frame and cuts encoded size ~34% per bit dropped. Measured on real video,
    p5 and p4 are visually indistinguishable from the original on a 128px panel;
    p3 shows banding in dark areas; p2 is visibly broken.
    """
    if posterize is not None and posterize < 8:
        img = ImageOps.posterize(img, posterize)
    return img.tobytes("raw", "RGB")


# Quality ladder, best first. (posterize_bits, frame_stride).
# p2 is deliberately absent: measured visually broken, worse than dropping frames.
LADDER: list[tuple[int, int]] = [
    (7, 1), (6, 1), (5, 1), (4, 1), (3, 1), (4, 2), (3, 2), (4, 4),
]
