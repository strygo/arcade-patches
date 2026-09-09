"""Build the three Final Fight OST edition packs from the Original Sound
Collection: X68000 FM, X68000 MIDI, and SNES.

WHERE THE SOURCES LIVE.  roms/soundtracks/final_fight_ost (override with
--ost).  The 5-CD collection interleaves platforms *within* discs:

    X68000 FM      disc 1 tracks 28-47   (20)
    X68000 MIDI    disc 1 tracks 48-67   (20)
    SNES           disc 2 tracks 17-37   (21)

THE JOIN IS BY DISC + TRACK NUMBER, NOT BY NAME.  Track titles vary with
whoever ripped the discs; the disc layout does not.  Every .flac or .wav
under the OST root is indexed by (disc, track) read from its tags (ffprobe; any of
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

from pack import adxcodec, protocols, xfade                           # noqa: E402
from pack.build_common import PACKS_DIR                               # noqa: E402
from pack.format import (PackWriter, PackReader, TrackMeta, TriggerRow,  # noqa: E402
                         CODEC_ADX, VERB_NONE, VERB_PLAY)


# arcade command -> (role key, one_shot?).  Commands and roles come straight
# from the ear-confirmed ffight map; 0x28/0x34 are SFX there and appear nowhere.
ROLES: list[tuple[int, str, bool]] = [
    # OPENING is ONE-SHOT, not a loop: the arcade's opening is a piece that
    # plays once and resolves as the title screen appears.  It enters on
    # 0x52, the SONG cue, in both regions: the board's own song runs from
    # that cue to the title (USA 33.1 -> 84.0 s, title 83.6 s; Japan
    # 12.7 -> 63.0 s, title 62.8 s), and the X68000 port plays the same
    # 51 s score over a 51 s story.  Every one of these album cuts is built
    # around that cue: the FM and MIDI transcriptions and the Double Impact
    # remix cross-correlate against the board's audio at a start of 32.7 to
    # 32.9 s in the USA attract, i.e. the song cue, never the ring (26.9 s).
    # Entering on the ring, as this builder once did, ran the whole piece
    # 6.2 s early and left 4 to 10 s of dead air before the title.
    # Audible lengths: FM 51.5 s, MIDI 52.3 s against the board's 50.9 s;
    # the SNES cut is 46.8 s and gets a baked one-wrap below (OPENING_WRAP).
    (0x52, "OPENING",            True),
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

# The three opening cues are three different sounds (see the arrange pack's
# map): 0x35 is the phone RINGING, 0x52 is the SONG, 0x36 is the phone being
# ANSWERED.  Only 0x52 is music, so it is the only cue that plays and gates;
# 0x35 and 0x36 are left unmapped in every edition and the board's own ring
# and click come through.  Cue order differs per region (latch traces):
# ffightu rings first (0x35 f1602, 0x52 f1970, 0x36 f2074); ffightj sings
# first (0x52 f758, 0x35 f1266, 0x36 f1598).  Playing on 0x52 therefore
# enters at 33.1 s in the USA and 12.7 s in Japan, exactly where the board's
# song enters, and the one-shot resolves on the title in both regions.
#
# The SNES cut is 3.8 s short of that window, so it gets ONE wrap baked into
# the audio (the FPGA player has no runtime crossfade for this), built the
# way the arrange pack wraps its opening: a phrase in the groove that the
# arrangement itself repeats, so the jump back is a repeat the ear already
# expects.  (loop_start, loop_end, blend) in samples at 44.1 kHz: 16.16 s ->
# 19.99 s is the first two bars of the phrase after the intro (8 beats at
# 125 bpm), loop_end nudged 3 ms so the waveform before the seam best matches
# the landing point; blend 7200 samples = 163 ms, the arrange pack's.  Chosen
# by ear from previews: the full four-bar wrap ran the music to the last
# second of the title screen, this one ends it as the title appears, which is
# what the board's own song does (USA 84.0 s, Japan 63.7 s after the cues).
OPENING_WRAP = {"snes": (712640, 881536, 7200)}

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


def index_ost(root: Path, default_disc: int | None = None
              ) -> dict[tuple[int, int], Path]:
    """(disc, track) -> audio path for every .flac/.wav under root.

    Tags win (track/tracknumber, disc/discnumber; "3/67" forms accepted);
    fallbacks are a leading number in the filename and a CD/Disc number in a
    parent folder name.  A file with no resolvable track number, or two files
    claiming the same (disc, track), is an ERROR: the join must never guess.
    A single-disc album may pass default_disc so a flat rip with no disc tag
    and no "Disc N" folder still indexes; multi-disc callers leave it None.
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
            or _disc_from_parents(f, root) or default_disc
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


