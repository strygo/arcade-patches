"""Build the three Final Fight OST edition packs from the Original Sound
Collection: X68000 FM, X68000 MIDI, and SNES.

WHERE THE SOURCES LIVE.  roms/soundtracks/final_fight_ost (override with
--ost).  The 5-CD collection interleaves platforms *within* discs:

    X68000 FM      disc 1 tracks 28-47   (20)
    X68000 MIDI    disc 1 tracks 48-67   (20)
    SNES           disc 2 tracks 17-37   (21)

THE JOIN IS BY DISC + TRACK NUMBER, NOT BY NAME.  Track titles vary with
whoever ripped the discs; the disc layout does not.  Every .flac under the
OST root holds the album as .flac or .wav, indexed by (disc, track)
read from its tags (ffprobe; any of
track/TRACKNUMBER + disc/DISCNUMBER, "3/67" forms accepted), falling back to
a leading number in the filename and a CD/Disc number in a parent folder
name.  Duplicate (disc, track) claims are an error, never a guess.  The
arcade command <-> role mapping still comes from the ear-confirmed
manifests/ffight_arrange_trigger_map.tsv.  Two role remaps are explicit
because the ports renumbered their stages:
  * SNES calls the Bay Area "ROUND4"; the arcade calls it Round 5.
  * SNES has no Industrial Area stage.  Its Industrial 2 survives only as
    "UNUSED TRACK(AC Ver ROUND4 INDUSTRIAL AREA2)" -- used here -- while
    Industrial 1 has no SNES recording at all, so arcade 0x48 is UNMAPPED in
    that edition and fails open to the arcade FM.

LOOPING -- why these need the crossfade when the CD packs did not.  Album rips
FADE OUT: measured 9-10 s decays on every stage theme (the PCE/Sega-CD packs
loop whole tracks because a CD drive repeats them, so they never fade).  Looping
a fading track would fade on every pass.  Instead loop_end is placed at the
FADE ONSET and xfade_enable is set, which is exactly what the format's
crossfade is for: the player blends the tail past loop_end (i.e. the fade
itself) against the loop head, so the fade becomes the blend material and the
seam is masked.  Jingles (round clear, game over, ...) stay one-shot.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pack import adxcodec, protocols                                  # noqa: E402
from pack.build_common import PACKS_DIR                               # noqa: E402
from pack.format import (PackWriter, PackReader, TrackMeta, TriggerRow,  # noqa: E402
                         CODEC_ADX, VERB_NONE, VERB_PLAY)


# arcade command -> (role key, one_shot?).  Commands and roles come straight
# from the ear-confirmed ffight map; 0x28/0x34 are SFX there and appear nowhere.
ROLES: list[tuple[int, str, bool]] = [
    # OPENING is ONE-SHOT, not a loop: the arcade's opening is a piece that
    # plays once and resolves as the title screen appears.  Measured on the
    # USA set (which this pack is authored against): the board's own music
    # runs 26.8 -> 84.0 s unbroken and the title comes up at 85.5 s, so it
    # finishes 1.5 s ahead of the title having never repeated.  Looping it
    # would talk straight over the title.  The album cuts are the right
    # length for that window played whole, fade and all -- X68000 MIDI 57.2 s
    # against the board's 57.2 s; FM 54.3 s and SNES 49.1 s fall 2.9 s and
    # 8.1 s short, which reads as an early finish rather than a wrong one.
    (0x35, "OPENING",            True),
    (0x55, "CHARACTER SELECT",   False),
    (0x50, "ROUND START",        True),
    (0x40, "R1 SLUM1",           False),
    (0x41, "R1 SLUM2",           False),
    (0x57, "ROUND CLEAR",        True),
    (0x42, "R2 SUBWAY1",         False),
    (0x43, "R2 SUBWAY2",         False),
    (0x4c, "BONUS STAGE",        False),
    (0x44, "R3 WEST SIDE1",      False),
    (0x48, "R4 INDUSTRIAL1",     False),
    (0x49, "R4 INDUSTRIAL2",     False),
    (0x45, "BAY AREA1",          False),
    (0x46, "BAY AREA2",          False),
    (0x47, "BAY AREA3",          False),
    (0x58, "ALL ROUND CLEAR",    True),
    (0x54, "ENDING",             False),
    (0x53, "CONTINUE",           False),
    (0x51, "GAME OVER",          True),
]

# Opening mid-sequence cues: silence the arcade WITHOUT issuing a play.
#
# The arcade splits its opening across several cues, but every edition's album
# carries the whole opening as ONE track (disc 1 tr29 / tr49 is CREDIT, not a
# second opening segment), so only the first cue can play it -- a second play
# verb restarts the track from zero.  VERB_NONE + suppress=1 gates the arcade
# FM for these cues while the album opening keeps running underneath, the same
# construction the Sega CD pack uses.
#
# Region note (latch traces, both measured): ffightu issues 0x35 at f1602 THEN
# 0x52 at f1970 and 0x36 at f2074; ffightj issues 0x52 FIRST at f758, then 0x35
# at f1266 and 0x36 at f1598.  With these rows absent the arcade original leaks
# back in mid-opening on USA and plays the whole head of the opening on Japan.
# Because Japan's first opening cue is 0x52 and it only suppresses, Japan's
# attract opens with ~8.5 s of silence before the album track starts at 0x35;
# fixing that too needs a play-if-idle verb the player does not have (its start
# pulse always restarts).
SUPPRESS_ONLY = [0x52, 0x36]

# role -> disc track number, per edition.  Explicit because the ports renumber
# stages; a fuzzy title match would silently pair the wrong round.  Numbers are
# the disc's own track numbers -- stable across rips regardless of file names.
X68K_FM = {
    "OPENING": 28, "CHARACTER SELECT": 30, "ROUND START": 31,
    "R1 SLUM1": 32, "R1 SLUM2": 33, "ROUND CLEAR": 34,
    "R2 SUBWAY1": 35, "R2 SUBWAY2": 36, "BONUS STAGE": 37,
    "R3 WEST SIDE1": 38, "R4 INDUSTRIAL1": 39, "R4 INDUSTRIAL2": 40,
    "BAY AREA1": 41, "BAY AREA2": 42, "BAY AREA3": 43,
    "ALL ROUND CLEAR": 44, "ENDING": 45, "CONTINUE": 46, "GAME OVER": 47,
}
X68K_MIDI = {k: v + 20 for k, v in X68K_FM.items()}   # MIDI set = FM tracks + 20
SNES = {
    "OPENING": 18, "CHARACTER SELECT": 19, "ROUND START": 20,
    "R1 SLUM1": 21, "R1 SLUM2": 22, "ROUND CLEAR": 23,
    "R2 SUBWAY1": 24, "R2 SUBWAY2": 25, "BONUS STAGE": 26,
    "R3 WEST SIDE1": 27,
    # no SNES Industrial Area 1 -- 0x48 is deliberately absent (fails open).
    # Industrial 2 survives only as CD2 track 37, "UNUSED TRACK(AC Ver ...)".
    "R4 INDUSTRIAL2": 37,
    "BAY AREA1": 28, "BAY AREA2": 29, "BAY AREA3": 30,
    "ALL ROUND CLEAR": 32, "ENDING": 33, "CONTINUE": 35, "GAME OVER": 36,
}

# The trigger gain is the MEASURED loudness match (EBU R128 vs the native
# ffight chip music, tools/loudness_match.py), baked in here rather than
# applied to the finished pack -- a post-build gain step is discarded by the
# next rebuild, and this keeps a from-source rebuild byte-identical.
EDITIONS = {
    "x68k_fm":   ("Final Fight (X68000 FM Soundtrack)",   1, X68K_FM,   0x3d),
    "x68k_midi": ("Final Fight (X68000 MIDI Soundtrack)", 1, X68K_MIDI, 0x4e),
    "snes":      ("Final Fight (SNES Soundtrack)",        2, SNES,      0x48),
}

XFADE_SECONDS = 1.0            # blend length; must fit inside the fade tail


def decode_flac(path: Path) -> tuple[np.ndarray, int]:
    """-> (int16 stereo interleaved, rate).  ffmpeg, matching adxcodec's own
    dependency rather than adding a decoder."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-acodec",
         "pcm_s16le", "-ac", "2", "-ar", "44100", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<i2").copy(), 44100


