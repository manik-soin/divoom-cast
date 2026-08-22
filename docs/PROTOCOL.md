# Divoom MiniToo wire protocol

Reconstructed by measurement against a real device. Independently corroborated
by [alvinunreal/divoom-minitoo-osx](https://github.com/alvinunreal/divoom-minitoo-osx).

## Transport

**Not BLE.** The device does advertise a BLE endpoint named `Divoom MiniToo-App`
exposing a Microchip/ISSC transparent-UART service
(`49535343-fe7d-4ae5-8fa9-9fafd205e455`). Correctly framed Divoom commands
written there are accepted at the GATT layer and produce **no response**. The
display protocol does not live there.

It lives on **Bluetooth Classic SPP, RFCOMM channel 1**. SDP on the paired
device shows:

```
JL_SPP     rfcomm_channel=1      <- the display protocol
JL_SPP     rfcomm_channel=10
SMS/MMS    rfcomm_channel=17
JL_HFP     rfcomm_channel=4
JL_A2DP    l2cap_psm=25
JL_HID     l2cap_psm=17
```

`JL` is Jieli, the SoC vendor. Negotiated RFCOMM MTU is 666.

## Framing

```
01 <declared_len_le16> <cmd> <body...> <checksum_le16> 02
```

- `declared_len` = total frame length minus 4
- `checksum` = sum of bytes `[1 : len-3]`, masked to 16 bits
- **No byte stuffing.** Binary payloads containing `0x01`/`0x02` pass through
  unescaped. Verified against a device ACK.

## Image / animation transfer, command `0x8b`

`SPP_APP_NEW_GIF_CMD2020`. A batch upload, not a frame stream.

1. **Start packet** — body `00 <total_payload_len_le32>`
2. **Device replies** `01 07 00 04 8b 55 00 01 ec 00 02` ("ready"). This
   handshake is **mandatory**; skipping it makes the device drop batches
   (measured: 3 of 8 hit a full ACK timeout).
3. **Chunk packets** — body `01 <total_len_le32> <seq_le16> <chunk>`, chunks of
   256 bytes, sequence from 0.
4. **Device ACKs** `01 09 00 04 bd 55 13 01 05 00 38 01 02`

### Payload layout

```
25              format marker
<frame_count>   1 byte, so max 255 frames per batch
<speed_be16>    per-frame duration, milliseconds
<rows>          size/16
<cols>          size/16
<zstd_len_be32>
<zstd_frame>    all frames concatenated, compressed as ONE zstd stream
```

Frames are raw RGB888 at `size x size`, where `size` is a multiple of 16 up to
128. Compressing them as a single stream is what makes this viable: zstd's
window does the inter-frame matching. **`window_log` must be 17** (128 KiB);
larger windows glitch the device decoder.

## Other traffic

The device emits `01 0d 00 04 f7 55 ...` every ~2 seconds. The payload is byte
identical every time, including across animation boundaries. It is a keepalive.

**There is no playback-position feedback anywhere in this protocol.** That is
the single most consequential constraint for anything trying to stream.
