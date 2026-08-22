"""Persistent Bluetooth Classic RFCOMM link to a Divoom MiniToo.

The pixel protocol is NOT BLE. The device also advertises a BLE endpoint
("Divoom MiniToo-App") exposing a Microchip transparent-UART service, but the
display protocol does not answer there. It lives on Classic SPP, RFCOMM
channel 1 (advertised in SDP as JL_SPP).

CoreBluetooth/IOBluetooth delivers delegate callbacks on the run loop of the
thread that opened the channel, so whichever thread calls open() must also be
the one that calls send() and pump().
"""
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field

import objc
from Foundation import NSObject, NSRunLoop, NSDate
from IOBluetooth import IOBluetoothDevice

from .codec import ACK, READY

DEFAULT_ADDR = "B1-21-81-58-36-B6"
CHANNEL = 1


def pump(seconds: float) -> None:
    """Run the current thread's run loop, letting IOBluetooth deliver callbacks."""
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def _raw(ptr, n: int) -> bytes:
    if isinstance(ptr, (bytes, bytearray)):
        return bytes(ptr[:n])
    try:
        return bytes((ctypes.c_ubyte * n).from_address(int(ptr)))
    except Exception:
        return b""


class _Delegate(NSObject):
    def init(self):
        self = objc.super(_Delegate, self).init()
        self.buf = bytearray()
        self.events = []
        self.closed = False
        return self

    def rfcommChannelData_data_length_(self, chan, ptr, n):
        d = _raw(ptr, n)
        self.buf.extend(d)
        self.events.append((time.time(), d))

    def rfcommChannelOpenComplete_status_(self, chan, err): pass
    def rfcommChannelClosed_(self, chan): self.closed = True
    def rfcommChannelWriteComplete_refcon_status_(self, chan, refcon, err): pass
    def rfcommChannelQueueSpaceAvailable_(self, chan): pass


@dataclass
class SendResult:
    acked: bool
    t_ack: float
    req_s: float          # time waiting for the device's ready-request
    tx_s: float           # time pushing chunk packets
    ack_s: float          # time waiting for the completion ACK
    total_s: float
    nbytes: int
    packets: int

    @property
    def tx_rate(self) -> float:
        """Raw chunk throughput, bytes/sec."""
        return self.nbytes / self.tx_s if self.tx_s > 1e-6 else 0.0

    @property
    def overhead_s(self) -> float:
        """Fixed per-batch cost that does NOT scale with payload."""
        return self.req_s + self.ack_s


@dataclass
class LinkStats:
    sent: int = 0
    bytes: int = 0
    acks: int = 0
    timeouts: int = 0


class DivoomLink:
    def __init__(self, addr: str = DEFAULT_ADDR, channel: int = CHANNEL):
        self.addr, self.channel = addr, channel
        self.dev = self.chan = self._d = None
        self.stats = LinkStats()

    def open(self, drop_audio: bool = False) -> "DivoomLink":
        """Open RFCOMM. drop_audio tears down A2DP first.

        Sustained A2DP costs ~89% of RFCOMM throughput (measured 156 -> 12 KB/s)
        and inflates round-trip ~6x. Dropping it is worth it for one-shot image
        sends. macOS normally re-establishes audio a few seconds later.
        """
        self.dev = IOBluetoothDevice.deviceWithAddressString_(self.addr)
        if self.dev is None:
            raise RuntimeError(f"device {self.addr} not paired")
        if drop_audio and self.dev.isConnected():
            self.dev.closeConnection()
            pump(2.0)
        self._d = _Delegate.alloc().init()
        err, self.chan = self.dev.openRFCOMMChannelSync_withChannelID_delegate_(
            None, self.channel, self._d)
        if err or self.chan is None:
            raise RuntimeError(f"RFCOMM open failed 0x{err & 0xffffffff:x}")
        pump(0.4)
        return self

    @property
    def mtu(self) -> int:
        return int(self.chan.getMTU())

    @property
    def is_open(self) -> bool:
        return self.chan is not None and bool(self.chan.isOpen())

    def drain_events(self):
        ev = list(self._d.events)
        self._d.events.clear()
        return ev

    def send(self, pkts: list[bytes], *, wait_ready: bool = True,
             ready_timeout: float = 5.0, ack_timeout: float = 4.0,
             chunk_delay: float = 0.0) -> SendResult:
        """Push one batch: start packet, wait for ready, stream chunks, await ACK.

        wait_ready must stay True. Skipping the handshake was measured to make
        the device drop batches entirely (3 of 8 hit full ACK timeout).
        """
        self._d.buf.clear()
        t0 = time.time()
        self.chan.writeSync_length_(pkts[0], len(pkts[0]))
        if wait_ready:
            deadline = time.time() + ready_timeout
            while time.time() < deadline:
                pump(0.03)
                if READY in bytes(self._d.buf):
                    break
        t_req = time.time()
        for p in pkts[1:]:
            self.chan.writeSync_length_(p, len(p))
            if chunk_delay:
                pump(chunk_delay)
        t_tx = time.time()
        deadline = time.time() + ack_timeout
        acked = False
        while time.time() < deadline:
            pump(0.03)
            if ACK in bytes(self._d.buf):
                acked = True
                break
        t_ack = time.time()
        nbytes = sum(len(p) for p in pkts[1:])
        self.stats.sent += 1
        self.stats.bytes += nbytes
        self.stats.acks += int(acked)
        self.stats.timeouts += int(not acked)
        return SendResult(acked=acked, t_ack=t_ack, req_s=t_req - t0, tx_s=t_tx - t_req,
                          ack_s=t_ack - t_tx, total_s=t_ack - t0, nbytes=nbytes,
                          packets=len(pkts))

    def close(self) -> None:
        try:
            if self.chan is not None:
                self.chan.closeChannel()
        except Exception:
            pass
        self.chan = None
