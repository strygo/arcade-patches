"""Saturn MUS stream decoder.

Format (proven — internal research notes addendum d):
headerless linear PCM, 16-bit big-endian, STEREO, 32000 Hz,
channel-block-interleaved in 4096-byte blocks ([ch0:4096][ch1:4096]...).
All files are multiples of 8192 bytes, so loops align to whole interleave
periods.  Loop point = the SXX0/SXX1 file boundary itself.

Channel order CONFIRMED (followup_a): first 4096-byte block =
LEFT, verified by channel-wise cross-correlation against the Anthology
zero6 ADX stereo decodes of the same recordings.  Do not swap.

Promoted from tools/mus2wav.py; stdlib-only (array module) version.
"""
from __future__ import annotations

import array
import sys

MUS_RATE = 32000
MUS_CHANNELS = 2
BLOCK = 4096                 # bytes per channel block
BLOCK_SAMPLES = BLOCK // 2   # s16 samples per channel block


def decode_mus(data: bytes, swap: bool = False) -> bytes:
    """Decode raw MUS bytes to s16le interleaved stereo PCM bytes."""
    nb = len(data) // BLOCK
    if nb % 2:
        nb -= 1  # ignore a trailing odd block (never seen; files are 8192*k)
    data = data[:nb * BLOCK]
    a = array.array("h")
    a.frombytes(data)
    if sys.byteorder == "little":
        a.byteswap()  # source is big-endian
    ch0 = array.array("h")
    ch1 = array.array("h")
    for blk in range(0, nb, 2):
        s = blk * BLOCK_SAMPLES
        ch0 += a[s:s + BLOCK_SAMPLES]
        ch1 += a[s + BLOCK_SAMPLES:s + 2 * BLOCK_SAMPLES]
    if swap:
        ch0, ch1 = ch1, ch0
    out = array.array("h", bytes(4 * len(ch0)))
    out[0::2] = ch0
    out[1::2] = ch1
    if sys.byteorder == "big":
        out.byteswap()  # emit little-endian
    return out.tobytes()