def _first_int(s: str | None) -> int | None:
    """Leading integer of a tag value ("3", "3/67", "03") or filename stem."""
    if not s:
        return None
    m = re.match(r"\s*(\d+)", s)
    return int(m.group(1)) if m else None


def _probe_tags(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags",
         "-of", "json", str(path)], capture_output=True, check=True).stdout
    tags = json.loads(out).get("format", {}).get("tags", {})
    return {k.lower(): v for k, v in tags.items()}


def _disc_from_parents(path: Path, root: Path) -> int | None:
    for parent in path.relative_to(root).parents:
        m = re.search(r"(?:cd|disc)[ ._-]*(\d+)", parent.name, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def index_ost(root: Path) -> dict[tuple[int, int], Path]:
    """(disc, track) -> flac path for every .flac under root.

    Tags win (track/tracknumber, disc/discnumber; "3/67" forms accepted);
    fallbacks are a leading number in the filename and a CD/Disc number in a
    parent folder name.  A file with no resolvable track number, or two files
    claiming the same (disc, track), is an ERROR: the join must never guess.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"OST root not found: {root} (use --ost)")
    index: dict[tuple[int, int], Path] = {}
    # Whatever the rip is stored as, ffmpeg decodes it to the same PCM, so a
    # WAV rip of the same discs produces the same pack and the same hash.
    tracks = sorted(f for f in root.rglob("*")
                    if f.is_file() and f.suffix.lower() in (".flac", ".wav"))
    if not tracks:
        raise FileNotFoundError(f"no .flac or .wav files under {root}")
    for f in tracks:
        tags = _probe_tags(f)
        track = _first_int(tags.get("track") or tags.get("tracknumber")) \
            or _first_int(f.stem)
        disc = _first_int(tags.get("disc") or tags.get("discnumber")) \
            or _disc_from_parents(f, root)
        if track is None:
            raise ValueError(f"cannot determine a track number for {f} "
                             "(no track tag, no leading number in the name)")
        if disc is None:
            raise ValueError(f"cannot determine a disc number for {f} "
                             "(no disc tag, no CD/Disc N in a parent folder)")
        key = (disc, track)
        if key in index:
            raise ValueError(f"disc {disc} track {track} claimed twice:\n"
                             f"  {index[key]}\n  {f}")
        index[key] = f
    return index


def fade_onset(pcm: np.ndarray, rate: int) -> int:
    """Frame index where the outro fade begins, else len (no fade).

    Album fades here run 9-10 s.  Walk back from the end while each 0.5 s block
    is quieter than the track's body median; the first block that is not is the
    last musical block, so the fade starts just after it."""
    mono = pcm.reshape(-1, 2).mean(axis=1)
    blk = rate // 2
    n = len(mono) // blk
    if n < 8:
        return len(mono)
    e = np.array([np.sqrt((mono[i*blk:(i+1)*blk]**2).mean()) for i in range(n)])
    body = np.median(e[n//4:n//2]) or 1.0
    i = n - 1
    while i > n//2 and e[i] < body * 0.55:
        i -= 1
    return min((i + 1) * blk, len(mono))


def build_edition(key: str, out_dir: Path, ost_index: dict[tuple[int, int], Path],
                  disc_label: str = "CD", measure_snr: bool = False) -> dict:
    title, disc, table, trig_gain = EDITIONS[key]
    proto = dataclasses.replace(protocols.PROTOCOLS["sf2"], game_id="ffight")
    if proto.latch_page != 0x800180:
        raise ValueError("sf2 descriptor is not the CPS1 byte latch")
    # The stored tail is a whole number of ADX frames (32 samples), and
    # add_track checks the byte length exactly -- so the blend length must
    # be frame-aligned or every track misses by the remainder.
    xfade_samples = int(XFADE_SECONDS * 44100) // 32 * 32
    w = PackWriter(proto, title=title, trigger_rows=protocols.SF2_TRIGGER_ROWS,
                   default_rate=44100, xfade_samples=xfade_samples)

    print(f"[ffost] === {title} ===")
    rows, skipped = [], []
    for cmd, role, one_shot in ROLES:
        trackno = table.get(role)
        if trackno is None:
            skipped.append((cmd, role))
            continue
        src = ost_index.get((disc, trackno))
        if src is None:
            raise FileNotFoundError(
                f"{title}: no file indexed for disc {disc} track {trackno} "
                f"({role}); files present for disc {disc}: "
                f"{sorted(tr for d, tr in ost_index if d == disc)}")
        pcm, rate = decode_flac(src)
        frames = len(pcm) // 2

        if one_shot:
            keep, ls, le, xf = frames, 0, 0, 0
        else:
            onset = fade_onset(pcm, rate)
            le_raw = min(onset, frames)
            xf = 1 if frames - le_raw >= xfade_samples else 0
            if not xf:                      # no usable tail: loop the whole thing
                le_raw = frames
            # ADX frames are 32 samples; loop points must be frame-aligned
            le_raw = (le_raw // 32) * 32
            ls, le = 0, le_raw
            # A crossfade track is stored as EXACTLY loop_end + one blend tail:
            # the player reads that tail once to blend it against the loop head,
            # so any fade beyond it would never be reached (format.add_track
            # enforces the length).  Keep the first blend-length of the fade and
            # discard the rest.
            keep = le + xfade_samples if xf else frames
        data, coef1, coef2, snr = _encode(pcm[:keep*2], rate, 2, keep,
                                          src.stem, measure_snr)
        meta = TrackMeta(
            sample_rate=rate, channels=2, codec=CODEC_ADX, gain=0x7f,
            loop_start_sample=ls, loop_start_byte=adxcodec.samples_to_stream_byte(ls, 2),
            loop_end_sample=(le if not one_shot else 0),
            loop_end_byte=(adxcodec.samples_to_stream_byte(le, 2) if not one_shot else 0),
            coef1=coef1, coef2=coef2, xfade_enable=xf,
            name=f"{role} ({'one_shot' if one_shot else ('xfade loop' if xf else 'whole loop')})",
            source=f"{disc_label} {disc}/{src.stem}.flac")
        ti = w.add_track(data, meta)
        w.set_trigger(cmd, TriggerRow(verb=VERB_PLAY, track=ti, gain=trig_gain,
                                      suppress=1))
        rows.append((cmd, role))
        kind = "one-shot" if one_shot else ("xfade" if xf else "whole ")
        print(f"[ffost] 0x{cmd:02x} {role:<18} {frames/rate:6.1f}s  loop_end "
              f"{le/rate:6.1f}s  {kind}  {len(data)/1e6:5.2f} MB")
    for cmd in SUPPRESS_ONLY:
        w.set_trigger(cmd, TriggerRow(verb=VERB_NONE, track=0, suppress=1))
        print(f"[ffost] 0x{cmd:02x} {'OPENING (mid-seq)':<18} suppress only, no play")
    for cmd, role in skipped:
        print(f"[ffost] 0x{cmd:02x} {role:<18} NO SOURCE IN THIS EDITION -> fails open")

    out_path = out_dir / f"ffight_{key}.cpk"
    w.write(out_path)
    rd = PackReader(out_path)
    for cmd, _ in rows:
        if rd.triggers[cmd].verb != VERB_PLAY:
            raise ValueError(f"readback: 0x{cmd:02x} missing")
    for cmd in SUPPRESS_ONLY:
        r = rd.triggers[cmd]
        if r.verb != VERB_NONE or r.suppress != 1:
            raise ValueError(f"readback: 0x{cmd:02x} must be suppress-only")
    for cmd, _ in skipped:
        if rd.triggers[cmd].verb != 0 or rd.triggers[cmd].suppress != 0:
            raise ValueError(f"readback: 0x{cmd:02x} should be unmapped")
    size = out_path.stat().st_size
    print(f"[ffost] {out_path.name}: {len(rows)} rows, {size/1e6:.1f} MB\n")
    return {"path": str(out_path), "rows": len(rows), "size": size,
            "skipped": [f"0x{c:02x}" for c, _ in skipped]}


def _encode(pcm: np.ndarray, rate: int, ch: int, frames: int,
            name: str, measure: bool):
    stream = adxcodec.encode(pcm.tobytes(), ch, rate)
    want = adxcodec.stream_bytes_for_samples(frames, ch)
    if len(stream) != want:
        raise ValueError(f"{name}: encoder gave {len(stream)} B, wanted {want}")
    c1, c2 = adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF, rate)
    return stream, c1, c2, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edition", choices=sorted(EDITIONS) + ["all"], default="all")
    ap.add_argument("--out-dir", type=Path, default=PACKS_DIR)
    ap.add_argument("--ost", type=Path, required=True,
                    help="root of the OST rip (scanned recursively for "
                         ".flac); see your wrapper script")
    a = ap.parse_args(argv)
    keys = sorted(EDITIONS) if a.edition == "all" else [a.edition]
    a.out_dir.mkdir(parents=True, exist_ok=True)
    index = index_ost(a.ost)
    discs = sorted({d for d, _ in index})
    print(f"[ffost] indexed {len(index)} tracks across discs {discs} from {a.ost}")
    for k in keys:
        build_edition(k, a.out_dir, index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
