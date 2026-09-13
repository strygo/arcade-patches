"""Shared audio layer: WAV in, ADX frame stream out, with an SNR check.

Every pack whose audio is re-encoded rather than copied from the disc goes
through here, so it has no dependency on any one game's builder.

numpy is imported lazily and only by snr_db, so packs that copy their audio
straight off the disc, or skip the SNR measurement, do not need it at all.
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


def subtract_s16_dc(pcm: bytes, channels: int, baseline: tuple[int, ...],
                    *, evidence_frames: int = 0, name: str = "audio") -> bytes:
    """Subtract a known per-channel DC pedestal without changing timing.

    FFmpeg's ADX encoder handles ordinary zero-centered material well, but a
    constant non-zero input is pathological for its integer frame-scale
    choice: predictor decay can exceed a scale-1 nibble, producing periodic
    recovery frames that sound like a quiet note.  Do not estimate DC from
    music.  Callers must provide a measured renderer-specific baseline and
    may require an exact constant run at the head as source evidence.

    Raises instead of clipping if centering would exceed signed 16-bit range.
    """
    import numpy as np

    if channels <= 0 or len(baseline) != channels:
        raise ValueError(f"{name}: baseline does not match channel count")
    samples = np.frombuffer(pcm, dtype="<i2")
    if samples.size % channels:
        raise ValueError(f"{name}: PCM ends mid-frame")
    frames = samples.reshape(-1, channels)
    if not len(frames):
        raise ValueError(f"{name}: empty PCM")
    dc = np.asarray(baseline, dtype=np.int32)
    if evidence_frames:
        if len(frames) < evidence_frames:
            raise ValueError(f"{name}: too short for DC evidence window")
        if not np.all(frames[:evidence_frames] == dc):
            raise ValueError(
                f"{name}: first {evidence_frames} frames do not carry the "
                f"expected DC pedestal {tuple(baseline)}")
    centered = frames.astype(np.int32) - dc
    lo, hi = int(centered.min()), int(centered.max())
    if lo < -32768 or hi > 32767:
        raise ValueError(
            f"{name}: DC subtraction would clip ({lo}..{hi})")
    return centered.astype("<i2").tobytes()


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
