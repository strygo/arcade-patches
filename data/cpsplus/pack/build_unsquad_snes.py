"""Build the UN Squadron / Area 88 SNES-soundtrack pack (unsquad_snes) from
the Capcom Music Generation album.

WHERE THE SOURCE LIVES.  roms/soundtracks/
capcom_music_generation_area_88_original_soundtrack (override with --ost):
one disc, 49 tracks -- tr01-26 the arcade score (a STUDIO RE-RECORDING, not
a board line-out), tr27 a standalone arrange (out of scope by decision),
tr28-49 the Super Famicom score.  THE JOIN IS BY DISC + TRACK NUMBER, NOT BY
NAME (build_ffight_ost doctrine); tags win, filename numbers are the
fallback, duplicate claims are an error.

THE MAP IS THE EAR-CLOSED MANIFEST.  Command -> role comes from
manifests/unsquad_snes_trigger_map.tsv: four review passes closed the
arcade side (every cue ear-confirmed against the album's arcade range),
then the full-album pairing pass set the SFC track per cue.  The SFC score
is largely ORIGINAL mission music -- only the shared material (UI, jingles,
bosses 1-2, R1, R3, endings) is a heard arrangement of the arcade tune, so
several assignments are the owner's curated role fit, and several SFC
tracks serve more than one cue (tr33 both ground bosses, tr39 both flight
rounds, tr37 Desert + Cave).  Tracks are DEDUPED: one copy in the pack,
many trigger rows.

ALIASES.  The driver mirrors its whole base bank at +0x42 (proven: renders
p50 0.99 identical) and cues 0x1a-0x1f are byte-identical clones of 0x18,
so every mapped row is also written at cmd+0x42, and the clones get the
Cave row.  The 0x80+ SFX bank stays unmapped by design.

FAIL-OPENS (final, by decision): 0x00 CREDIT (the SFC has no coin sound),
0x07 (the album's "Unused Tune" -- an unused slot the game never triggers),
0x19 EMERGENCY (no SFC counterpart), and the ear-retired garbage cues.

LOOPING -- STRUCTURE FIRST (the listening gate rejected fade-onset-only
seams).  Loop points come from manifests/unsquad_snes_loops.tsv, authored
by tools/find_repeat_loop.py: REPEAT rows wrap between the track's two
passes of the same performance (near-identical audio at the seam; the 1 s
crossfade erases the <=16-sample ADX-grid offset), SPLICE rows keep le at
the fade onset but start the loop at the content-matched splice point
instead of the track head.  Jingles are one-shot.  A track missing from
the manifest falls back to the ffight fade-onset construction.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pack import protocols                                            # noqa: E402
from pack.build_common import PACKS_DIR                               # noqa: E402
from pack.build_ffight_ost import (decode_flac, fade_onset,           # noqa: E402
                                   index_ost, XFADE_SECONDS, _encode)
from pack.format import (PackWriter, PackReader, TrackMeta, TriggerRow,  # noqa: E402
                         CODEC_ADX, VERB_PLAY)

ALIAS = 0x42                    # base-bank mirror offset (measured)
CAVE_CLONES = [0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f]   # byte-identical to 0x18

# arcade command -> (role, one_shot?, SFC album track).  Rows and their
# provenance live in manifests/unsquad_snes_trigger_map.tsv.
ROLES: list[tuple[int, str, bool, int]] = [
    (0x01, "PLAYER SELECT",       False, 30),
    (0x02, "SHOP",                False, 31),
    (0x03, "GAME OVER",           True,  49),
    (0x04, "R1 OIL FIELD",        False, 32),
    (0x05, "R2 THUNDER CLOUD",    False, 39),
    (0x06, "R3 FOREST STRONGHOLD", False, 35),
    (0x08, "R5 CANYON",           False, 43),
    (0x09, "R4 DESERT",           False, 37),
    (0x0a, "R7 ASCENDING",        False, 39),
    (0x0b, "R8 MARITIME",         False, 38),
    (0x0c, "R9 ARMORY",           False, 44),
    (0x0d, "R10 LAST BATTLE",     False, 45),
    (0x0e, "BOSS 2 DOGFIGHT",     False, 40),
    (0x0f, "BOSS 1 LAND WAR",     False, 33),
    (0x10, "BOSS 5 BATTLESHIP",   False, 42),
    (0x11, "BOSS 4 GROUND CARRIER", False, 33),
    (0x12, "BOSS 3 STRONGHOLD",   False, 36),
    (0x13, "ROUND CLEAR",         True,  34),
    # tr48 is the piece's NATURAL ENDING, not a fade (full level to 103.5 s,
    # decay to digital silence by 106.5; the SPC "The End" is a non-looping
    # ender, 105.6 s) -- so it plays ONCE, exactly as the SNES game behaves.
    (0x14, "LAST RANKING",        True,  48),
    (0x15, "LAST ROUND CLEAR",    True,  46),
    (0x16, "ENDING & STAFF ROLL", False, 47),
    (0x17, "SPECIAL ROUND",       False, 41),
    (0x18, "R6 CAVE",             False, 37),
]

TITLE = "UN Squadron (SNES Soundtrack)"
DISC = 1

# MEASURED trigger gain (EBU R128, loudness_match.py method, 2026-08-28):
# native cue probes (8 s past injection, attract-inject long renders) vs the
# mapped album tracks over nine pairs -- deltas +10.5..+14.6 LU, median
# +11.2 LU -> 0x7f * 10^(-11.2/20) = 0x23.  Baked here so a from-source
# rebuild stays byte-identical.
TRIG_GAIN = 0x23

LOOPS_TSV = Path(__file__).resolve().parent.parent / "manifests" / "unsquad_snes_loops.tsv"


def _read_loops() -> dict[int, tuple[int, int, str]]:
    out = {}
    for line in LOOPS_TSV.read_text().splitlines():
        if not line or line.startswith("#") or line.startswith("track"):
            continue
        f = line.split("\t")
        out[int(f[0])] = (int(f[2]), int(f[3]), f[5] if len(f) > 5 else "")
    return out


def _defade_tail(pcm, ls: int, le: int, n: int):
    """Restore the blend tail's sagging level (a loop_end near the master's
    fade) using the loop head's envelope as the reference.  Valid only when
    tail content ~= head content (the manifest's defade flag asserts it, on
    measured NCC); gain is per-100ms-block head/tail RMS, clamped [1, 2.5],
    linearly interpolated between block centres so the correction adds no
    steps of its own.  Deterministic; runs before ADX encode."""
    import numpy as np
    stereo = pcm.reshape(-1, 2).astype(np.float64)
    blk = 4410
    nb = n // blk
    gains = []
    for k in range(nb):
        h = stereo[ls + k*blk: ls + (k+1)*blk]
        a = stereo[le + k*blk: le + (k+1)*blk]
        rh = np.sqrt((h**2).mean()); ra = np.sqrt((a**2).mean())
        gains.append(min(max(rh / ra if ra > 0 else 1.0, 1.0), 2.5))
    centres = np.arange(nb) * blk + blk // 2
    curve = np.interp(np.arange(n), centres, gains)
    seg = stereo[le:le+n] * curve[:, None]
    out = pcm.copy()
    out[le*2:(le+n)*2] = np.clip(np.round(seg), -32768, 32767).astype(pcm.dtype).reshape(-1)
    return out


def build(out_dir: Path, ost_index: dict[tuple[int, int], Path]) -> dict:
    proto = protocols.PROTOCOLS["unsquad"]
    if proto.latch_page != 0x800180:
        raise ValueError("unsquad descriptor is not the CPS1 byte latch")
    xfade_samples = int(XFADE_SECONDS * 44100) // 32 * 32
    w = PackWriter(proto, title=TITLE, trigger_rows=protocols.SF2_TRIGGER_ROWS,
                   default_rate=44100, xfade_samples=xfade_samples)

    print(f"[unsq] === {TITLE} ===")
    loops = _read_loops()
    track_index: dict[int, int] = {}      # album track no -> pack track id
    rows = []
    for cmd, role, one_shot, trackno in ROLES:
        ti = track_index.get(trackno)
        if ti is None:
            src = ost_index.get((DISC, trackno))
            if src is None:
                raise FileNotFoundError(
                    f"{TITLE}: no file indexed for disc {DISC} track "
                    f"{trackno} ({role})")
            pcm, rate = decode_flac(src)
            frames = len(pcm) // 2
            if one_shot:
                keep, ls, le, xf = frames, 0, 0, 0
            elif trackno in loops:
                ls, le, flags = loops[trackno]
                if le + xfade_samples > frames:
                    raise ValueError(f"tr{trackno}: no blend tail past le")
                xf = 1
                keep = le + xfade_samples
                if "defade" in flags:
                    pcm = _defade_tail(pcm, ls, le, xfade_samples)
            else:
                onset = fade_onset(pcm, rate)
                le_raw = min(onset, frames)
                xf = 1 if frames - le_raw >= xfade_samples else 0
                if not xf:
                    le_raw = frames
                le_raw = (le_raw // 32) * 32
                ls, le = 0, le_raw
                keep = le + xfade_samples if xf else frames
            from pack import adxcodec
            data, coef1, coef2, _ = _encode(pcm[:keep*2], rate, 2, keep,
                                            src.stem, False)
            meta = TrackMeta(
                sample_rate=rate, channels=2, codec=CODEC_ADX, gain=0x7f,
                loop_start_sample=ls,
                loop_start_byte=adxcodec.samples_to_stream_byte(ls, 2),
                loop_end_sample=(le if not one_shot else 0),
                loop_end_byte=(adxcodec.samples_to_stream_byte(le, 2)
                               if not one_shot else 0),
                coef1=coef1, coef2=coef2, xfade_enable=xf,
                name=f"tr{trackno:02d} ({'one_shot' if one_shot else ('xfade loop' if xf else 'whole loop')})",
                source=f"CD {DISC}/{src.stem}.flac")
            ti = w.add_track(data, meta)
            track_index[trackno] = ti
            kind = "one-shot" if one_shot else ("xfade" if xf else "whole ")
            print(f"[unsq] tr{trackno:02d} {frames/rate:6.1f}s  loop_end "
                  f"{(le if not one_shot else 0)/rate:6.1f}s  {kind}  "
                  f"{len(data)/1e6:5.2f} MB")
        row = TriggerRow(verb=VERB_PLAY, track=ti, gain=TRIG_GAIN, suppress=1)
        w.set_trigger(cmd, row)
        w.set_trigger(cmd + ALIAS, row)
        rows.append((cmd, role))
        print(f"[unsq] 0x{cmd:02x}/0x{cmd+ALIAS:02x} {role:<22} -> tr{trackno:02d}")
    cave = w  # clones share 0x18's row
    ti_cave = track_index[37]
    for c in CAVE_CLONES:
        row = TriggerRow(verb=VERB_PLAY, track=ti_cave, gain=TRIG_GAIN, suppress=1)
        w.set_trigger(c, row)
        w.set_trigger(c + ALIAS, row)

    out_path = out_dir / "unsquad_snes.cpk"
    w.write(out_path)
    rd = PackReader(out_path)
    for cmd, role in rows:
        for c in (cmd, cmd + ALIAS):
            if rd.triggers[c].verb != VERB_PLAY:
                raise ValueError(f"readback: 0x{c:02x} ({role}) missing")
    for c in CAVE_CLONES:
        if rd.triggers[c].track != rd.triggers[0x18].track:
            raise ValueError(f"readback: clone 0x{c:02x} does not share 0x18's track")
    for c in (0x00, 0x07, 0x19, 0x20, 0x22, 0x3a, 0x3c):
        r = rd.triggers[c]
        if r.verb != 0 or r.suppress != 0:
            raise ValueError(f"readback: 0x{c:02x} must fail open")
    size = out_path.stat().st_size
    print(f"[unsq] {out_path.name}: {len(rows)} mapped cues (+{len(CAVE_CLONES)} "
          f"clones, +aliases), {len(track_index)} tracks, {size/1e6:.1f} MB\n")
    return {"path": str(out_path), "rows": len(rows), "size": size}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=PACKS_DIR)
    ap.add_argument("--ost", type=Path, required=True,
                    help="root of the CMG Area 88 rip (scanned for .flac)")
    a = ap.parse_args(argv)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    index = index_ost(a.ost, default_disc=DISC)   # one-disc album
    print(f"[unsq] indexed {len(index)} tracks from {a.ost}")
    build(a.out_dir, index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
