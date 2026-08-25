"""CRI ADX codec support: header parse/synthesis, coefficients, ffmpeg bridge.

References (in-repo):
  reference/adx/cri_adx_file.wiki   — container header (v3/v4 loop blocks)
  reference/adx/cri_adx_adpcm.wiki  — codec math + coefficient formula
  reference/ffmpeg/adx.c            — ff_adx_calculate_coeffs (lrint variant)
  reference/ffmpeg/adxenc.c/adxdec.c

Notes for the eventual RTL decoder:
  * ffmpeg's decoder uses `scale = frame_scale` while the multimedia.cx wiki
    (and CRI's own decoder, reportedly) uses `scale + 1`.  Everything in this
    toolchain (encode AND decode) goes through ffmpeg, so Phase-1 comparisons
    are self-consistent; the RTL/testbench must pick one and be compared
    against the same choice.  Difference is <= 1 LSB per residual step.
  * Coefficients: ffmpeg uses lrint(); the wiki uses floor().  We store the
    ffmpeg (lrint) values in the pack index, matching the streams' encoder.

All PCM in this module is s16le channel-interleaved bytes.
"""
from __future__ import annotations

import math
import struct
import subprocess
from dataclasses import dataclass

import os as _os
import shutil as _shutil
# ADX encode/decode shells out to ffmpeg (the reference codec this module's
# tables were verified against).  Resolution order: $CPSPLUS_FFMPEG, PATH.
# NOTE for byte-reproducibility: pack ADX bytes depend on ffmpeg's adxenc
# implementation (unchanged upstream for many years; shipped packs were
# encoded with ffmpeg 7.x/homebrew).  A differing ffmpeg build fails the
# byte-gates rather than shipping silently different audio.
FFMPEG = _os.environ.get("CPSPLUS_FFMPEG") or _shutil.which("ffmpeg")
if not FFMPEG:
    raise ImportError(
        "ffmpeg not found -- install it (brew install ffmpeg) or set "
        "CPSPLUS_FFMPEG to the binary path; ADX packs cannot build without it")
FRAME_BYTES = 18
FRAME_SAMPLES = 32
COEFF_BITS = 12
DEFAULT_CUTOFF = 500


@dataclass
class AdxInfo:
    data_offset: int          # absolute file offset of the first audio frame
    encoding: int
    frame_size: int
    channels: int
    sample_rate: int
    total_samples: int
    cutoff: int
    version: int              # 3 or 4
    flags: int
    loop_flag: int = 0
    loop_start_sample: int = 0
    loop_start_byte: int = 0  # absolute file offsets, per CRI convention
    loop_end_sample: int = 0
    loop_end_byte: int = 0


def parse_header(buf: bytes) -> AdxInfo:
    """Parse a CRI ADX container header (v3 or v4 loop block)."""
    if len(buf) < 0x18 or buf[0] != 0x80 or buf[1] != 0x00:
        raise ValueError("not an ADX header")
    (copyright_off,) = struct.unpack_from(">H", buf, 2)
    info = AdxInfo(
        data_offset=copyright_off + 4,
        encoding=buf[4], frame_size=buf[5], channels=buf[7],
        sample_rate=struct.unpack_from(">I", buf, 8)[0],
        total_samples=struct.unpack_from(">I", buf, 0xc)[0],
        cutoff=struct.unpack_from(">H", buf, 0x10)[0],
        version=buf[0x12], flags=buf[0x13])
    if info.encoding != 3 or info.frame_size != FRAME_BYTES:
        raise ValueError(f"unsupported ADX encoding {info.encoding}/"
                         f"frame {info.frame_size}")
    if info.version == 3 and copyright_off + 4 >= 0x2c and len(buf) >= 0x2c:
        (info.loop_flag, info.loop_start_sample, info.loop_start_byte,
         info.loop_end_sample, info.loop_end_byte) = struct.unpack_from(
            ">5I", buf, 0x18)
    elif info.version == 4 and copyright_off + 4 >= 0x38 and len(buf) >= 0x38:
        (info.loop_flag, info.loop_start_sample, info.loop_start_byte,
         info.loop_end_sample, info.loop_end_byte) = struct.unpack_from(
            ">5I", buf, 0x24)
    return info


def calc_coeffs(cutoff: int, sample_rate: int) -> tuple[int, int]:
    """ffmpeg's ff_adx_calculate_coeffs (lrint variant), COEFF_BITS=12."""
    a = math.sqrt(2.0) - math.cos(2.0 * math.pi * cutoff / sample_rate)
    b = math.sqrt(2.0) - 1.0
    c = (a - math.sqrt((a + b) * (a - b))) / b
    # lrint = round half away from zero on typical libc; the values here are
    # far from .5 boundaries for every rate we use, so round() is equivalent.
    return int(round(c * 2.0 * (1 << COEFF_BITS))), \
        int(round(-(c * c) * (1 << COEFF_BITS)))


