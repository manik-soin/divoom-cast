import pytest
import zstandard as zstd
from PIL import Image

from divoomcast import codec


def test_frame_structure_and_checksum():
    f = codec.frame(0x8B, b"\x00\x01\x02\x03")
    assert f[0] == 0x01 and f[-1] == 0x02
    assert int.from_bytes(f[1:3], "little") == len(f) - 4
    assert f[3] == 0x8B
    assert codec.check_frame(f)


def test_frame_matches_known_good_capture():
    # start packet for a 195-byte payload, verified against a real device ACK
    f = codec.frame(codec.CMD_NEW_GIF, b"\x00" + codec.u32le(195))
    assert f.hex() == "010800 8b00c3000000 5601 02".replace(" ", "")


def test_checksum_detects_corruption():
    f = bytearray(codec.frame(0x8B, b"abcd"))
    f[5] ^= 0xFF
    assert not codec.check_frame(bytes(f))


def test_binary_payload_is_not_escaped():
    """0x01/0x02/0x03 inside the body must pass through untouched."""
    body = bytes([0x01, 0x02, 0x03, 0x01, 0x02])
    f = codec.frame(0x8B, body)
    assert f[4:9] == body
    assert codec.check_frame(f)


@pytest.mark.parametrize("size", [16, 32, 64, 128])
def test_payload_header_and_roundtrip(size):
    n = 3
    frames = [bytes(range(256)) * (size * size * 3 // 256) for _ in range(n)]
    frames = [f[:size * size * 3] for f in frames]
    pl = codec.build_payload(frames, size, 500)
    assert pl[0] == 0x25
    assert pl[1] == n
    assert int.from_bytes(pl[2:4], "big") == 500
    assert pl[4] == pl[5] == size // 16
    zlen = int.from_bytes(pl[6:10], "big")
    assert zlen == len(pl) - 10
    assert zstd.ZstdDecompressor().decompress(pl[10:]) == b"".join(frames)


def test_zstd_window_log_is_capped():
    """window_log > 17 is known to glitch the device decoder."""
    assert codec.WINDOW_LOG == 17


def test_packets_chunk_at_256_and_sequence():
    payload = bytes(1000)
    pkts = codec.build_packets(payload)
    assert len(pkts) == 1 + 4                      # start + ceil(1000/256)
    assert all(codec.check_frame(p) for p in pkts)
    for i, p in enumerate(pkts[1:]):
        assert p[4] == 0x01
        assert int.from_bytes(p[5:9], "little") == len(payload)
        assert int.from_bytes(p[9:11], "little") == i


def test_start_packet_declares_total_length():
    payload = bytes(700)
    start = codec.build_packets(payload)[0]
    assert start[4] == 0x00
    assert int.from_bytes(start[5:9], "little") == 700


def test_rejects_bad_size():
    with pytest.raises(ValueError):
        codec.build_payload([bytes(100 * 100 * 3)], 100, 100)   # not a multiple of 16
    with pytest.raises(ValueError):
        codec.build_payload([bytes(144 * 144 * 3)], 144, 100)   # over 128


def test_rejects_wrong_frame_length():
    with pytest.raises(ValueError):
        codec.build_payload([bytes(10)], 32, 100)


def test_rejects_too_many_frames():
    with pytest.raises(ValueError):
        codec.build_payload([bytes(16 * 16 * 3)] * 256, 16, 100)


def test_posterize_monotonically_shrinks_payload():
    """Core rate-control assumption: fewer bits => smaller encoded batch."""
    img = Image.linear_gradient("L").convert("RGB").resize((64, 64))
    frames = [codec.to_raw(img, p) for p in (None,)]
    sizes = []
    for p in (7, 6, 5, 4, 3):
        raw = [codec.to_raw(img, p)] * 4
        sizes.append(len(codec.build_payload(raw, 64, 100)))
    assert sizes == sorted(sizes, reverse=True), sizes


def test_ladder_is_ordered_best_first_and_excludes_p2():
    posts = [p for p, _ in codec.LADDER]
    strides = [s for _, s in codec.LADDER]
    assert 2 not in posts, "p2 measured visually broken"
    assert strides[0] == 1 and strides[-1] >= 2
    assert codec.LADDER[0][0] > codec.LADDER[4][0]


def test_square_resize_crops_to_square():
    img = Image.new("RGB", (400, 200), (10, 20, 30))
    out = codec.square_resize(img, 64)
    assert out.size == (64, 64)
