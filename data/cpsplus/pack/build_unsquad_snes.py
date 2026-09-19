"""Build the UN Squadron / Area 88 SNES-soundtrack pack (unsquad_snes) from
the Capcom Music Generation album.

WHERE THE SOURCE LIVES.  roms/soundtracks/
capcom_music_generation_area_88_original_soundtrack (override with --ost):
Capcom Music Generation: Area 88 Original Soundtrack (Suleputer
CPCA-10161), one disc, 49 tracks -- tr01-26 the arcade score (a STUDIO
RE-RECORDING, not a board line-out), tr27 a standalone arrange (out of scope
by decision), tr28-49 the Super Famicom score, of which the pack uses
tr30-49 (tr28 opening and tr29 start jingle have no arcade cue).  THE JOIN
IS BY DISC + TRACK NUMBER, NOT BY NAME (build_ffight_ost doctrine); tags
win, filename numbers are the fallback, duplicate claims are an error.

THE INPUTS ARE PINNED (manifests/unsquad_snes_inputs.json, see
pack/inputpins.py and build_ffight_ost).  The verified rip is one
16-bit/44.1 kHz FLAC per track, every length a whole number of CD sectors;
FLAC, WAV or any lossless container decodes to the same PCM and matches,
lossy files never can.  Tracks are identified by their audio, not their
tags: a rip numbered differently, split differently, or with another read
offset is searched for each pinned recording and re-cut exactly, so the
loop points in manifests/unsquad_snes_loops.tsv -- absolute sample
positions in the pinned track -- stay valid without any shifting.  A track
that cannot be re-cut to its pinned PCM is reported, and the pack is not
built unless --allow-input-mismatch.

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

from pack import inputpins, protocols                                 # noqa: E402
from pack.build_common import MANIFESTS, PACKS_DIR                    # noqa: E402
from pack.build_ffight_ost import (decode_flac, fade_onset,           # noqa: E402
                                   index_ost, XFADE_SECONDS, _encode,
                                   check_album, _probe_tags)
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
INPUT_PINS = MANIFESTS / "unsquad_snes_inputs.json"
ALBUM = ("Capcom Music Generation: Area 88 Original Soundtrack (Suleputer "
         "CPCA-10161)")
PINS_SOURCE = {
    "release": ALBUM + ": one disc, 49 tracks; tracks 28-49 are the Super "
               "Famicom score and the pack uses tracks 30-49",
    "rip": "one 16-bit/44.1 kHz FLAC per track, every length a whole number "
           "of CD sectors; crc32 is the CRC32 of the track's PCM (EAC's Copy "
           "CRC for an EAC rip with the same offset and gap handling)",
}


def pin_key(trackno: int) -> str:
    return f"tr{trackno:02d}"


def wrong_album_message(root: Path, n_files: int, sample: Path | None) -> str:
    """Why nothing matched, when none of the pinned recordings is there."""
    tags = _probe_tags(sample) if sample else {}
    seen = f"{n_files} audio file(s)"
    if tags.get("album"):
        seen += f', album tag "{tags["album"]}"'
    if tags.get("tracktotal") or tags.get("totaltracks"):
        seen += f", {tags.get('tracktotal') or tags.get('totaltracks')} tracks per its tags"
    return (f"none of the Super Famicom score recordings this pack needs was "
            f"found in {root} ({seen}).\n"
            f"The pack is built from {ALBUM}: one disc of 49 tracks, the "
            f"arcade score (tracks 1-27) followed by the Super Famicom score "
            f"(tracks 28-49; the pack uses 30-49).\n"
            f"An Area 88 album with a different track count -- a 21-track "
            f"disc, for instance -- is a different release that does not "
            f"carry the Super Famicom score, and cannot build this pack.")


def _read_loops() -> dict[int, tuple[int, int, str]]:
    out = {}
    for line in LOOPS_TSV.read_text(encoding="utf-8").splitlines():
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


def build(out_dir: Path, ost_index: dict[tuple[int, int], Path],
          checks: dict | None = None, pins: dict | None = None,
          pin_tracks: dict | None = None) -> dict:
    """checks: the input table (pin key -> TrackCheck) the audio comes from;
    None reads the indexed files.  pin_tracks collects input fingerprints
    for the maintainer --write-pins step."""
    proto = protocols.PROTOCOLS["unsquad"]
    if proto.latch_page != 0x800180:
        raise ValueError("unsquad descriptor is not the CPS1 byte latch")
    xfade_samples = int(XFADE_SECONDS * 44100) // 32 * 32
    w = PackWriter(proto, title=TITLE, trigger_rows=protocols.SF2_TRIGGER_ROWS,
                   default_rate=44100, xfade_samples=xfade_samples)

    print(f"[unsq] === {TITLE} ===")
    audit = inputpins.Audit("unsquad_snes", pins, checks or {})
    loops = _read_loops()
    track_index: dict[int, int] = {}      # album track no -> pack track id
    rows = []
    for cmd, role, one_shot, trackno in ROLES:
        ti = track_index.get(trackno)
        if ti is None:
            pkey = pin_key(trackno)
            if checks is not None:
                chk = checks[pkey]
                src = chk.path or next(p for p, _, _ in chk.segments if p)
                pcm, rate = inputpins.load_checked(chk, "ffmpeg"), 44100
            else:
                src = ost_index.get((DISC, trackno))
                if src is None:
                    raise FileNotFoundError(
                        f"{TITLE}: no file indexed for disc {DISC} track "
                        f"{trackno} ({role}). This pack needs {ALBUM}, 49 "
                        f"tracks, whose tracks 30-49 are the Super Famicom "
                        f"score; an Area 88 album with fewer tracks is a "
                        f"different release without it")
                pcm, rate = decode_flac(src)
            if pin_tracks is not None:
                pin_tracks[pkey] = {"role": role, "disc": DISC, "track": trackno,
                                    **inputpins.fingerprint(pcm)}
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
            audit.track(f"tr{trackno:02d}", [pkey], pcm[:keep*2], data,
                        (ls, le if not one_shot else 0, xf))
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
    rec = audit.write(out_dir / "unsquad_snes.audit.json", out_path)
    print(f"[unsq] {out_path.name}: {len(rows)} mapped cues (+{len(CAVE_CLONES)} "
          f"clones, +aliases), {len(track_index)} tracks, {size/1e6:.1f} MB")
    if pins:
        print(f"[unsq] {out_path.name}: {rec['diagnosis']}")
    print()
    return {"path": str(out_path), "rows": len(rows), "size": size,
            "audit": audit}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=PACKS_DIR)
    ap.add_argument("--ost", type=Path, required=True,
                    help="folder holding your rip of " + ALBUM + " (49 "
                         "tracks, lossless: .flac or .wav)")
    ap.add_argument("--check-inputs", action="store_true",
                    help="only check the rip against the pinned input tracks "
                         "(one row per track) and exit: 0 all match, 3 not")
    ap.add_argument("--allow-input-mismatch", action="store_true",
                    help="build even when input tracks differ from the "
                         "verified rip (it will not match the published pack)")
    ap.add_argument("--write-pins", action="store_true",
                    help="maintainer: build from the verified rip and write "
                         + INPUT_PINS.name)
    a = ap.parse_args(argv)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    if a.write_pins:
        index = index_ost(a.ost, default_disc=DISC)
        tracks: dict = {}
        res = build(a.out_dir, index, pin_tracks=tracks)
        inputpins.write_pins(INPUT_PINS, PINS_SOURCE, dict(sorted(tracks.items())),
                             {"unsquad_snes": res["audit"].pins_entry(Path(res["path"]))})
        return 0
    pins = inputpins.load_pins(INPUT_PINS)
    if pins is None:
        if a.check_inputs:
            print(f"[unsq] {INPUT_PINS.name} not found: nothing to check against")
            return 0
        index = index_ost(a.ost, default_disc=DISC)   # one-disc album
        print(f"[unsq] indexed {len(index)} tracks from {a.ost}")
        build(a.out_dir, index)
        return 0

    wanted = {pin_key(t): (DISC, t) for t in sorted({t for *_, t in ROLES})}
    checks, index, unplaced, _ = check_album(
        pins, a.ost, wanted, "Area 88 Original Soundtrack (CPCA-10161)",
        INPUT_PINS.name, default_disc=DISC, tag="[unsq]")
    ok = [c for c in checks.values() if c.ok]
    if not ok:
        files = sorted(index.values()) + sorted(unplaced)
        print("[unsq] " + wrong_album_message(a.ost, len(files),
                                              files[0] if files else None))
    inputpins.gate("unsquad_snes", checks, pins, a.out_dir / "unsquad_snes.cpk",
                   a.allow_input_mismatch, a.check_inputs, "[unsq]")
    build(a.out_dir, index, checks=checks, pins=pins)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