def synth_header(channels: int, sample_rate: int, total_samples: int,
                 cutoff: int = DEFAULT_CUTOFF) -> bytes:
    """Minimal 36-byte v3 header, identical shape to ffmpeg's encoder output.
    Prepend to a raw frame stream to make it a decodable .adx."""
    return (struct.pack(">HHBBBBIIHBB", 0x8000, 32, 3, FRAME_BYTES, 4,
                        channels, sample_rate, total_samples, cutoff, 3, 0)
            + struct.pack(">IIH", 0, 0, 0) + b"(c)CRI")


def stream_bytes_for_samples(samples: int, channels: int) -> int:
    """Bytes of ADX frame stream covering `samples` samples (ceil to frame)."""
    return ((samples + FRAME_SAMPLES - 1) // FRAME_SAMPLES) \
        * FRAME_BYTES * channels


def samples_to_stream_byte(sample: int, channels: int) -> int:
    """Stream byte offset of a frame-aligned sample position."""
    if sample % FRAME_SAMPLES:
        raise ValueError(f"sample {sample} not {FRAME_SAMPLES}-aligned")
    return sample // FRAME_SAMPLES * FRAME_BYTES * channels


def _run_ffmpeg(args: list[str], input_bytes: bytes) -> bytes:
    proc = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error",
                           *args],
                          input=input_bytes, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode()[:500]}")
    return proc.stdout


def decode(stream: bytes, channels: int, sample_rate: int,
           total_samples: int | None = None,
           cutoff: int = DEFAULT_CUTOFF) -> bytes:
    """Decode a header-less ADX frame stream to s16le interleaved PCM."""
    fb = FRAME_BYTES * channels
    if len(stream) % fb:
        raise ValueError("stream not frame-aligned")
    if total_samples is None:
        total_samples = len(stream) // fb * FRAME_SAMPLES
    adx = synth_header(channels, sample_rate, total_samples, cutoff) + stream
    pcm = _run_ffmpeg(["-f", "adx", "-i", "pipe:0",
                       "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"], adx)
    want = total_samples * 2 * channels
    if len(pcm) < want:
        raise RuntimeError(f"short decode: {len(pcm)} < {want}")
    return pcm[:want]


def decode_file_bytes(adx_file_bytes: bytes) -> tuple[bytes, AdxInfo]:
    """Decode a complete .adx file (with its own header) to PCM, exactly
    total_samples long."""
    info = parse_header(adx_file_bytes)
    pcm = _run_ffmpeg(["-f", "adx", "-i", "pipe:0",
                       "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"],
                      adx_file_bytes)
    want = info.total_samples * 2 * info.channels
    if len(pcm) < want:
        raise RuntimeError(f"short decode: {len(pcm)} < {want}")
    return pcm[:want], info


def encode(pcm: bytes, channels: int, sample_rate: int) -> bytes:
    """Encode s16le interleaved PCM to a header-less ADX frame stream.

    PCM is zero-padded to a whole 32-sample frame.  Returns exactly
    ceil(samples/32) frames (EOF/dummy frames stripped).
    """
    bpf = 2 * channels
    if len(pcm) % bpf:
        raise ValueError("pcm not sample-aligned")
    n = len(pcm) // bpf
    pad = (-n) % FRAME_SAMPLES
    if pad:
        pcm = pcm + b"\0" * (pad * bpf)
        n += pad
    out = _run_ffmpeg(["-f", "s16le", "-ar", str(sample_rate),
                       "-ac", str(channels), "-i", "pipe:0",
                       "-c:a", "adpcm_adx", "-f", "adx", "pipe:1"], pcm)
    info = parse_header(out)
    if info.channels != channels or info.sample_rate != sample_rate:
        raise RuntimeError("encoder header mismatch")
    stream = out[info.data_offset:]
    want = n // FRAME_SAMPLES * FRAME_BYTES * channels
    if len(stream) < want:
        raise RuntimeError(f"short encode: {len(stream)} < {want}")
    stream = stream[:want]
    if struct.unpack_from(">H", stream, 0)[0] & 0x8000:
        raise RuntimeError("encode produced EOF frame at stream start")
    return stream


def py_decode(stream: bytes, channels: int, coef1: int, coef2: int,
              scale_plus_one: bool = False) -> list[list[int]]:
    """Reference pure-Python ADX decoder (per cri_adx_adpcm.wiki; the
    scale_plus_one=False default matches ffmpeg).  Slow — selftest use only.
    Returns one int list per channel."""
    out = [[] for _ in range(channels)]
    hist = [[0, 0] for _ in range(channels)]
    fb = FRAME_BYTES
    pos = 0
    while pos + fb * channels <= len(stream):
        for ch in range(channels):
            frame = stream[pos:pos + fb]
            pos += fb
            scale = (frame[0] << 8) | frame[1]
            if scale & 0x8000:
                return out
            if scale_plus_one:
                scale += 1
            s1, s2 = hist[ch]
            o = out[ch]
            for i in range(FRAME_SAMPLES):
                b = frame[2 + (i >> 1)]
                nib = (b >> 4) if (i & 1) == 0 else (b & 0x0f)
                if nib & 8:
                    nib -= 16
                s0 = nib * scale + ((coef1 * s1 + coef2 * s2) >> COEFF_BITS)
                s0 = max(-32768, min(32767, s0))
                s2, s1 = s1, s0
                o.append(s0)
            hist[ch] = [s1, s2]
    return out
