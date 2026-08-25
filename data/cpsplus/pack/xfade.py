"""Loop-with-tail equal-power crossfade — the software model the FPGA player
implements bit-for-bit (rtl/cpsplus_player.v §loop crossfade blend stage).

A crossfade track is stored byte-exact but keeps `n` extra samples of natural
continuation past loop_end (the "tail").  At playback the loop body plays up to
loop_end, then the tail [loop_end, loop_end+n) is blended sample-for-sample
against the loop head [loop_start, loop_start+n) with an equal-power cos/sin
weighting, after which the read pointer resumes at loop_start+n.  Steady-state
loop period stays exactly loop_end-loop_start and the stored ADX is never
re-encoded.

Fixed point matches the RTL exactly:
  * weights are Q15:  w_out(k) = round(cos((k+0.5)/n * pi/2) * 32768)
                      w_in(k)  = w_out(n-1-k)  (== round(sin(...) * 32768))
  * blend:  (w_out*tail + w_in*head + 2**14) >> 15, then clip to int16.
"""
from __future__ import annotations

import math
from pathlib import Path

Q15 = 1 << 15


def make_lut(n: int) -> list[int]:
    """Q15 equal-power weight table: lut[k] = round(cos((k+0.5)/n*pi/2)*32768).
    w_out(k) = lut[k]; w_in(k) = lut[n-1-k]."""
    return [round(math.cos((k + 0.5) / n * (math.pi / 2)) * Q15)
            for k in range(n)]


def write_lut_hex(n: int, path: str | Path) -> Path:
    """Emit the LUT as one hex word per line for Verilog $readmemh."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{w:04x}" for w in make_lut(n)) + "\n")
    return path


def _blend1(tail: int, head: int, wo: int, wi: int) -> int:
    v = (wo * tail + wi * head + (1 << 14)) >> 15
    return -32768 if v < -32768 else 32767 if v > 32767 else v


def blend_pair(tail_l: int, tail_r: int, head_l: int, head_r: int,
               k: int, lut: list[int], n: int) -> tuple[int, int]:
    wo, wi = lut[k], lut[n - 1 - k]
    return _blend1(tail_l, head_l, wo, wi), _blend1(tail_r, head_r, wo, wi)


def render(samples: list[tuple[int, int]], ls: int, le: int, n: int,
           loops: int, lut: list[int] | None = None
           ) -> list[tuple[int, int]]:
    """Reproduce the player's output pair stream for a crossfade track.

    `samples` is the fully (linearly) decoded stored stream as (l, r) int16
    pairs, covering at least [0, le+n).  Returns intro + `loops` loop passes,
    exactly as the FPGA player emits them (pass 0 = [0, le) then the crossfade;
    each further pass = [ls+n, le) then the crossfade).
    """
    if lut is None:
        lut = make_lut(n)
    if le + n > len(samples):
        raise ValueError(f"stream too short for tail: need {le + n}, "
                         f"have {len(samples)}")
    if le - ls <= n:
        raise ValueError("loop body must be longer than the crossfade")
    head = [samples[ls + k] for k in range(n)]

    def seam(out: list[tuple[int, int]]):
        for k in range(n):
            tl, tr = samples[le + k]
            hl, hr = head[k]
            out.append(blend_pair(tl, tr, hl, hr, k, lut, n))

    out: list[tuple[int, int]] = []
    out.extend(samples[i] for i in range(0, le))     # pass 0 intro
    seam(out)
    for _ in range(loops):
        out.extend(samples[i] for i in range(ls + n, le))
        seam(out)
    return out
