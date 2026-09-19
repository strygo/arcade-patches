"""Deterministic sample-rate conversion: ffmpeg's swresample, redone in-repo.

Two kinds of pack are resampled.  The HD Remix album is mostly 44.1 kHz; the
X68000 MIDI exports convert Nuked-SC55's 64 kHz float captures.  Both were
first resampled by ffmpeg, whose swresample runs in float and takes different
rounding on different SIMD paths, so the same source gave packs that differed
between a Mac and a PC and failed the pinned hashes.  Here the conversion is
the same resampler, with every output bit decided the same way everywhere.

Shared with swresample (ffmpeg 8.1.2), whatever the output format:

  * the filter bank: build_filter's Kaiser-windowed sinc (beta 9, cutoff
    0.97 when downsampling), worked in decimal arithmetic (no libm) and
    rounded to double, then float32, as swr stores it -- taps32(), with its
    integer form pinned by sha256 below;
  * exact_rational phases (160 for 44.1 -> 48 kHz, 3 for 64 -> 48 kHz), where
    linear_interp is a no-op because its fraction is always 0;
  * the first output centred on input 0 with the history mirrored about
    sample 0, and taps/2 samples mirrored about the last one at the flush,
    giving ceil(frames * L / M) outputs.

Two ways to run the sum:

  convolve_f32  the exact arm64 float computation: four accumulator lanes of
                fused multiply-adds (fmla), summed (0 + 1) + (2 + 3), each
                step rounded to float32 exactly by fma32() from IEEE-754
                operations every CPU rounds identically.  Bit-exact with the
                arm64 builds.  Used for the X68000 exports: all 27 Daimakaimura
                songs match the Mac's 24-bit FLAC audio bit for bit.
  convolve      int64: samples times taps held at 2**coef_bits (integer
                matmul, never BLAS).  Within 1 LSB of the arm64 build; used
                for HD Remix, whose pinned table and pack predate the exact
                path, with the arm64 NEON s16 conversion in to_s16:

  float -> s16, 2-channel arm64 path    truncate toward zero at Q31, then
    (NEON: fcvtzs #31, then >> 16)        arithmetic >> 16, saturate

For HD Remix two things are not reproduced, and each moves single samples by
one LSB: float32's own rounding inside the 32-tap sum (about 0.03% of
samples), and ffmpeg converting the last few samples of each 4096-sample FLAC
frame with the C lrintf instead of NEON (about 0.06%).  Measured over the
album: every sample within 1 LSB of the arm64 build, 99.8% identical.  No
dither, as swresample's default.

The HD Remix filter (factor 1, as that is upsampling): tap i of
phase p at r = (i - 15) - p/160 is sinc(pi*r) * I0(9*sqrt(1 - (r/16)**2)),
divided by the sum of phase 0's taps; phases 81..159 are phases 79..1 reversed,
as swr copies them.  Measured on the integer table: flat to 0.0001 dB below
16 kHz, -0.1 dB at 19.2 kHz, -3 dB at 21.3 kHz, -6 dB at 22.05 kHz; images
are 25 dB down above 24.1 kHz and 90 dB down above 26 kHz.  That is the sound
that was reviewed.

A 3-minute stereo track takes about 0.2 s through convolve (one int64 matmul
per phase over a strided view, 8 threads) and about 4 s through convolve_f32
(64 -> 48 kHz, 88 taps; about 0.7 microseconds per output frame).
"""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
import math
from math import gcd

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

FILTER_SIZE = 32               # swr's default filter_size
PHASE_LIMIT = 1 << 10          # swr phase_shift 10
KAISER_BETA = 9
CUTOFF = 0.97                  # swr's cutoff when none is given
COEF_BITS = 36
_PREC = 40
_PI = Decimal("3.14159265358979323846264338327950288419716939937510582097494")
# sha256 of the (phases, taps) little-endian int64 table, keyed by
# (src_rate, dst_rate, filter_size, coef_bits).  The design is exact, so a
# digest changes only if the construction does -- and then every resampled
# pack changes with it, which the pinned pack hashes must hear about.
TABLE_SHA256 = {
    (44100, 48000, 32, 36): "1726d10b2d7ebcb7f1d0c91e5df76f15c32e33d67b85c7d13f499a1a73600a3a",
}
_BLOCK = 1 << 12
_JOB_ROWS = 1 << 18
_F32_BLOCK = 1 << 16


