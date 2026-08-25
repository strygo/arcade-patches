"""Shared audio layer: WAV in, ADX frame stream out, with an SNR check.

Every pack whose audio is re-encoded rather than copied from the disc goes
through here -- Final Fight, its OST editions, Mega Twins and Forgotten
Worlds.  It lives in its own module because it is genuinely shared: as part of
build_ffight_arrange it put a Final Fight file on three unrelated builders' import
path, so a missing numpy surfaced as a Final Fight traceback while building
Mega Twins.

numpy is imported lazily and only by snr_db, so packs that copy their audio
straight off the disc do not need it at all.
"""
from __future__ import annotations

import math
import wave
from pathlib import Path

from . import adxcodec


def read_wav(path: Path) -> tuple[bytes, int, int, int]:
    """Whole file as s16le interleaved PCM -> (pcm, rate, channels, frames)."""
    with wave.open(str(path), "rb") as w:
        rate, ch, sw, n = (w.getframerate(), w.getnchannels(),
                           w.getsampwidth(), w.getnframes())
        if sw != 2:
            raise ValueError(f"{path.name}: expected 16-bit, got {sw * 8}")
        if rate != 44100 or ch != 2:
            raise ValueError(f"{path.name}: expected 44100 Hz stereo, "
                             f"got {rate} Hz {ch}ch")
        pcm = w.readframes(n)
    if len(pcm) != n * ch * 2:
        raise ValueError(f"{path.name}: short read")
    return pcm, rate, ch, n


def snr_db(ref: bytes, test: bytes) -> float:
    """Signal-to-noise ratio in dB of `test` against `ref` (s16le, equal
    length).  numpy-only: 315 MB of samples is far past pure-Python range."""
    import numpy as np                      # lazy: only the ADX path needs it
    a = np.frombuffer(ref, dtype="<i2").astype(np.float64)
    b = np.frombuffer(test, dtype="<i2").astype(np.float64)
    n = min(a.size, b.size)
    a, b = a[:n], b[:n]
    sig = float(np.dot(a, a))
    err = float(np.dot(a - b, a - b))
    if err == 0.0:
        return math.inf
    return 10.0 * math.log10(sig / err)


def encode_adx_track(pcm: bytes, rate: int, ch: int, n: int, name: str,
                     measure: bool) -> tuple[bytes, int, int, float | None]:
    """PCM -> (adx frame stream, coef1, coef2, snr_db or None).

    The coefficients stored in the pack index come from adxcodec.calc_coeffs at
    the encoder's own cutoff, so index and stream agree — everything here
    (encode AND decode) goes through the same ffmpeg, per adxcodec's docstring.
    """
    stream = adxcodec.encode(pcm, ch, rate)
    want = adxcodec.stream_bytes_for_samples(n, ch)
    if len(stream) != want:
        raise ValueError(f"{name}: encoder returned {len(stream)} B, "
                         f"expected {want} B for {n} samples")
    coef1, coef2 = adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF, rate)

    # Round-trip the stream through our OWN header synthesis + parser, so the
    # pack's stream is proven to be a thing this toolchain can read back.
    adx_file = adxcodec.synth_header(ch, rate, n) + stream
    info = adxcodec.parse_header(adx_file)
    if (info.channels, info.sample_rate, info.total_samples) != (ch, rate, n):
        raise ValueError(f"{name}: parse_header round-trip mismatch")
    if len(adx_file) - info.data_offset != len(stream):
        raise ValueError(f"{name}: header/stream length disagreement")

    snr = None
    if measure:
        decoded = adxcodec.decode(stream, ch, rate, total_samples=n)
        if len(decoded) != len(pcm):
            raise ValueError(f"{name}: decode length {len(decoded)} != "
                             f"source {len(pcm)}")
        snr = snr_db(pcm, decoded)
    return stream, coef1, coef2, snr
