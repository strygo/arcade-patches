"""Shared helpers for the automatic builder modes (HSF2 / zero1 / Saturn):
ADX AFS-entry -> pack-track conversion, and post-build source cross-checks.
"""
from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

from . import adxcodec
from .adxcodec import AdxInfo
from .format import TrackMeta, CODEC_ADX, PackReader, VERB_PLAY, VERB_NONE

PKG_ROOT = Path(__file__).resolve().parent.parent   # the tree holding pack/
REPO_ROOT = PKG_ROOT.parent
MANIFESTS = PKG_ROOT / "manifests"
PACKS_DIR = PKG_ROOT / "work" / "packs"
INTERMEDIATE_DIR = PKG_ROOT / "work" / "intermediate" / "phase1"


def adx_entry_to_track(raw: bytes, *, name: str, source: str,
                       gain: int = 0x7f, force_loop: bool = False,
                       truncate_at_loop_end: bool = True
                       ) -> tuple[bytes, TrackMeta, AdxInfo]:
    """Convert a complete .adx blob into (frame stream, TrackMeta).

    Header is stripped; loop byte offsets are rebased to the stream.  Looped
    tracks are truncated at loop_end_byte (validated safe: playback never
    reaches past the loop end — internal research notes open-q 3).
    """
    info = adxcodec.parse_header(raw)
    coef1, coef2 = adxcodec.calc_coeffs(info.cutoff or adxcodec.DEFAULT_CUTOFF,
                                        info.sample_rate)
    fb = adxcodec.FRAME_BYTES * info.channels
    full_bytes = adxcodec.stream_bytes_for_samples(info.total_samples,
                                                   info.channels)
    meta = TrackMeta(sample_rate=info.sample_rate, channels=info.channels,
                     codec=CODEC_ADX, gain=gain, coef1=coef1, coef2=coef2,
                     name=name, source=source)
    if info.loop_flag:
        ls_rel = info.loop_start_byte - info.data_offset
        le_rel = info.loop_end_byte - info.data_offset
        if ls_rel < 0 or le_rel <= ls_rel or ls_rel % fb or le_rel % fb:
            raise ValueError(f"{name}: implausible loop bytes "
                             f"{info.loop_start_byte}/{info.loop_end_byte}")
        end = le_rel if truncate_at_loop_end else full_bytes
        stream = raw[info.data_offset:info.data_offset + end]
        meta.loop_start_sample = info.loop_start_sample
        meta.loop_start_byte = ls_rel
        meta.loop_end_sample = info.loop_end_sample
        meta.loop_end_byte = le_rel
    else:
        stream = raw[info.data_offset:info.data_offset + full_bytes]
        if force_loop:
            # table-forced whole-track loop (Anthology row bit [6]&0x80)
            n = len(stream) // fb * adxcodec.FRAME_SAMPLES
            meta.loop_start_sample = 0
            meta.loop_start_byte = 0
            meta.loop_end_sample = min(info.total_samples, n)
            meta.loop_end_byte = len(stream)
    if len(stream) % fb:
        raise ValueError(f"{name}: source truncated mid-frame")
    return stream, meta, info


def crosscheck_pack(pack_path: Path, afs, track_sources: dict[int, int],
                    decode_checks: int = 3) -> dict:
    """Byte-exactness acceptance (byte-gate acceptance).

    1. Every pack track's data must equal the corresponding byte range of
       its source AFS entry (header stripped, loop-end truncation applied) —
       re-read independently from the written pack and the source image.
    2. For `decode_checks` tracks: ffmpeg-decode the pack stream (synthetic
       v3 header) and the untouched source .adx; PCM must match sample-exactly
       over the pack track's coverage.

    `track_sources` maps pack track index -> AFS entry index.
    Returns a result dict; raises AssertionError on any mismatch.
    """
    rd = PackReader(pack_path)
    byte_ok = 0
    decode_ok = 0
    try:
        for ti, entry in track_sources.items():
            m = rd.tracks[ti]
            raw = afs.read(entry)
            info = adxcodec.parse_header(raw)
            src_slice = raw[info.data_offset:info.data_offset + m.data_length]
            pack_data = rd.read_track(ti)
            assert pack_data == src_slice, \
                f"track {ti} (entry {entry}): pack bytes != source bytes"
            byte_ok += 1
        for ti, entry in list(track_sources.items())[:decode_checks]:
            m = rd.tracks[ti]
            raw = afs.read(entry)
            pack_data = rd.read_track(ti)
            n_samples = m.data_length // (18 * m.channels) * 32
            pcm_pack = adxcodec.decode(pack_data, m.channels, m.sample_rate,
                                       total_samples=n_samples)
            pcm_src, _ = adxcodec.decode_file_bytes(raw)
            want = len(pcm_pack)
            assert pcm_src[:want] == pcm_pack, \
                f"track {ti} (entry {entry}): decode mismatch"
            decode_ok += 1
    finally:
        rd.close()
    return {"byte_exact_tracks": byte_ok, "decode_exact_tracks": decode_ok}


def iso_find_basename(iso, basename: str):
    """(path, lba, size) of the unique file named `basename` anywhere on the
    ISO.  The standalone HSF2 AE discs keep HSF2.AFS at the root; the US
    Anniversary Collection nests it under /HYPER/ -- disc-internal layout,
    located by basename, never by host-filesystem matching."""
    want = "/" + basename.upper()
    hits = [(path, lba, size) for path, lba, size, isdir in iso.entries()
            if not isdir and path.upper().endswith(want)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"{basename} not found anywhere on the disc")
    raise FileExistsError(
        f"{basename} appears {len(hits)} times on the disc: "
        + ", ".join(h[0] for h in hits))


def is_playstation_disc(iso) -> bool:
    """True when the image is a PlayStation disc (PS-X EXE boot).  Some
    library rips are misfiled -- e.g. 'SF Collection (USA) (Disc 2)' exists
    as BOTH a PlayStation and a Saturn rip -- and the PlayStation versions
    carry a different audio engine entirely, not the Saturn MUS masters."""
    roots = {p.rsplit("/", 1)[-1].upper()
             for p, _, _, isdir in iso.entries() if not isdir}
    if "SYSTEM.CNF" not in roots:
        return False
    return any(n.startswith(("SLUS", "SLPS", "SLES", "SCUS", "SCPS", "SCES"))
               for n in roots)


# ------------------------------------------------------------ trigger maps ---
# The tracked trigger maps are TSV: cmd, verb, suppress, track, cue.  Shared by
# every builder that reads one, which is why they live here rather than in any
# single game's module.
VERBS = {"play": VERB_PLAY, "none": VERB_NONE}


def _rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.reader(f, delimiter="\t")
                if r and not r[0].startswith("#")]


@dataclasses.dataclass
class MapRow:
    cmd: int
    verb: int
    suppress: int
    track: str | None       # None for verb=none (silence-only) rows
    cue: str