def sin_pi(r: Fraction, prec: int = _PREC) -> Decimal:
    """sin(pi*r) in decimal arithmetic, folded into [0, pi/2] so the Taylor
    series is short.  No libm: the same digits on every machine."""
    with localcontext() as ctx:
        ctx.prec = prec
        r %= 2
        neg = r >= 1
        if neg:
            r -= 1
        if r > Fraction(1, 2):
            r = 1 - r
        x = _PI * r.numerator / r.denominator
        x2, term, total, k = x * x, x, x, 1
        eps = Decimal(10) ** -(prec - 2)
        while abs(term) > eps:
            term = -term * x2 / ((2 * k) * (2 * k + 1))
            total += term
            k += 1
        return +(-total if neg else total)


def _i0(q: Decimal) -> Decimal:
    """Modified Bessel I0 at z where q = (z/2)**2: sum q**k / (k!)**2."""
    term, total, k = Decimal(1), Decimal(1), 1
    eps = Decimal(10) ** -(_PREC - 2)
    while term > eps * total:
        term = term * q / (k * k)
        total += term
        k += 1
    return total


def design(src_rate: int, dst_rate: int,
           filter_size: int = FILTER_SIZE) -> tuple[int, int, int, float]:
    """(L, M, taps, factor) as swr's resample_init derives them: L exact-
    rational phases, M input samples per L outputs, the tap count, and the
    cutoff factor (1 when upsampling)."""
    g = gcd(src_rate, dst_rate)
    L, M = dst_rate // g, src_rate // g
    if L > PHASE_LIMIT:
        # swr would fall back to 1024 interpolated phases; no source needs it
        raise ValueError(f"{src_rate} -> {dst_rate} Hz needs more than "
                         f"{PHASE_LIMIT} phases")
    # the same double arithmetic swr does (IEEE +,*,/ are exact-rounded, so
    # every machine gets the same factor and tap count)
    factor = min(dst_rate * CUTOFF / src_rate, 1.0)
    taps = max(math.ceil(filter_size / factor), 1)
    if taps % 2:
        raise ValueError(f"{src_rate} -> {dst_rate} Hz: odd tap count {taps}")
    return L, M, taps, factor


@lru_cache(maxsize=None)
def taps32(src_rate: int, dst_rate: int,
           filter_size: int = FILTER_SIZE) -> np.ndarray:
    """swr's float32 filter bank, shape (L, taps).  Tap i of row p weights
    input sample floor(t) - center + i, where the output's input time t has
    fractional part p/L.  Worked in decimal arithmetic (no libm) and rounded
    to double, then float32, as swr's build_filter stores y / norm."""
    L, _, taps, factor = design(src_rate, dst_rate, filter_size)
    center = (taps - 1) // 2
    fac = Fraction(factor)
    # an even phase count is built half-way and mirrored, as swr does
    built = L // 2 + 1 if L % 2 == 0 else L
    with localcontext() as ctx:
        ctx.prec = _PREC
        b2 = Decimal(KAISER_BETA * KAISER_BETA) / 4
        half = []
        for p in range(built):
            row = []
            for i in range(taps):
                r = Fraction(i - center) - Fraction(p, L)
                rf = r * fac
                y = Decimal(1) if r == 0 else (
                    sin_pi(rf) / (_PI * rf.numerator / rf.denominator))
                w2 = min((2 * r / taps) ** 2, Fraction(1))
                y *= _i0(b2 * (1 - Decimal(w2.numerator) / w2.denominator))
                row.append(y)
            half.append(row)
        norm = sum(half[0])
        rows = np.zeros((L + 1, taps), dtype="<f4")
        for p, row in enumerate(half):
            rows[p] = [np.float32(float(y / norm)) for y in row]
            if L % 2 == 0:
                # swr mirrors each computed phase into L - p as it goes; at
                # p = L/2 that copy runs over its own row, so the first half wins
                for i in range(taps):
                    rows[L - p, taps - 1 - i] = rows[p, i]
    return rows[:L]


def table_sha256(src_rate: int, dst_rate: int, filter_size: int = FILTER_SIZE,
                 coef_bits: int = COEF_BITS) -> str:
    """The pinned digest for a rate pair, looked up the one way.

    TABLE_SHA256 is keyed by all four values the table depends on; a caller
    that spells the key itself gets it wrong the moment the design gains a
    parameter, which is how a build report started raising KeyError.
    """
    try:
        return TABLE_SHA256[(src_rate, dst_rate, filter_size, coef_bits)]
    except KeyError:
        raise KeyError(f"no pinned resampler table for {src_rate}->{dst_rate} "
                       f"filter_size {filter_size} coef_bits {coef_bits}") from None


