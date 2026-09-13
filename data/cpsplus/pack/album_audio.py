"""Decode album tracks for the packs built from soundtrack releases.

The Double Impact and HD Remix packs are cut from album files (MP3 and FLAC)
rather than from a game disc, and the kit rebuilds them on the user's machine
against pinned pack hashes.  So the decode must give the same PCM on every
CPU, which ffmpeg's defaults do not: its float MP3 decoder (mp3float) and
swresample round differently depending on the SIMD path taken (measured with
`-cpuflags 0`: most frames of a track moved by an LSB).  The chain here uses
only integer steps:

  MP3   ffmpeg's fixed-point decoder (`-c:a mp3`), whose native output is s16;
  FLAC  ffmpeg's FLAC decoder, lossless by definition, widened to s32 (an
        exact shift; 24-bit FLAC is s32 natively);
  both  handed over as WAV at the file's own rate and channel count, then
        brought to 48 kHz s16 by pack/resample.py: the resampler the reviewed
        packs were made with (swresample's default), redone in integer
        arithmetic, which is also where 24-bit sources lose their low byte.
        A 48 kHz 16-bit source passes through untouched.

ffmpeg is the executable adxcodec resolves ($CPSPLUS_FFMPEG, then PATH).
"""
from __future__ import annotations

import hashlib
import struct
import subprocess
from pathlib import Path

import numpy as np

from . import adxcodec, resample

RATE = 48000

# extension -> (forced decoder, PCM codec it is handed over in, bytes/sample).
# Forcing the decoder matters for MP3: left to itself ffmpeg picks mp3float.
_CHAINS = {
    ".mp3": ("mp3", "pcm_s16le", 2),
    ".flac": ("flac", "pcm_s32le", 4),
}


def sha256(path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _wav_pcm(buf: bytes) -> tuple[int, int, int, bytes]:
    """(channels, rate, bits, sample bytes) of ffmpeg's piped WAV output.

    A pipe cannot be seeked back to fill in the sizes, so the data chunk is
    simply the rest of the stream."""
    if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise RuntimeError("ffmpeg did not produce WAV")
    pos, fmt = 12, None
    while pos + 8 <= len(buf):
        cid, size = buf[pos:pos + 4], struct.unpack_from("<I", buf, pos + 4)[0]
        if cid == b"fmt ":
            _, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", buf, pos + 8)
            fmt = ch, rate, bits
        elif cid == b"data":
            if fmt is None:
                break
            return (*fmt, buf[pos + 8:])
        pos += 8 + size + (size & 1)
    raise RuntimeError("ffmpeg WAV output has no fmt/data chunk")


def decode_native(path) -> tuple[np.ndarray, int, int]:
    """(samples, rate, extra_bits): the file's first audio stream at its own
    rate as int32, shape (frames, channels), each value s16 * 2**extra_bits."""
    path = Path(path)
    chain = _CHAINS.get(path.suffix.lower())
    if chain is None:
        raise ValueError(f"{path.name}: no deterministic decode for "
                         f"{path.suffix or 'this file'} (MP3 and FLAC only)")
    decoder, codec, width = chain
    proc = subprocess.run(
        [adxcodec.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-c:a", decoder, "-i", str(path), "-map", "0:a:0",
         "-map_metadata", "-1", "-c:a", codec, "-f", "wav", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode {path.name}: "
                           f"{proc.stderr.decode(errors='replace')[:500]}")
    ch, rate, bits, data = _wav_pcm(proc.stdout)
    if bits != 8 * width or ch not in (1, 2):
        raise RuntimeError(f"{path.name}: unexpected {ch} ch / {bits}-bit output")
    data = data[:len(data) // (width * ch) * width * ch]
    pcm = np.frombuffer(data, f"<i{width}").reshape(-1, ch).astype(np.int32)
    if width == 4:
        # s32 carries up to 24 significant bits; keep exactly those
        if np.any(pcm & 0xff):
            raise ValueError(f"{path.name}: deeper than 24-bit")
        pcm >>= 8
    if ch == 1:
        pcm = np.repeat(pcm, 2, axis=1)
    return pcm, rate, 8 if width == 4 else 0


def decode(path) -> np.ndarray:
    """The file as 48 kHz stereo s16 PCM, shape (frames, 2)."""
    pcm, rate, extra_bits = decode_native(path)
    return resample.resample(pcm, rate, RATE, extra_bits)


def locate(source_root: Path, rel: str) -> Path:
    """The album file `rel` under `source_root`.

    The recipe names files by the album's own folder layout.  If the user's
    copy is laid out differently (renamed folder, flattened), the file is
    found by its basename instead, as long as that name is unique.
    """
    direct = Path(source_root) / rel
    if direct.exists():
        return direct
    name = Path(rel).name
    hits = [p for p in Path(source_root).rglob(name) if p.is_file()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"{name} not found under {source_root}")
    raise FileExistsError(f"{name} appears {len(hits)} times under {source_root}")
