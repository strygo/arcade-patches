"""`build_ffight_arrange.py` — Final Fight (CPS1 `ffight`) ARRANGE pack, per region.

Builds the Final Fight CD (Sega CD, 1993) CD-DA arrangement re-keyed onto the
CPS1 `ffight` Z80 byte-latch stage commands, for the jtcps1_cpsplus core.

ONE PACK PER REGION (--region us|jp, see REGIONS): ffight_arrange from the US
disc and ffight_arrange_jp from the JP disc, each carrying only its own voiced
cutscene pair and playing the opening on the cue its own ROM issues first.

Each region can also be built in two codec variants from the SAME trigger map
and the SAME 19 CD tracks, to A/B on hardware:

  ffight_arrange_pcm.cpk   CODEC_PCM, byte-exact s16le  (~315 MB — OVER the
                           256 MiB DDR cap; built deliberately, see below)
  ffight_arrange.cpk       CODEC_ADX, ffmpeg adpcm_adx  (~1/4 the size)

The PCM variant EXCEEDS the DDR budget documented in
internal research notes (268,435,456 B at 0x30000000).  That cap is
derived from source and has never been observed failing, so the user asked for
the over-budget pack to be built and tested rather than assumed impossible.
The builder prints the overage; it does not refuse to write it.

TRIGGER MAP.  manifests/ffight_arrange_trigger_map.tsv — 21 rows, every one
snapshot- or trace-anchored (see that file's header for the evidence per row).
Columns: cmd, verb, suppress, cd_track, cue, evidence.  Two row shapes:

  verb=play, suppress=1   substitute: the arranged track plays and the arcade
                          YM2151/OKI cue is suppressed at the Z80 latch.
  verb=none, suppress=1   silence-only: the arcade cue is suppressed but NO
                          arranged track is started, so the track already
                          playing keeps running.  This is what stitches the
                          arcade's four-cue opening onto the CD's single
                          continuous opening track (tr25) — 0x52 and 0x36 are
                          mid-sequence cuts that must not restart it.

PROTOCOL.  protocols.py has NO `ffight` descriptor, and `get_protocol()` would
silently fall back to `generic_protocol()` — the CPS2/Anthology QSound family
(latch page 0x618000, shared-RAM record, Anthology control verbs), which is
WRONG for a CPS1 game.  So this builder takes the tracked CPS1 byte-latch
descriptor (`protocols.PROTOCOLS["sf2"]`, ground truth manifests/protocol/
sf2.json + internal research notes) and re-labels its game_id.  Nothing
about the dialect is invented here: same latch page 0x800180, same single
command byte at +0x01, no handshake, no fade law, empty control-verb map, and
the same 256-row (8-bit-keyed) trigger table.  If a real `ffight` descriptor is
ever added to protocols.py this builder picks it up automatically.

LOOP TREATMENT.  Default: authentic whole-track repeat (the Sega CD drives
its CD-DA with MSCPLAYR hardware whole-track repeat, verified on the command
bus; masters keep their fades and ~2 s trailing silence).  That gap-then-
restart IS what a Sega CD does mid-game, so stage themes keep it.

Two cues play against FIXED-LENGTH arcade sequences, where a whole-track
restart dies early and an infinite inner loop gets chopped mid-phrase by the
next command.  Those use AUTHORED_LOOPS: verbatim whole-stream storage with a
finite loop_count — the player wraps N times, then plays THROUGH loop_end
into the source's own outro/fade (format codec-byte bits 6-7; the blend
material past loop_end is the stream's natural continuation, so nothing is
re-authored):
  tr25 OPENING  le=1183904 xfade count=1, loop_start PER REGION (REGIONS
       ["<r>"]["opening_loop"], not AUTHORED_LOOPS) — head+groove, one
       crossfaded groove repeat, then the master's own outro.  The wrap is
       sized so that outro lands on that ROM's title screen, and the two
       regions need very different lengths because Japan enters 20.4 s earlier
       relative to its title:
         US  ls=732352  → 10.24 s wrap → music 26.9-84.7 s, title 83.6-88.8 s
         JP  ls=1056928 →  2.88 s wrap → music 12.7-63.1 s, title 62.8-68.1 s
       (the US wrap is not a whole number of bars: the authored body is 18
       beats, and one bar was taken off it.  Japan's is exactly one bar.)
       One bar = 126976 samples = 4 beats at the measured 83.4 BPM, 32-sample
       aligned as ADX requires.  Shortening the wrap edits nothing: the intro
       and the outro are untouched, the groove just repeats less, so everything
       after it arrives earlier.  Seam EAR-CONFIRMED (candB - 1
       beat) at the original ls=605376, and again by ear per region.
  tr26 ENDING   ls=59488 le=1164864 xfade count=3 — measured 25.07 s
       musical period (the voiced tr24 repeats at it, r=0.99; the old TSV
       29.75 s loop was off by 4.7 s), three passes, then the CD master's own
       musical fade, completing 4.7 s before the game's 0xf7 stop.

  14 looping tracks: tr02-tr12, tr17, tr25, tr26
   5 one-shot tracks: tr13, tr14, tr15, tr16, tr18

Run:
  python3 -m cpsplus.pack.build_ffight_arrange --region us --variant adx
  python3 -m cpsplus.pack.build_ffight_arrange --region both --variant adx
The ADX SNR measurement needs numpy (repo venv: .venv-tools/bin/python).

INPUTS: ONE Final Fight CD rip per region pack, passed EXPLICITLY as
  --region us --us-disc <rip>   or   --region jp --jp-disc <rip>
  (a .cue, or a .zip/.7z that contains one).  See REGIONS below for why the
  pack is split and what differs between the two.
The builder never searches for or name-matches disc files.  cd_full /
cd_jp_full are a deterministic extraction cache; once populated, the disc
arguments are no longer needed.  Audio is raw CD-DA cut at INDEX 01 and is
shipped unmodified, with one deliberate exception CLASS: both voiced
ENDINGS (0x71 US, 0x73 JP) are lead-in-padded and then spliced to the arcade
ending's length -- see ARCADE_CUT for why
that has to happen here. E2E-verified: a build into an empty
cache reproduces the shipped pack byte-for-byte.  Unpacking a .7z needs 7zz on PATH.
Keep your local disc paths in a small wrapper script.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import math
import wave
from pathlib import Path

from . import adxcodec, protocols
from .protocols import cps1_protocol
from .adxencode import encode_adx_track, read_wav, snr_db
from .audition import verify as audition_verify
from .build_common import (MANIFESTS, PACKS_DIR, PKG_ROOT,
                           MapRow, VERBS, _rows)
from .format import (PackWriter, PackReader, TrackMeta, TriggerRow, CODEC_ADX,
                     CODEC_PCM, VERB_NONE, VERB_PLAY, VERB_NAMES,
                     DDR_BUDGET_BYTES)

CD_DIR = PKG_ROOT / "work" / "intermediate" / "ffightcd" / "cd_full"
TRIGGER_TSV = MANIFESTS / "ffight_arrange_trigger_map.tsv"

# The disc rips are the TRUE inputs; the cd_full / cd_jp_full WAV dirs are a
# deterministic extraction cache.  The builder never guesses where discs live:
# the user passes --us-disc / --jp-disc (a .cue, or a .zip/.7z containing one)
# and ensure_disc_audio() fills an incomplete cache from that rip.  A complete
# cache builds without the disc arguments. VERIFIED: the scripted
# extraction is byte-identical to the pre-existing manually ripped cd_full for
# all 25 US audio tracks (and to cd_jp_full for the JP tracks) -- raw CD-DA
# cut at INDEX 01 either way, so authored loop offsets are stable.

# internal research notes: the image base is fixed at 0x30000000 and
# physical DDR3 ends at 0x40000000, so 256 MiB is an architectural bound.
# format.DDR_BUDGET_BYTES is the older, more conservative ~240 MB figure; both
# are reported.
DDR_HARD_CAP_BYTES = 256 * 1024 * 1024        # 268,435,456
FFIGHT_ROM_BYTES = 0x350000                   # "Total 0x350000" in base/ffight.mra

# Fixed-window cues: (loop_start_sample, loop_end_sample, xfade, loop_count).
# See the LOOP TREATMENT docstring section for the evidence behind each.
AUTHORED_LOOPS = {
    # tr25 (OPENING) is NOT here: its wrap is sized per region so the outro
    # lands on that ROM's title screen, so REGIONS["<r>"]["opening_loop"] owns
    # it.  Keeping a copy here as well would just drift out of sync.
    "tr26": (59488, 1164864, 1, 3),    # ENDING: three BLENDED passes, natural
                                       # fade (raw hard cut measured a 10605
                                       # instantaneous step at the wrap)
    # Stage themes: the FINAL ear-adjudicated set from the 8-round seam
    # campaign (the internal listening-campaign log holds every round,
    # verdict and metric).  All crossfade, infinite inner loops.
    #
    # 2026-08-22 REVISION (tr02, tr11) -- the seam campaign only ever
    # auditioned the JOIN, in clips a few seconds either side of the wrap.
    # That cannot show WHERE the loop lands, and two tracks failed on
    # exactly that:
    #   tr02 looped to 4.30 s, so a wrap replayed the whole statement and
    #     read as the song restarting mid-fight (R3 holds it >117 s).
    #   tr11 had no inner loop at all, so loop_end sat at the end of the
    #     stream -- PAST the master's fade.  It faded to silence and snapped
    #     back to full level.  R4 holds it 356 s, so it did this twice.
    # Both replaced with periods derived from STRUCTURE, not from a BPM
    # estimate: tr02 = 83.425 s (onset+spectral match r=0.771, 32 bars @
    # 92.0 BPM), tr11 = 40.808 s (onset-envelope autocorrelation, confirmed
    # against the user's own landmark -- a figure recurring at 54.56 s and
    # 95.37 s).  A bar grid from beat detection put tr11 0.96 s out and was
    # audible; see the loops manifest header.  Both ear-approved.
    # tr12/tr17 remain whole-track: their loop_end is likewise past the fade,
    # but neither can reach it (bonus stage 34 s vs a 170 s track; select
    # times out at 10.6 s vs 120 s), so the wrap is unreachable in play.
    "tr02": (1524736, 5203808, 1, 0),  #  34.57..118.00s  R1 Slum (+R3/R6)
    "tr03": (4268864, 5591424, 1, 0),  #  96.80..126.79s  Slum 2
    "tr04": (2782688, 5292416, 1, 0),  #  63.10..120.01s  Subway
    "tr05": (2187360, 5078528, 1, 0),  #  49.60..115.16s  Sodom
    "tr06": (3968992, 7144192, 1, 0),  #  90.00..162.00s  West Side (user region)
    "tr07": (1750752, 5030016, 1, 0),  #  39.70..114.06s  Bay 1
    "tr08": (1252416, 4736768, 1, 0),  #  28.40..107.41s  Bay 2
    "tr09": (2429888, 5306976, 1, 0),  #  55.10..120.34s  Bay 3
    "tr10": (1653728, 4968736, 1, 0),  #  37.50..112.67s  Industrial 1
    "tr11": (2406080, 4205696, 1, 0),  #  54.56.. 95.37s  Industrial 2 (+R6)
}
XFADE_SAMPLES = 7200                   # 163 ms equal-power blend (pack header)

# Voiced-cutscene extension cues (contract: manifests/ffight_voiced_cues.tsv
# + the Final Fight CD backport project's audio contract).  The stock ROM never emits these
# IDs (verified: full phase-0 census tops out at 0x58 before the 0xf0 control
# family); the ffightcd backport ROM emits them from its cutscene code, so ONE
# pack powers both ROM families -- stock = no-voice loops on 0x35/0x54,
# backport = voiced one-shots on 0x70-0x73.  One-shot: the backport cutscenes
# span the audio, so it plays through and ends itself.
# (cmd, source_dir_kind, track, label)
VOICED_CUES = [
    (0x70, "us", "tr23", "Opening voiced US (backport cutscene)"),
    (0x71, "us", "tr24", "Ending voiced US (backport cutscene)"),
    (0x72, "jp", "tr23", "Opening voiced JP (backport cutscene)"),
    (0x73, "jp", "tr24", "Ending voiced JP (backport cutscene)"),
]

OPENING_TRACK = "tr25"

# Voiced ENDINGS (0x71 US, 0x73 JP): the two cues whose audio is processed.
#
# The CD and the arcade play the same dialogues, but the CD spends far longer
# on the credits-and-spar block (US: 64 s where the arcade spends 46.5 s), so
# played raw the farewell dialogue arrives many seconds after the arcade has
# finished the scene.  The cutscene backport does not retime the ROM -- it
# shortens the MUSIC: whole bars lifted out of the steady 82.19 BPM credits
# loop, joined with a 0.12 s equal-power crossfade.  A whole number of bars
# out of a steady loop is inaudible, the reunion sits before the splice and
# is untouched, and everything after arrives earlier by exactly the lifted
# amount (US 20.5604 s; JP 23.361 s).
#
# This has to happen HERE rather than on the ROM side, because the backport's
# caption frames and lip anchors are derived from the processed track's own
# clock.  Shipping raw audio puts every anchored line seconds off -- the
# exact failure the processing exists to prevent.  Parameters are fixed, so
# the transform is deterministic and the pack stays byte-reproducible from
# the disc.
#
# BOTH endings are processed, and both start with a LEAD-IN -- silence for the gap between
# the ending cue and the CD spinning up, a whole number of CD sectors, and
# the regions differ (150 sectors for US, 149 for JP).  The first shipped
# form of this table cut 0x71 from the raw rip (no lead-in, 2.00 s early --
# "the lips are badly out of sync in both scenes") and shipped 0x73
# verbatim on a "JP is fitted on the ROM side" note that predated the JP
# ending build (~23.4 s late from 56 s on).  The JP cut point is 56.0 s
# because 56.0-79.5 s is entirely instrumental -- every vocal survives; a
# 10-bar cut ran into the 85 s vocal.
#
# cue -> (lead_samples, cut_at_s, bars, bpm, xfade_s)
ARCADE_CUT = {
    0x71: (88200, 39.225, 7, 82.19, 0.12),   # 115.00 -> 117.00 -> 96.44 s
    0x73: (87612, 56.0, 8, 82.19, 0.12),     # 138.57 -> 140.56 -> 117.08 s
}

# OPENINGS (0x70 US, 0x72 JP) need the same lead the endings get, for the
# same reason and from the same source -- the disc's own pregap.
#
# tools/extract_cdda cuts at INDEX 01 (--pregap trim), which discards the
# standard 2-second pregap every track on these discs carries: the USA cue
# sheet puts INDEX 01 at 00:02:00 (150 sectors) and the Japanese one at
# 00:01:74 (149).  The backport ROM's video lead was authored against the
# UNTRIMMED track -- rom.py: "175-frame black lead-in ... (128f pregap + 47f
# musical lead; untrimmed track file, sample-aligned visuals)" -- so a
# trimmed track starts the voice ~2 s before the ROM expects it and it stays
# that far ahead for the whole opening.
#
# The endings already carry exactly these values in ARCADE_CUT (0x71 = 88200
# = 150 sectors, 0x73 = 87612 = 149); the openings were simply never given
# them.  Same defect, same fix, no ROM change: restore the pregap.
PREGAP_LEAD = {
    0x70: 150 * 588,        # 88200 = 2.000 s, USA  INDEX 01 00:02:00
    0x72: 149 * 588,        # 87612 = 1.987 s, JP   INDEX 01 00:01:74
}

# md5 of arcade_cut()'s PCM output per cue, pinned to the byte-exact files
# the shipped backport ROMs were ear-tuned against.  The builder computes this
# hash on every build and refuses to pack a drifted transform -- without that
# gate nothing compares the two implementations, which is how a missing
# lead-in reaches a pack.
# cue -> (md5_of_pcm, frames)
ARCADE_CUT_PIN = {
    0x71: ("1f4e57194345feb8ee7a80eb006e276b", 4252985),   # 96.44 s
    0x73: ("fff5689dbd124b0df49d1d6786af9a84", 5163206),   # 117.08 s
}


def arcade_cut(pcm: bytes, ch: int, rate: int, lead_samples: int,
               cut_at: float, bars: int, bpm: float,
               xfade: float) -> tuple[bytes, int]:
    """Pad with `lead_samples` of silence, then lift `bars` bars -> (pcm, frames).

    The pad models the gap between the ending cue and the CD spinning up: a
    whole number of CD sectors (588 samples each), prepended BEFORE the cut
    because every caption frame and lip anchor in the backport ROMs is
    measured against the PADDED clock.  Then an equal-power join across
    `xfade` seconds so the splice has no level dip.  Float64 throughout,
    clip+truncate to int16 at the end, matching the reference implementation
    the caption timings were measured against -- ARCADE_CUT_PIN holds the
    reference output's md5 and the build fails loudly if this ever drifts.
    """
    import numpy as np                      # lazy, as snr_db does
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64).reshape(-1, ch)
    if lead_samples:
        a = np.concatenate([np.zeros((lead_samples, ch), np.float64), a])
    drop = (60.0 / bpm) * 4 * bars
    t0 = int(cut_at * rate)
    t1 = t0 + int(drop * rate)
    xf = int(xfade * rate)
    if t1 + xf > len(a):
        raise ValueError("arcade cut runs past the end of the track")
    head, tail = a[:t0], a[t1:]
    n = min(xf, len(head), len(tail))
    if n:
        f = np.linspace(0, np.pi / 2, n)[:, None]
        joined = head[-n:] * np.cos(f) ** 2 + tail[:n] * np.sin(f) ** 2
        out = np.concatenate([head[:-n], joined, tail[n:]])
    else:
        out = np.concatenate([head, tail])
    return np.clip(out, -32768, 32767).astype("<i2").tobytes(), len(out)

# --------------------------------------------------------------- regions ---
# ONE PACK PER REGION.  A shared pack had to carry BOTH languages of voiced
# cutscene audio (25.8 MB of the 108 MB, half of it dead weight for any given
# ROM) and had to be built from BOTH discs.  Per region each pack ships only
# the voiced pair its own backport ROM emits -- the audio contract assigns
# 0x70/0x71 to the US build and 0x72/0x73 to the JP build -- and builds from
# ONE disc.  A ROM that emitted the other region's pair finds no row and fails
# open to arcade audio, which is the safe outcome.
#
# The JP disc carries the same masters: measured track by track, every music
# cue correlates r=1.0000 against its US counterpart at a constant -120 sample
# (2.7 ms) offset with no drift, and every JP track is exactly 87612 frames
# (1.99 s = 149 sectors) shorter because that rip trims trailing DIGITAL
# SILENCE -- the US tail measures RMS 0.0 across it, so no music is lost.  A
# 2.7 ms shift sits inside the 163 ms crossfade, but it is measured, so it is
# applied rather than ignored.  The cutscene family is renumbered on the JP
# disc: tr25 -> tr28, tr26 -> tr29.
#
# OPENING ENTRY.
#
# The three opening cues are three different sounds, not one sequence: 0x35 is
# the phone ringing, 0x52 is the song, 0x36 is the phone being answered (which
# also stops the ring).  Both ROMs agree on those roles and differ only in
# ORDER (latch traces, cold boot; title screens snapshot-measured):
#     ffightu   ring 26.9 s   song 33.1 s   answer 34.8 s   title 83.6 s
#     ffightj   song 12.7 s   ring 21.2 s   answer 26.8 s   title 62.8 s
#
# Gate ONLY 0x52, the song.  The ring and the answer are the board's own sounds
# and pass through, so the phone rings and is answered exactly as on the board.
# Each pack ENTERS on the first cue its own ROM issues -- 0x35 for the US,
# 0x52 for Japan (apply_region) -- because that is the earliest the music can
# start, and the attract is long.
#
# MEASURE THE MUSIC, NOT THE FILE.  Both discs carry the same 47.5 s master;
# only the trailing digital silence differs (US 2.9 s, JP 0.9 s -- the 1.98 s
# the JP rip trims).  Taking file length for music length puts the entry 6.2 s
# late and leaves dead air before the title.  Coverage of the two options, in
# AUDIBLE music:
#     0x35 + authored loop   26.9 -> 87.5 s   no silence either end   <- ships
#     0x52 + one-shot        33.1 -> 80.6 s   6.2 s late, 3.0 s short
# The looped US opening runs through the title appearance (83.6 s) and resolves
# 1.3 s before the title screen gives way (88.8 s).  That is deliberate: the
# alternative leaves audible dead air, and the user prefers continuous music.
#
# Japan stays ONE-SHOT and cannot do better.  Its window is 12.7 -> 62.8 s =
# 50.1 s against the same 47.5 s master, so one-shot fits with 2.6 s to spare;
# the authored one-wrap loop would run to 73.3 s, past the title AND past the
# title screen's own end (68.1 s) into the demo.  Japan also cannot start
# earlier -- 0x52 at 12.7 s is already its first cue.
REGIONS = {
    "us": {
        "cd_subdir": "cd_full", "disc_flag": "--us-disc",
        "voiced_kind": "us", "track_alias": {}, "loop_shift": 0,
        "opening_cmd": 0x35,
        # one wrap of the groove, then the master's own outro -- see OPENING
        # ENTRY for why the wrap is this long in each region
        "opening_loop": (732352, 1183904, 1, 1),
        "pack": "ffight_arrange", "title": "Final Fight (Arrange)",
    },
    "jp": {
        "cd_subdir": "cd_jp_full", "disc_flag": "--jp-disc",
        "voiced_kind": "jp", "track_alias": {"tr25": "tr28", "tr26": "tr29"},
        # -128 not -120: ADX loop points must be 32-sample aligned, and 128 is
        # 4 ADX frames sitting mid-range of the per-track offsets actually
        # measured (-101 .. -139).  The 8-sample difference from the mean is
        # 0.18 ms, against a 163 ms crossfade.
        "loop_shift": -128,
        "opening_cmd": 0x52,
        # Japan enters 20.4 s earlier than the US relative to its own title, so
        # it needs a much SHORTER wrap: 2.88 s against the US 10.24 s.  In US
        # sample coordinates like every other authored loop here; loop_shift is
        # applied on top.
        "opening_loop": (1056928, 1183904, 1, 1),
        "pack": "ffight_arrange_jp", "title": "Final Fight (Arrange, Japan)",
    },
}


def apply_region(rows: list["MapRow"], region: str) -> list["MapRow"]:
    """Move the opening PLAY onto the cue THIS region issues first.

    The trigger map is written against the US cue order, where the opening
    enters on 0x35.  Japan issues 0x52 first, so for it the PLAY moves there.

    Only the verb and track move.  `suppress` belongs to the CUE, not to the
    play row: 0x52 is the board's song and is gated in both regions, 0x35 and
    0x36 are the phone and are gated in neither.  The map deliberately carries
    the play row's suppress bit along with it, which silenced Japan's ring.
    """
    cfg = REGIONS[region]
    if cfg["opening_cmd"] == 0x35:
        return rows
    start = next(r for r in rows if r.cmd == 0x35 and r.verb == VERB_PLAY)
    out = []
    for r in rows:
        if r.cmd == 0x35:
            out.append(dataclasses.replace(
                r, verb=VERB_NONE, track=None,
                cue="Phone RINGING (this region enters on "
                    f"0x{cfg['opening_cmd']:02x})"))
        elif r.cmd == cfg["opening_cmd"]:
            out.append(dataclasses.replace(
                r, verb=VERB_PLAY, track=start.track,
                cue="Opening entry (JP) - the SONG cue - ATTRACT ONLY"))
        else:
            out.append(r)
    return out

# JP tracks come RAW from the JP disc rip: tools/extract_cdda.py against
# roms/segacd/"Final Fight CD (JP).cue" -> the internal listening archive
# Identity verified by NCC against the backport's aligned references
# (opening = tr23 r=1.000; ending = tr24 r=1.000 mid-file).

# Loop treatment (user decision, on the MSCPLAYR evidence).  Every track in the
# pack must appear in exactly one of these two sets.
LOOPING = {"tr02", "tr03", "tr04", "tr05", "tr06", "tr07", "tr08", "tr09",
           "tr10", "tr11", "tr12", "tr17", "tr25", "tr26"}
ONE_SHOT = {"tr13", "tr14", "tr15", "tr16", "tr18"}



def load_triggers(path: Path = TRIGGER_TSV) -> list[MapRow]:
    """Parse the tracked trigger map.  Every row is built — the map carries no
    confidence column because every row in it is already snapshot- or
    trace-anchored; rows that were not are commented out or absent (unmapped
    commands fail open to the arcade original, which is the correct outcome)."""
    out: list[MapRow] = []
    for r in _rows(path):
        if len(r) < 5:
            raise ValueError(f"short row in {path.name}: {r!r}")
        cmd_s, verb_s, sup_s, track_s, cue = r[0], r[1], r[2], r[3], r[4]
        if verb_s not in VERBS:
            raise ValueError(f"cmd {cmd_s}: unknown verb {verb_s!r}")
        verb = VERBS[verb_s]
        cmd = int(cmd_s, 16)
        if not 0 <= cmd < protocols.SF2_TRIGGER_ROWS:
            raise ValueError(f"cmd {cmd_s} outside the CPS1 byte-command range")
        track = None if track_s.strip() in ("-", "") else track_s.strip()
        if verb == VERB_PLAY and track is None:
            raise ValueError(f"cmd {cmd_s}: verb=play with no track")
        if verb == VERB_NONE and track is not None:
            raise ValueError(f"cmd {cmd_s}: verb=none must not name a track")
        # verb=none, suppress=0 is a DOCUMENTED FAIL-OPEN (the shape mtwins
        # uses): it produces bytes identical to an unmapped command, so it is
        # skipped at write time and the real byte reaches the Z80.  It exists so
        # the map can record that a cue was considered and deliberately left to
        # the arcade -- which is the correct handling for an SE that must not be
        # suppressed (0x28 Damnd's laugh, 0x34 train, per 0x3c's precedent).
        if verb == VERB_NONE and int(sup_s) not in (0, 1):
            raise ValueError(f"cmd {cmd_s}: verb=none suppress must be 0 or 1")
        out.append(MapRow(cmd=cmd, verb=verb, suppress=int(sup_s),
                          track=track, cue=cue))
    seen = {}
    for row in out:
        if row.cmd in seen:
            raise ValueError(f"duplicate command 0x{row.cmd:02x} in {path.name}")
        seen[row.cmd] = row
    return out


# ------------------------------------------------------------------ audio ---
def ensure_disc_audio(dest: Path, needed: set[str], disc: str | None,
                      flag: str) -> None:
    """Fill the extraction cache from the user-supplied rip (pregap=trim --
    the Final Fight rips cut at INDEX 01, verified byte-identical)."""
    from .discsrc import ensure_audio_cache
    ensure_audio_cache(dest, needed, disc, flag, pregap="trim")


# ------------------------------------------------------------------ build ---
def build(variant: str, out: str | None = None, triggers_path: str | None = None,
          cd_dir: str | None = None, measure_snr: bool = True,
          us_disc: str | None = None, jp_disc: str | None = None,
          region: str = "us") -> dict:
    if variant not in ("pcm", "adx"):
        raise ValueError(f"unknown variant {variant!r}")
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}")
    cfg = REGIONS[region]
    alias = cfg["track_alias"]
    rows = apply_region(
        load_triggers(Path(triggers_path) if triggers_path else TRIGGER_TSV),
        region)
    cd = Path(cd_dir) if cd_dir else CD_DIR.parent / cfg["cd_subdir"]
    voiced = [v for v in VOICED_CUES if v[1] == cfg["voiced_kind"]]
    needed = {alias.get(r.track, r.track) for r in rows if r.track}
    needed |= {v[2] for v in voiced}
    ensure_disc_audio(cd, needed,
                      us_disc if region == "us" else jp_disc, cfg["disc_flag"])

    tracks = [r.track for r in rows if r.track]
    unknown = sorted(set(tracks) - LOOPING - ONE_SHOT)
    if unknown:
        raise ValueError(f"tracks with no loop treatment decided: {unknown}")
    dup = sorted({t for t in tracks if tracks.count(t) > 1})
    if dup:                      # not an error, but it must be visible
        print(f"[ffight] note: track(s) driven by more than one command: {dup}")

    codec = CODEC_PCM if variant == "pcm" else CODEC_ADX
    title = cfg["title"] + (" [PCM]" if variant == "pcm" else "")
    w = PackWriter(cps1_protocol(), title=title,
                   trigger_rows=protocols.SF2_TRIGGER_ROWS,
                   default_rate=44100, xfade_samples=XFADE_SAMPLES)

    ti_of: dict[str, int] = {}
    snrs: list[tuple[str, float]] = []
    total_audio = 0
    print(f"[ffight] === variant {variant} ===")
    for row in rows:
        if row.verb == VERB_NONE:
            if row.suppress == 0:
                # Documented FAIL-OPEN: identical bytes to an unmapped command,
                # so do not write it -- the real byte must reach the Z80 or the
                # SE it names gets muted.
                print(f"[ffight] 0x{row.cmd:02x} -> (fail open, not written)"
                      f"{'':>21}  {row.cue}")
                continue
            # Silence-only: suppress the arcade cue, start nothing.  Keeps the
            # already-playing arranged track running (opening sequence).
            w.set_trigger(row.cmd, TriggerRow(verb=VERB_NONE, track=0,
                                              gain=0x7f, suppress=row.suppress))
            print(f"[ffight] 0x{row.cmd:02x} -> (silence, no restart)"
                  f"{'':>26}  {row.cue}")
            continue

        track = row.track
        if track not in ti_of:
            fname = alias.get(track, track)
            src = cd / f"{fname}.wav"
            pcm, rate, ch, n = read_wav(src)
            loops = track in LOOPING
            authored = AUTHORED_LOOPS.get(track)
            if track == OPENING_TRACK:
                # the opening's wrap is per-region: it is sized so the master's
                # own outro lands on that ROM's title screen.  See REGIONS.
                authored, loops = cfg["opening_loop"], True
            # NOT an elif: the opening loop is written in US sample coordinates
            # like every other authored loop, so it needs the region shift too.
            if authored and cfg["loop_shift"]:
                als, ale, axf, acnt = authored
                sh = cfg["loop_shift"]
                authored = (als + sh, ale + sh, axf, acnt)

            if variant == "pcm":
                data = pcm
                coef1 = coef2 = 0
                loop_end_byte = len(data) if loops else 0
                loop_end_sample = n if loops else 0
                if authored:
                    als, ale, axf, acnt = authored
                    loop_start_sample, loop_start_byte = als, als * 2 * ch
                    loop_end_sample, loop_end_byte = ale, ale * 2 * ch
                    if axf and acnt == 0:
                        data = data[:loop_end_byte + XFADE_SAMPLES * 2 * ch]
            else:
                data, coef1, coef2, snr = encode_adx_track(
                    pcm, rate, ch, n, track, measure_snr)
                if snr is not None:
                    snrs.append((track, snr))
                # Whole-track repeat: loop_start is sample 0 (trivially
                # frame-aligned, asserted) and loop_end is the end of the
                # stream, which is a whole number of ADX frames by
                # construction.  loop_end_sample is the true sample count; the
                # byte field covers ceil(n/32) frames, so the two differ by the
                # <32-sample encoder pad, inside format.py's tolerance.
                if adxcodec.samples_to_stream_byte(0, ch) != 0:
                    raise ValueError("loop start not frame-aligned")
                loop_end_byte = len(data) if loops else 0
                loop_end_sample = n if loops else 0
                if loops and loop_end_byte % (adxcodec.FRAME_BYTES * ch):
                    raise ValueError(f"{track}: loop end not frame-aligned")
                if authored:
                    als, ale, axf, acnt = authored
                    loop_start_sample, loop_start_byte = \
                        als, adxcodec.samples_to_stream_byte(als, ch)
                    loop_end_sample, loop_end_byte = \
                        ale, adxcodec.samples_to_stream_byte(ale, ch)
                    if axf and acnt == 0:
                        # infinite crossfade stores exactly loop_end + blend
                        # tail (format rule); beyond is never reachable
                        tail = XFADE_SAMPLES // 32 * 18 * ch
                        data = data[:loop_end_byte + tail]

            if not authored:
                loop_start_sample = loop_start_byte = 0
            kind = (("inner loop" if authored[3] == 0 else "finite loop")
                    if authored else
                    "whole-track loop" if loops else "one_shot")
            meta = TrackMeta(
                sample_rate=rate, channels=ch, codec=codec, gain=0x7f,
                loop_start_sample=loop_start_sample,
                loop_start_byte=loop_start_byte,
                loop_end_sample=loop_end_sample, loop_end_byte=loop_end_byte,
                coef1=coef1, coef2=coef2,
                xfade_enable=(authored[2] if authored else 0),
                loop_count=(authored[3] if authored else 0),
                name=f"{track} ({kind})",
                source=f"ffightcd/{cfg['cd_subdir']}/{fname}.wav[0:{n}]")
            ti_of[track] = w.add_track(data, meta)
            total_audio += len(data)
            print(f"[ffight] 0x{row.cmd:02x} -> {track} track{ti_of[track]:<3} "
                  f"{n / rate:7.2f}s  "
                  f"{'loop ' if loops else 'once '}  "
                  f"{len(data) / 1e6:7.2f} MB  {row.cue}")
        else:
            print(f"[ffight] 0x{row.cmd:02x} -> {track} track{ti_of[track]:<3} "
                  f"{'(shared)':>22}  {row.cue}")
        w.set_trigger(row.cmd, TriggerRow(verb=VERB_PLAY, track=ti_of[track],
                                          gain=0x7f, suppress=row.suppress))

    # ---- voiced extension cues (0x70-0x73) --------------------------------
    for vcmd, kind, vtrack, vlabel in voiced:
        vsrc = cd / f"{vtrack}.wav"
        pcm, rate, ch, n = read_wav(vsrc)
        lead = PREGAP_LEAD.get(vcmd)
        if lead:
            # Pure prepend: fully specified by `lead`, so gate it structurally
            # rather than by hash -- the length must grow by exactly the lead
            # and the head must be silent.  A drift here cannot hide.
            head = bytes(lead * ch * 2)
            pcm, n = head + pcm, n + lead
            if len(pcm) != (n * ch * 2) or any(pcm[:lead * ch * 2]):
                raise SystemExit(f"pregap lead broken on 0x{vcmd:02x}")
            vlabel += f" [+{lead / rate:.3f}s disc pregap]"
        cut = ARCADE_CUT.get(vcmd)
        if cut:
            pcm, n = arcade_cut(pcm, ch, rate, *cut)
            pin = ARCADE_CUT_PIN.get(vcmd)
            if pin:
                import hashlib
                got = (hashlib.md5(pcm).hexdigest(), n)
                if got != pin:
                    raise SystemExit(
                        f"ARCADE_CUT drift on 0x{vcmd:02x}: got {got}, "
                        f"pinned {pin} -- the transform no longer matches "
                        f"the files the backport ROMs were tuned against")
            vlabel += f" [arcade cut -> {n / rate:.2f}s]"
        if variant == "pcm":
            vdata, vc1, vc2 = pcm, 0, 0
        else:
            vdata, vc1, vc2, _ = encode_adx_track(pcm, rate, ch, n,
                                                  f"{kind}_{vtrack}", False)
        vmeta = TrackMeta(
            sample_rate=rate, channels=ch, codec=codec, gain=0x7f,
            loop_start_sample=0, loop_start_byte=0,
            loop_end_sample=0, loop_end_byte=0,          # one-shot
            coef1=vc1, coef2=vc2, xfade_enable=0,
            name=f"{kind}_{vtrack} (voiced one_shot)",
            source=(f"ffightcd/{cfg['cd_subdir']}/{vtrack}.wav"
                    + (f"[arcade cut @{cut[0]}s -{cut[1]}bars -> 0:{n}]"
                       if cut else f"[0:{n}]")))
        vti = w.add_track(vdata, vmeta)
        w.set_trigger(vcmd, TriggerRow(verb=VERB_PLAY, track=vti, gain=0x7f,
                                       suppress=1))
        total_audio += len(vdata)
        print(f"[ffight] 0x{vcmd:02x} -> {kind}/{vtrack} track{vti:<3d} "
              f"{n/rate:7.2f}s  voiced one-shot  {vlabel}")

    name = cfg["pack"] + ("_pcm" if variant == "pcm" else "") + ".cpk"
    out_path = Path(out) if out else PACKS_DIR / name
    w.write(out_path)
    size = out_path.stat().st_size

    # Read the pack back and confirm the silence-only rows survived.  These are
    # invisible to audition.verify (it skips verb==0 rows), and they are the
    # rows the opening sequence depends on.
    rd = PackReader(out_path)
    try:
        for row in rows:
            got = rd.triggers[row.cmd]
            if got.verb != row.verb or got.suppress != row.suppress:
                raise ValueError(
                    f"readback: cmd 0x{row.cmd:02x} is verb "
                    f"{VERB_NAMES[got.verb]}/suppress {got.suppress}, "
                    f"expected {VERB_NAMES[row.verb]}/{row.suppress}")
            if row.verb == VERB_PLAY and got.track != ti_of[row.track]:
                raise ValueError(f"readback: cmd 0x{row.cmd:02x} wrong track")
        if rd.header.proto.latch_page != 0x800180:
            raise ValueError("readback: latch page is not the CPS1 dialect")
        if rd.header.trigger_rows != protocols.SF2_TRIGGER_ROWS:
            raise ValueError("readback: trigger table is not 256 rows")
    finally:
        rd.close()

    image = size + FFIGHT_ROM_BYTES
    n_play = sum(1 for r in rows if r.verb == VERB_PLAY)
    n_sil = sum(1 for r in rows if r.verb == VERB_NONE)
    print(f"[ffight] {out_path}")
    print(f"[ffight]   pack        {size:,} B ({size / 1e6:.1f} MB), "
          f"{len(w.tracks)} tracks, {n_play} play + {n_sil} silence triggers")
    print(f"[ffight]   + ROM       {FFIGHT_ROM_BYTES:,} B "
          f"= image {image:,} B ({image / 1e6:.1f} MB)")
    over = image - DDR_HARD_CAP_BYTES
    verdict = (f"OVER by {over:,} B ({over / 1e6:.1f} MB)" if over > 0
               else f"fits, {-over:,} B ({-over / 1e6:.1f} MB) headroom")
    print(f"[ffight]   256 MiB cap {DDR_HARD_CAP_BYTES:,} B -> "
          f"{100.0 * image / DDR_HARD_CAP_BYTES:.1f}% — {verdict}")
    if image > DDR_HARD_CAP_BYTES:
        print("[ffight]   *** THIS IMAGE EXCEEDS THE DDR CAP — built on "
              "purpose, to be tested on hardware ***")

    if snrs:
        worst = min(snrs, key=lambda s: s[1])
        mean = sum(s for _, s in snrs) / len(snrs)
        print(f"[ffight]   ADX SNR: mean {mean:.2f} dB, worst "
              f"{worst[0]} {worst[1]:.2f} dB, best "
              f"{max(snrs, key=lambda s: s[1])[1]:.2f} dB")
        for t, s in snrs:
            print(f"[ffight]     {t}  {s:6.2f} dB")

    return {"path": out_path, "size": size, "image": image,
            "tracks": len(w.tracks), "snrs": snrs, "audio": total_audio}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cpsplus.pack.build_ffight_arrange")
    ap.add_argument("--variant", choices=("pcm", "adx", "both"), default="both")
    ap.add_argument("--out", help="output path (single-variant builds only)")
    ap.add_argument("--triggers")
    ap.add_argument("--cd-dir")
    ap.add_argument("--us-disc", help="US Final Fight CD rip: a .cue, or a "
                    ".zip/.7z containing one (needed when the extraction "
                    "cache is empty)")
    ap.add_argument("--jp-disc", help="JP Final Fight CD rip, same forms")
    ap.add_argument("--region", choices=("us", "jp", "both"), default="us",
                    help="which region pack to build (default us).  Each "
                    "region needs ONLY its own disc and carries only its own "
                    "voiced cutscene pair -- see REGIONS")
    ap.add_argument("--no-snr", action="store_true",
                    help="skip the ADX decode-back SNR measurement")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args(argv)
    if a.out and (a.variant == "both" or a.region == "both"):
        ap.error("--out needs a single --variant and a single --region")

    variants = ["pcm", "adx"] if a.variant == "both" else [a.variant]
    regions = ["us", "jp"] if a.region == "both" else [a.region]
    results = []
    for reg in regions:
        for v in variants:
            res = build(v, out=a.out, triggers_path=a.triggers,
                        cd_dir=a.cd_dir, measure_snr=not a.no_snr,
                        us_disc=a.us_disc, jp_disc=a.jp_disc, region=reg)
            results.append(res)
            if not a.no_verify and not audition_verify(str(res["path"])):
                raise SystemExit(f"verification FAILED for {res['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