@lru_cache(maxsize=None)
def table(src_rate: int, dst_rate: int, filter_size: int = FILTER_SIZE,
          coef_bits: int = COEF_BITS) -> np.ndarray:
    """taps32() as integers, value * 2**coef_bits, rounded exactly."""
    scale = 1 << coef_bits
    t = taps32(src_rate, dst_rate, filter_size)
    h = np.array([[round(Fraction(float(v)) * scale) for v in row] for row in t],
                 dtype="<i8")
    want = TABLE_SHA256.get((src_rate, dst_rate, filter_size, coef_bits))
    if want and hashlib.sha256(h.tobytes()).hexdigest() != want:
        raise AssertionError(f"resampler table {src_rate}->{dst_rate} "
                             f"filter_size {filter_size} changed")
    return h


def convolve(channel, frames: int, channels: int, src_rate: int,
             dst_rate: int, convert, dtype, filter_size: int = FILTER_SIZE,
             coef_bits: int = COEF_BITS) -> np.ndarray:
    """swr's filter over integer samples: channel(k) gives channel k as int64,
    convert(acc) turns sums of sample * tap (taps at 2**coef_bits) into the
    output dtype.  ceil(frames * L / M) frames, as swr emits after its flush.

    The caller keeps |sample| * sum|taps| inside int64 (sum|taps| < 2**1.3
    for the filters used here)."""
    h = table(src_rate, dst_rate, filter_size, coef_bits)
    L, M, taps, _ = design(src_rate, dst_rate, filter_size)
    c = (taps - 1) // 2
    if frames <= 2 * taps:
        raise ValueError("track too short to resample")
    n_out = -(-frames * L // M)
    out = np.empty((n_out, channels), dtype=dtype)
    inv = pow(M, -1, L)

    def job(win, k: int, p: int, q0: int, q1: int):
        dst = out[p * inv % L::L, k]
        for q in range(q0, q1, _BLOCK):
            rows = win[q:min(q + _BLOCK, q1)]
            dst[q:q + len(rows)] = convert(rows @ h[p])

    with ThreadPoolExecutor(min(8, os.cpu_count() or 1)) as pool:
        for k in range(channels):
            x = channel(k)
            # ext[i + c] is input sample i: swr mirrors c samples about sample
            # 0 into its history, and taps/2 about the last one when flushing
            ext = sliding_window_view(
                np.concatenate((x[c:0:-1], x, x[:-taps // 2 - 1:-1])), taps)
            jobs = []
            for p in range(L):
                # outputs j0, j0+L, ... share phase p; windows start M apart
                j0 = p * inv % L
                win = ext[j0 * M // L::M]
                rows = len(range(j0, n_out, L))
                jobs += [pool.submit(job, win, k, p, q, min(q + _JOB_ROWS, rows))
                         for q in range(0, rows, _JOB_ROWS)]
            # every job writes its own output samples: threads cannot change bits
            for f in jobs:
                f.result()
            del ext, x
    return out


def fma32(p: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Fused multiply-add rounded once to float32, as AArch64's fmla does:
    p is the exact product of two float32 values held in float64 (always
    exact), c the float32 accumulator held in float64.

    The float64 sum may be inexact, but TwoSum recovers its error exactly,
    and that error can only change the float32 rounding when the float64 sum
    sits exactly on a float32 midpoint -- which its sign then settles.  Only
    IEEE-754 operations that every conforming CPU rounds identically (+, -,
    *, conversion, nextafter) are used, in a fixed order, so no SIMD path or
    CPU can move a bit.  Returns the float32 result, in float64."""
    s = p + c
    bb = s - p
    err = (p - (s - bb)) + (c - bb)
    r = s.astype(np.float32).astype(np.float64)
    # a midpoint s has 2s - r exactly on the neighbouring float32
    other = 2 * s - r
    cand = np.flatnonzero((err != 0) & (other != r) &
                          (other.astype(np.float32) == other))
    if len(cand):
        sc, rc, ec = s.flat[cand], r.flat[cand], err.flat[cand]
        oc = other.flat[cand]
        lo, hi = np.minimum(rc, oc), np.maximum(rc, oc)
        adjacent = np.nextafter(lo.astype(np.float32), np.float32(np.inf)) == hi
        mid = adjacent & (lo + hi == 2 * sc)
        r.flat[cand] = np.where(mid, np.where(ec > 0, hi, lo), rc)
    return r


def neon_dot(win: np.ndarray, h: np.ndarray) -> np.ndarray:
    """swr's aarch64 resample_common_float over rows of float32 windows: four
    accumulator lanes, tap i fused-multiply-added into lane i % 4, then the
    lanes summed pairwise, (0 + 1) + (2 + 3), in float32.  Bit-exact with the
    arm64 build the canonical X68000 packs came from."""
    n, taps = win.shape
    # every exact product at once, laid out (block, row, lane) and contiguous
    prod = np.ascontiguousarray(
        (win.astype(np.float64).reshape(n, taps // 4, 4)
         * h.astype(np.float64).reshape(taps // 4, 4)).transpose(1, 0, 2))
    lanes = np.zeros((n, 4), dtype=np.float64)
    for p in prod:
        lanes = fma32(p, lanes)
    l32 = lanes.astype(np.float32)
    return (l32[:, 0] + l32[:, 1]) + (l32[:, 2] + l32[:, 3])


def convolve_f32(channel, frames: int, channels: int, src_rate: int,
                 dst_rate: int, convert, dtype, filter_size: int = FILTER_SIZE,
                 limit: int | None = None) -> np.ndarray:
    """swr's float resampler over float32 samples, reproduced exactly: the
    same float32 taps, windows, reflections and arm64 accumulation order.
    channel(k) gives channel k as float32; convert(v) maps the float32 output
    samples to dtype.  ceil(frames * L / M) frames, or the first `limit` of
    them (identical to the head of the full conversion)."""
    t = taps32(src_rate, dst_rate, filter_size)
    L, M, taps, _ = design(src_rate, dst_rate, filter_size)
    if taps % 4:
        raise ValueError("the arm64 accumulation needs a tap count divisible by 4")
    c = (taps - 1) // 2
    if frames <= 2 * taps:
        raise ValueError("track too short to resample")
    n_out = -(-frames * L // M)
    if limit is not None:
        n_out = min(n_out, limit)
    out = np.empty((n_out, channels), dtype=dtype)
    inv = pow(M, -1, L)

    def job(win, k: int, p: int, q0: int, q1: int):
        dst = out[p * inv % L::L, k]
        for q in range(q0, q1, _F32_BLOCK):
            rows = win[q:min(q + _F32_BLOCK, q1)]
            dst[q:q + len(rows)] = convert(neon_dot(rows, t[p]))

    # float32 samples are small enough to hold every channel at once
    exts = []
    for k in range(channels):
        x = channel(k)
        exts.append(sliding_window_view(
            np.concatenate((x[c:0:-1], x, x[:-taps // 2 - 1:-1])), taps))
    jobs = []
    step = max(_F32_BLOCK, -(-n_out // (L * 64)))
    with ThreadPoolExecutor(min(8, os.cpu_count() or 1)) as pool:
        for k, ext in enumerate(exts):
            for p in range(L):
                j0 = p * inv % L
                win = ext[j0 * M // L::M]
                rows = len(range(j0, n_out, L))
                jobs += [pool.submit(job, win, k, p, q, min(q + step, rows))
                         for q in range(0, rows, step)]
        # every job writes its own output samples: threads cannot change bits
        for f in jobs:
            f.result()
    return out


def to_s16(acc: np.ndarray, frac_bits: int) -> np.ndarray:
    """acc / 2**frac_bits full-scale-1.0 float as arm64 swr converts it to
    s16: truncate toward zero at Q31, arithmetic shift right 16, saturate."""
    shift = frac_bits - 31
    if shift <= 0:
        q31 = acc << -shift
    else:
        q31 = np.where(acc >= 0, acc >> shift, -((-acc) >> shift))
    return np.clip(q31 >> 16, -32768, 32767).astype("<i2")


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int,
             extra_bits: int = 0) -> np.ndarray:
    """(frames, channels) integers worth s16 * 2**extra_bits (0 or 8) at
    src_rate, as int16 at dst_rate, ceil(frames * L / M) frames long."""
    if extra_bits not in (0, 8):
        raise ValueError("16- or 24-bit sources only")
    if src_rate == dst_rate:
        return to_s16(pcm.astype(np.int64) << (8 - extra_bits), 23)
    # every source is carried as 24-bit, a value of 2**23 being full scale
    return convolve(lambda k: pcm[:, k].astype(np.int64) << (8 - extra_bits),
                    len(pcm), pcm.shape[1], src_rate, dst_rate,
                    lambda acc: to_s16(acc, COEF_BITS + 23), "<i2")