def bake_wrap(pcm: np.ndarray, ls: int, le: int, n: int) -> np.ndarray:
    """One extra pass of [ls, le) baked into interleaved stereo PCM: play to
    le, blend the n samples past le against the n samples from ls, resume at
    ls+n.  Lengthens the track by le - ls; the outro is untouched."""
    st = pcm.reshape(-1, 2)
    if n % 32 or ls % 32 or le % 32 or le - ls <= n or len(st) < le + n:
        raise ValueError("wrap needs frame-aligned, nonoverlapping head and tail")
    lut = np.array(xfade.make_lut(n), dtype=np.int64)[:, None]
    blend = np.clip((st[le:le+n].astype(np.int64)*lut + st[ls:ls+n].astype(np.int64)*lut[::-1]
                     + 16384) >> 15, -32768, 32767).astype(st.dtype)
    return np.concatenate((st[:le], blend, st[ls+n:])).reshape(-1)


def configure_opening(w, key):
    """The opening plays and gates on the song cue only; the ring (0x35) and
    the answer click (0x36) stay unmapped so the board's own effects pass."""
    if w.triggers[0x52].verb != VERB_PLAY or w.triggers[0x52].suppress != 1:
        raise ValueError("opening must PLAY and gate on 0x52")
    for cmd in (0x35, 0x36):
        if w.triggers[cmd] != TriggerRow():
            raise ValueError(f"0x{cmd:02x} must stay unmapped (native phone effect)")
    print("[ffost] 0x52 OPENING plays on the song cue; 0x35 ring and 0x36 click pass through")
    return []


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
            wrap = OPENING_WRAP.get(key) if role == "OPENING" else None
            if wrap:
                pcm = bake_wrap(pcm, *wrap)
                frames = len(pcm) // 2
                print(f"[ffost] OPENING wrap baked: +{(wrap[1]-wrap[0])/rate:.2f}s "
                      f"-> {frames/rate:.2f}s")
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
    suppress_only = configure_opening(w, key)
    for cmd, role in skipped:
        print(f"[ffost] 0x{cmd:02x} {role:<18} NO SOURCE IN THIS EDITION -> fails open")

    out_path = out_dir / f"ffight_{key}.cpk"
    w.write(out_path)
    rd = PackReader(out_path)
    for cmd, _ in rows:
        if rd.triggers[cmd].verb != VERB_PLAY:
            raise ValueError(f"readback: 0x{cmd:02x} missing")
    for cmd in suppress_only:
        r = rd.triggers[cmd]
        if r.verb != VERB_NONE or r.suppress != 1:
            raise ValueError(f"readback: 0x{cmd:02x} must be suppress-only")
    assert rd.triggers[0x35] == TriggerRow()
    assert rd.triggers[0x36] == TriggerRow()
    assert rd.triggers[0x52].verb == VERB_PLAY and rd.triggers[0x52].suppress == 1
    for cmd, _ in skipped:
        if rd.triggers[cmd].verb != 0 or rd.triggers[cmd].suppress != 0:
            raise ValueError(f"readback: 0x{cmd:02x} should be unmapped")
    size = out_path.stat().st_size
    rd.close()
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


def self_test():
    for key in EDITIONS:
        w = PackWriter(protocols.PROTOCOLS["sf2"], trigger_rows=256)
        opening = TriggerRow(verb=VERB_PLAY, track=7, gain=80, suppress=1)
        w.set_trigger(0x52, opening)
        assert configure_opening(w, key) == []
        assert w.triggers[0x52] == opening
        assert w.triggers[0x35] == TriggerRow() and w.triggers[0x36] == TriggerRow()
    assert next(c for c, r, _ in ROLES if r == "OPENING") == 0x52
    # a wrap lengthens by exactly le - ls and leaves the head and outro intact
    pcm = np.arange(4000, dtype=np.int16).reshape(-1, 2).repeat(1, axis=1).reshape(-1)
    out = bake_wrap(pcm, 640, 1280, 64)
    assert len(out) == len(pcm) + 2*(1280-640)
    assert np.array_equal(out[:2*1280], pcm[:2*1280]) and np.array_equal(out[-100:], pcm[-100:])
    ls, le, n = OPENING_WRAP["snes"]
    assert ls % 32 == 0 and le % 32 == 0 and n % 32 == 0 and le - ls == 168896
    print("Final Fight OST opening routing self-test passed")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edition", choices=sorted(EDITIONS) + ["all"], default="all")
    ap.add_argument("--out-dir", type=Path, default=PACKS_DIR)
    ap.add_argument("--ost", type=Path,
                    help="root of the OST rip (scanned recursively for "
                         ".flac); see your wrapper script")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        self_test()
        return 0
    if a.ost is None:
        ap.error("--ost is required unless --self-test is used")
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
