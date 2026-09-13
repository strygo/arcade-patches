"""Assemble the finished ROM set: patch the arcade program, place the
engine and its data blobs, and write the members.

Inputs are the user's arcade romset plus the conv directories the scene
stages produced.  Nothing here reads a previously built set.
"""
from __future__ import annotations
import argparse
import json
import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ffcd.cps1 import chunky_to_planar  # noqa: E402
from ffcd import romset  # noqa: E402
from engine import build_engine, be16, be32, ENGINE, DATA, OFFTAB, MARGTAB, CAPTAB, CAPPAL, SCRIPT, PALBLOCKS, BASEMAPS, DELTAS, TAIL_SCRIPT, TAIL_BASEMAP, STATE  # noqa: E402
import fmloop  # noqa: E402
import vbsched  # noqa: E402
import title_ex  # noqa: E402
import select_window  # noqa: E402
from text import (CREDITS_SRC, build_ending_captions, build_ending_captions_rom,

                  build_credits, rom_caption_lines)  # noqa: E402




# The developer tree's repo root.  An unpacked kit can sit only a folder or
# two deep (C:\ffex\build\rom.py), where parents[3] doesn't exist; the kit
# always names its romset in paths.json, so REPO is only a default there.
_here = Path(__file__).resolve()
REPO = _here.parents[3] if len(_here.parents) > 3 else _here.parents[-1]
# Everything the build reads lives under the TRACK -- the directory that
# holds build/, ffcd/ and data/.  Anchoring to the track rather than to a
# repo root is what lets this run from wherever it is unpacked.
TRACK = Path(__file__).resolve().parents[1]
# The arcade romset normally sits at <repo>/roms/mame0260.  A caller whose
# copy lives elsewhere -- the reconstruction kit, run against the user's own
# files -- writes paths.json beside the track and names it there.  Stage
# processes are separate interpreters, so a file is what carries it.
ROMSET = REPO / "roms" / "mame0260"
_cfg = Path(__file__).resolve().parents[1] / "paths.json"
if _cfg.exists():
    import json as _json
    ROMSET = Path(_json.loads(_cfg.read_text()).get("romset", ROMSET))


ROW0, COL0 = 0x11, 0x26


GFX_FILES = ["c07us01.c09", "c07us01.c11", "c07us01.c13", "c07us01.c15"]


GFX_FILES6 = ["c07us01.c17", "c07us01.c19", "c07us01.c21", "c07us01.c23"]






# ---- CD-timed ending subtitles.  The arcade script IS the CD voice script,
# so the lines are the stock 0x13a/0x13c text -- with "father" lowercased on
# request; only the placement and the clock are ours.  Frames are WORD-level
# VO onsets from data/audio/ending_vo_us.tsv (t * 59.637, onset-15) relative
# to the ENGINE2 INIT (= the genuine ending entry).  Segment-level times
# would carry ~1 s of VAD padding.  Each line fires ON its speaker's word
# onset, not 15 frames early -- a lead-in reads as the subtitle beating the
# voice.  Ends are the next onset, so a line stays up until the next one
# starts.
ENDING_CAPTIONS_US = (
    (379,  618,  6, ("Oh father!", "I was so scared...")),
    (633,  833, 14, ("I'm so glad to see", "they didn't hurt you.")),
    (848,  955, 14, ("I'm so sorry, Jessica.",)),
    (970,  1144, 14, ("I thought I'd lost you", "like I lost your mother.")),
    (1159, 1365, 14, ("I'll never let anything", "bad happen to you again.")),
    # t1 is her VO's end, not 1530: the conv fade runs after the line, and
    # 1530 would leave the caption sitting alone on a black screen for ~45f.
    (1380, 1490,  6, ("I love you father.",)),
)


# Lip-sync anchors: capture cum-frame -> CPS frame (see the retime loop).
# Derived from the full per-frame L/R motion timeline of the capture
# (Haggar = left half, Jessica = right): the animation follows the longer
# JAPANESE dialogue, so Haggar has THREE speech blocks (440-720, 760-1160,
# 1240-1360) and Jessica's final-line lips sit at 1440-1490 -- Haggar's
# third block must not be mistaken for her line, or the sync drifts worse as
# the scene goes on.  Each motion block is pinned to its US VO span
# (word-level): blocks compress up to ~1.7x / stretch ~2x, and every silent
# gap lands on a real VO gap.
# These anchors are drift-corrected.  The ending body they describe is the
# MODELED VM sweep (the clock-phase fix); the esnap is a direct slice of it
# (ending.py), so the mouth blocks below are measured on the conv's OWN INPUT
# and pinned to the VO spans -- the same method in both regions.
#
# Measured esnap blocks (local frames):
# us  J 183-359 | H 409-553 | H 618-714 | H 746-889 | H 930-1108
# J 1159-1264 | native fade 1297-1335
# jp  J 67-176, 235-333 | H 511-709 | H 753-1189 | H 1265-1384
# J 1471-1523
#
# US uses a single constant offset, not a per-block pin map.  A 14-pin map
# fits 94.9% of voiced duration inside a mouth against a 1-parameter offset's
# 90.4%, but that comparison is worthless: a more flexible model always fits
# better.  It is the same trap that produces a fitted 1.2525 clock rate and a
# phantom k+1 sprite term -- fitting against a VO table five detectors cannot
# validate.
#
# ASK INSTEAD WHAT THE RESIDUAL IS.  Each block, on its own, wants an offset:
#
# Jessica 1  +197        Haggar 3  +225
# Haggar 1   +224        Haggar 4  +230
# Haggar 2   +231        Jessica 2 +222
#
# The last five: mean 226.4, **sd 3.5 frames**.  Least-squares slope through
# them: -0.0028 f/f, i.e. **-3 frames across the entire reunion** -- FLAT.  So
# there is no drift and nothing per-line; there is one constant, +-3 frames of
# block-boundary noise, and ONE outlier: Jessica's first line at +197, 29
# frames off the rest.
#
# The map's whole 4.5-point advantage is that outlier plus noise.  And the
# outlier is the DISC'S OWN RELATIONSHIP: at the common offset her mouth opens
# 29 frames after her row does, and a map overriding that forces her onto the
# caption -- inventing sync the disc does not have, exactly what a JP map does.
# Review: "why would the us continue to depend on the map?"
#
# A RATE is not it either, though a drift would be physical (the 1.25 vs
# 1.2516617 class): a free linear fit cps = a + b*capture gives b=1.0395 for
# 91.0% -- 0.7 points over the pure offset for a second parameter, on a
# ZERO-WIDTH plateau, and 1.0395 matches no physical quantity (the rate
# conversion and its inverse, squared, doubled and absent all score
# 90.1-90.8%).  A map's implied slope of 1.0231 against the physical 0.99523
# is a +2.8% stretch, +30 frames, that its pins impose.
#
# The JP/US pin-map tables pin blocks measured on a DIFFERENT timeline (US on
# the E6-attract static-hold capture with no animation to align, JP on a
# capture-DTW'd esnap) -- measuring -13..+15 (US) and +20..+51 (JP starts)
# against the voice, where these anchors measure +1.  Do not resurrect them
# without also reverting the ending convs -- anchors and body are one unit.
#
# CAVEAT: the US ending has no usable hardware oracle -- work/build/ending/snap
# is a pre-silenced capture whose clock is documented as corrupted.
ENDING_ANCHORS_US = (
    (0, 228),                     # head delay; uniform rate after it
    (1333, 1555),                 # slope 0.9955 == the 59.637/59.92275 rate
    (1513, 1560),                 # the documented tail collapse; handback held
)


# ---- CD-6 farewell subtitles (the Cody/Jessica exchange).
# The CD DOES voice this scene -- the dialogue is in the ending track at
# 96.2-110.7 s, and matches the stock arcade text (id 0x13e) almost word
# for word.  It reads as unvoiced only if you transcribe with a VAD filter,
# which discards speech mixed under the music.
#
# The arcade reaches this scene ~20 s before the straight CD track reaches
# the dialogue, because the CD spends 64 s on the credits-and-spar block
# where the arcade spends 46.5 s.  Rather than retime the ROM, the AUDIO is
# cut: tools/cut_ending_track.py lifts 7 bars out of the steady 82 BPM
# credits loop.  These frames are the word onsets measured on that cut
# track, scene-relative, onset-15 -- the same convention as the reunion.
# ---- CD-6 farewell subtitles, JP.  The arcade ROM already HOLDS this
# exchange -- record 0x3d is the Cody/Jessica farewell, the same text the
# stock JP ending types during segment G -- so these are the ROM's own
# glyphs on our schedule, not authored text.  Speaker colour comes with
# them: Jessica's lines carry attr 6 and Cody's 14, which is where the
# CODY_PAL/GUY_PAL convention was read from in the first place.
#
# line 0  pal 6   なぜにげるの、、、、          Jessica
# line 1  pal 14  、、おれは、ふつうにはいきられない   Cody
# line 2  pal 14  おとこだ、、、、いいならこい！だれにも
# line 3  pal 14  できんいきかたをさせてやる！ジェシカ。
# line 4  pal 6   コーディー！                  Jessica
#
# The CD's close-up is a STATIC held cel here -- measured, only 3 changed
# pixels across the whole held section -- so unlike the Guy/Cody scene
# there is no mouth animation to anchor against.  These come from the
# TRACK instead, which is exact:
#
# tr24 farewell dialogue   121.0 / 125.0 / 128.0 / 132.0 / 134.0 s
# minus the arcade cut     29.32 s (MEASURED by sample-matching the cut
# file against the original; the bar arithmetic
# predicts 29.201, the crossfade covers the rest)
# engine f = t * 59.6, and ENGINE3 starts at f5318, so scene-relative
# = f - 5318  ->  +146 / +385 / +563 / +802 / +921
#
# Lines 2-4 deliberately run into the pan (which starts at +559): the CD
# keeps talking over the tilt down to their feet, and the engine's band
# wipe covers the caption rows throughout.
FAREWELL_CAPTIONS_JP = (
    # The head freeze moves ENGINE3 from f5318 to f5631, and the cut is 8
    # bars (MEASURED shift 23.48 s), not 10.  Ten bars remove 56.0-85.2 s,
    # running straight into the 85 s vocal -- which would throw the credits
    # out around Jessica's "CODY!".  Eight bars take 56.0-79.5 s, entirely
    # instrumental, so every vocal survives.
    # engine f = (t_tr24 - 23.48) * 59.6; scene-relative = f - 5631
    # NO credits compensation: the backport section costs 7 rows and
    # collapsing the roll's fully-blank runs to one dot each gives 7 back,
    # so the roll is still 87 rows and ENGINE3 still arms at f6067.
    (181,  415, 0x3d, 0, 23),
    (420,  593, 0x3d, 1, 23),
    (598,  832, 0x3d, 2, 23),
    (837,  951, 0x3d, 3, 23),
    (956, 1150, 0x3d, 4, 23),
)


FAREWELL_CAPTIONS_US = (
    # Frames derived by SAMPLE-MATCHING the cut track against the original
    # (residual 0.00): the splice removes 20.5604 s, and the scene's first
    # frame is at 72.66 s of the cut file.  Onsets are exact; do not
    # re-derive them with a transcriber -- transcribing the cut file directly
    # runs ~0.8 s early and puts every caption AND every lip anchor ahead of
    # the voice.
    # The shift is +54 (12, the afplay startup allowance, + 42).  User
    # "jessica's voice starts a bit too late ... we may be
    # holding for slightly too long".  MEASURED rather than nudged: the
    # farewell two-shot is a STILL -- a per-frame diff of both jaw boxes
    # across the whole scene head is 0.00, neither face animates -- so the
    # caption is the only cue tying picture to voice, and this schedule is
    # the only thing that can be wrong.  Utterance onsets were detected in
    # the cut track (centre-channel = |L+R| - |L-R|, which lifts the centred
    # dialogue out of the wide music bed) and the single global shift that
    # best fits ALL FIVE lines is +41.7 f, RMS residual 13.9 f; the line the
    # user flagged fits to +3.5 f.  Rounded to +42.
    #
    # Note this restores the ~0.8 s the header above says sample-matching
    # removes: that correction is not applied to these numbers.  Do not
    # "re-fix" it by subtracting 42 again.
    # Lines 4 and 5 carry a further +7, bracketed by ear.
    # Line 4's speech onset is measurable (it follows a real pause) at
    # 87.05 s of the cut track, which makes this a statement about the LEAD
    # a caption should have, not about where the voice is:
    #
    # +0  -> fires at onset-15 f  -> "a bit fast relative to speech"
    # +15 -> fires at onset-0  f  -> "a bit too delayed"
    # +7  -> fires at onset-8  f  <- taken
    #
    # So this scene wants roughly half the opening's lead (that schedule
    # uses fire = onset - 15).  Line 5 gets the same +7: its onset is NOT
    # measurable -- in the voice band (300-3400 Hz, centred) the dialogue
    # runs CONTINUOUSLY from 88.4 to 90.4 s with no silence between Cody's
    # last line and Jessica's, and her line is then delivered quietly, well
    # under the music, as a low-level centred segment from ~90.45 to
    # ~91.75 s.  A loudness threshold reports nonsense on both counts, so
    # it tracks line 4 rather than a detector.
    # NO credits compensation: the backport section costs 7 rows and
    # collapsing the roll's fully-blank runs to one dot each gives 7 back,
    # so the roll is still 87 rows and ENGINE3 still arms at f4766.  These
    # are the values approved before the credit existed.
    # Line 2 fires at its measured voice onset - 8 f, the lead line 4
    # settled on: her "How can you..." harmonics enter at 78.50 s of the
    # cut track (centre-channel spectrogram; the whisper word table agrees
    # to 1 f), a full second before the +42 global fit placed it -- that
    # fit was pinned by the noisier later lines.  Flagged as delayed on
    # hardware.  Line 1 clears when line 2 fires, as before.
    (232,  340,  6, ("Where are you going?",)),
    (340,  606,  6, ("How can you just walk", "away now?")),
    (612,  843, 14, ("I want to stay here with you Jessica,", "but I can't...")),
    # "street", not "streets" -- matches the delivered line (user).
    (850, 1014, 14, ("not while evil still stalks", "the street.")),
    # "Oh Cody..." was late in EVERY build so far and my first reading of it
    # was wrong.  The tell was "still too late" after three different values
    # had been tried (90.07 / 90.18 / 90.32 s of the cut track), all of them
    # inside her line rather than ahead of it.
    #
    # The back half of this scene has two distinct bursts with a real gap
    # between them: 89.20-89.60 (Cody finishing "...the streets.") and
    # 89.85-90.40, much louder, which is her.  So the caption belongs at
    # ~89.72 for the same ~8 f lead line 4 settled on, and line 4 has to
    # clear by 1014 to make room -- it was sitting on screen until 90.09,
    # half a second after Cody stopped, which is what pinned her line late.
    #
    # The quiet 90.45-91.75 segment is NOT her line; it is the music tail.
    # Do not mistake it for a soft delivery and move the caption toward it.
    (1017, 1191, 6, ("Oh Cody...",)),
)








# ---- JP lip-sync anchors: ENDING_ANCHORS_US's strategy, JP voice track.
#
# Derived from the conv's OWN INPUT -- mouth-box per-frame diffs over
# work/build/ending/snap f0-1512, the exact frames the reunion conv consumes
# (rconv --window 0-1512), so there is no capture-offset to get wrong.
#
# source mouth blocks (conv frames):
# Jessica  39-148, 207-304        (one voice row, two bursts)
# Haggar   484-704 | 760-1160 | 1239-1354
# Jessica  1441-1493              (closing line)
# voice rows (engine frames, tr24 word clock x 59.6 -- the same values
# the JP captions fire on):
# 371-554 | 720-1013 | 1013-1306 | 1306-1490 | 1490-1668 | 1700-1787
#
# Block pairing: Haggar's middle block (760-1160) spans voice rows 2+3
# (1013-1490) -- pairing it with row 2 alone would need a 0.73x COMPRESS
# where the ROM measurably needs a 1.19x stretch.  His last block maps to
# row 4.  The 1160->1239 source pause collapses (both ends pinned at
# ~1490): it is a held still, and emap's max(1,...) keeps every event
# alive.
#
# The final pair PINS the reunion's end: raw span 1513 (builder prints it)
# -> engine 1868, the scene-start frame measured from the SHIPPED script,
# so the Guy/Cody scene, ENGINE3's arm frame and the farewell schedules
# all stay exactly where they are tuned.  Head: (0,0)->(39,371) parks the
# wait on the settled paint instead of a 314-frame freeze event.
# Same structure as the JP/US pin-map table (Jessica's two bursts share
# row 1; Haggar's long block spans rows 2+3), measured on the modeled
# sweep's esnap: the clock-phase model reproduces the CD pacing from first
# principles rather than absorbing it empirically from a DTW alignment onto
# the e6 CAPTURE.
# Method: --no-ending-anchors build of the CURRENT conv, measured in game
# (ending_metrics.lua from states/ffightjs01/r6clear.sta +
# measure_ending_v2.py), each block pinned to its VO span.  Source values are
# the mouth blocks of the conv's own input, work/build/vmsweep_v3/jp_esnap:
#
# Jessica  66-176, 235-332          (one voice row, two bursts)
# Haggar   535-708 | 752-1188 | 1263-1382
# Jessica  1469-1521                (closing line)
#
# Two effects matter here.  The CLOCK correction is small -- blocks differ by
# only 1 to 2 frames against the canonical 1.25-clock esnap -- so staleness
# alone is not the issue.  The real lever is the pins: pinning Haggar's first
# block at 511 when his first flap is at 535 scales the 24-frame lead up to
# put his mouth 37 frames (0.6 s) AFTER his voice starts (the block runs
# 757-804 against a VO row opening at 720).  So each pin sits at its actual
# block start.
# THE PIECEWISE LIP-SYNC MAP IS RETIRED.  Read this before ever
# adding one back.
#
# WHY A MAP WOULD EXIST.  It corrects a DISTORTED CAPTURE: the E6 attract
# playback runs the scene's talk sections at distorted pacing (needed shifts
# 340/192/144 across the three sections), which a uniform RATE cannot
# lip-sync -- each flap section pins to its voiced span instead.
#
# WHY THERE IS NONE.  The ending frames come from the VM executing the disc's
# own script at the corrected clock, so there is no pacing distortion to
# correct -- and warping VM-faithful frames onto targets displaced by a
# constant INJECTS the error rather than removing it.  The tell is in the
# numbers: consecutive blocks need x0.688 then x1.694.  Authored animation
# does not alternate like that; compensation does.
#
# WHAT REPLACED IT.  One constant.  Scanning the single shift that maximises
# voiced duration falling inside a mouth block -- mouth positions measured
# exactly, words from data/audio/ending_vo_jp.tsv, no voice detector needed:
#
# D=0  60.5%    D=229  94.6%    **D=249-256  98.6%**    D=292  91.4%
#
# 98.6% of all voiced duration inside a mouth, every segment 96-100%.  The
# scene and the dialogue were authored together and match by construction;
# only the start was wrong.  D=256 is the top of the plateau and the value
# that moves the handback least (12 frames).  Review of this build:
# "this looks great - much better than before."
#
# It also corroborates the word table, which five independent voice detectors
# could not validate against the music bed: its segments sit at irregular
# spacings (781, 1040, 1297, 1351) and one shift lands all of them at once,
# which a wrong table could not do.
#
# --ending-head-delay=<D> overrides this for re-derivation, the way
# vmconvert's --derive-offsets serves the aligner.
ENDING_ANCHORS_JP = (
    (0, 256),                     # head delay; uniform rate after it
    (1557, 1806),                 # slope 0.9955 == the 59.637/59.92275 rate
)


# With the anchors above putting the mouths ON the voice, the captions go
# on the same clock -- they are the voice spans.
JP_ENDING_CAPTIONS = (
    ( 371,  554, 0x39, 0, 23),   # Jessica: ああ おとうさん...ありがとう
    ( 720, 1013, 0x39, 1, 23),   # Haggar:  おお ジェシカ、よくぶじで...
    (1013, 1306, 0x39, 2, 23),   # Haggar:  つまに死なれ、いままた...
    (1306, 1490, 0x39, 3, 23),   # Haggar:  しまったら わたしは もう...
    (1490, 1668, 0x39, 4, 23),   # Haggar:  ほんとによかった。
    # with the reunion back at its full 1513 frames, her line
    # (VO to 1787) ends BEFORE the post-line fade-out starts (~1794);
    # the subtitle clears at the line's end, a beat before the fade.
    (1700, 1790, 0x3b, 0, 23),   # Jessica: おとうさん、、、、、
)


# The Guy/Cody scene is CD-exclusive, so the arcade ROM has no text for it
# and the Mega CD draws none -- the beat is carried by voice alone.  These
# are AUTHORED subtitles for that voice, the same treatment the USA set
# gives the CD-6 farewell.
#
# Anchors come from **session.wav**, the audio recorded DURING the capture
# run, not from a standalone tr24 rip.  session.wav is frame-locked to the
# capture by construction (its 161.86 s length is exactly frame 9700 /
# 59.92), so session_t * 59.92 IS the capture frame -- no offset to guess.
# Reading the standalone track instead cost a visible bug: it assumes tr24
# t=0 lands on capture frame 3900, and the true anchor is 3694, so every
# caption shipped 206 frames (3.44 s) LATE and the mouths moved first
# (caught in review).
#
# Cross-checked against the CD's own mouth animation, measured by
# pan-cancelled per-character frame differencing of the capture:
# Cody  mouth f5602-5738   voice f5624-5735  おれたちも いくとするか
# Guy   mouth f5774-6048   voice f5882-6020  だが いいのか
# Cody  mouth f6052-6178   voice f6082-6285  いいんだよ おれのためにも...
# engine_t = capture_f - 3966 (from the built script's own cumulative
# durations: the first scene event sits at t=1654 and is capture f5620).
# Speaker colour follows the ROM's own two-palette convention, not a
# guess: in the intro's Cody/Jessica scene (record 0x3d, and its USA twin
# 0x3e) CODY's lines carry attr 14 and Jessica's carry 6, and the reunion
# does the same with Haggar 14 / Jessica 6.  So Cody keeps 14 here and Guy
# takes 6 -- two speakers, two colours, exactly as the intro reads.
CODY_PAL, GUY_PAL = 14, 6


# A two-line utterance is ONE record holding both rows' cells, not two
# records sharing a window.  The walker reveals a record's cells one per
# frame, so two records type in PARALLEL -- Cody's last line put 27 cells
# up in 14 frames, double everyone else's rate, and read as an instant
# reveal next to the typewriter elsewhere (spotted in review).  One record
# types row 22 to the end, then row 24, at a steady one cell per frame --
# which is what the Latin builder has always done for its 2-line entries.
JP_SCENE_CAPTIONS = (
    (1919, 2150, "おれたちも いくとするか、、、", 23, CODY_PAL),
    (2176, 2350, "だが いいのか、、、", 23, GUY_PAL),
    (2366, 2640, ("いいんだよ おれのためにも",
                  "ジェシカのためにも、、、"), 22, CODY_PAL),
)


# ---- BUILD KNOBS.  These were FFCD_* environment variables read at the
# point of use, which meant a stray shell variable silently changed the ROM
# and the provenance file had to record the whole namespace to compensate.
# They are argv now: they cannot be set by accident, --help lists them, and
# a build records the argv that produced it.
OPT = {
    "ending_conv": None,        # --ending-conv DIR   the reunion conv
    "pan_conv": None,           # --pan-conv DIR      the CD-6 farewell conv
    "caption_nudge": None,      # --caption-nudge SPEC  re-derivation harness
    "ending_head_delay": None,  # --ending-head-delay D      "
    "no_ending_anchors": False, # --no-ending-anchors        "
    "no_vbl_detour": False,     # --no-vbl-detour     A/B: leave the vblank
                                # epilogue unpatched
}


REGIONS = {
    # p4:   (archive, member) for the region's 0x40001-odd program bank
    # cues: (script-frame, value) -- 0x52 melody restarts; 0x35 ring;
    # 0x36 click.  JP cue times are set from the JP script timeline.
    "us": dict(
        set_name="ffightus01",
        p4=("ffightu.7z", "ffu_43.12h"),
        p4_name="c07u.p4",
        p7_name="c07us01.p7",
        credit_header="cd cutscenes",
        key_name="ffightus01.key",
        jgfx=False,
        ending_captions=ENDING_CAPTIONS_US,
        ending_anchors=ENDING_ANCHORS_US,
        ending_entry_fade=64,
        opening_entry_fade=64,
        farewell_captions=FAREWELL_CAPTIONS_US,
        # CPS+ compatibility, zero audible change: INIT fires 0x5C -- a
        # NULL entry in the Z80 song table (driver RETs untouched, proven
        # no-op) -- purely as the pack's PLAY key for CD tr23, with a
        # 175-frame black lead-in before the map matching the CD's
        # measured track-start structure (128f pregap + 47f musical
        # lead; untrimmed track file, sample-aligned visuals).  CUES ARE
        # ALIASES: 0x76 for the melody, 0x75 for the ring -- NOT
        # 0x52/0x35.  Measured with the
        # CPS+ prototype: the raw bytes collide with the arrange pack's
        # opening entry, which is 0x35 in the US pack and 0x52 in the JP
        # one, so the cutscene's own voiced track was being replaced
        # 44.2 s in (US) and 2.5 s in (JP).  Mapping 0x52 to verb=NONE +
        # suppress so the track never re-triggers is true of the US pack
        # only; in the JP pack 0x52 is the PLAY row.  The aliases are gated
        # (verb=none suppress=1) in BOTH packs, and the Z80 stub maps them
        # back to 0x35/0x52 so stock hardware is unchanged.
        # The melody fires ONCE: fmloop.py restructures the m1's cue 0x52
        # (the score's own mid-song repeat, raised to fm_open_loop rounds)
        # to span the opening, its final chord landing on the title
        # screen's logo slam, so the schedule carries no restarts.
        cues=((0, 0x76), (2790, 0x75), (2952, 0x77)),
        fm_open_loop=(12, 1),
        # init_cue 0x70 (was 0x5C): the pack's VOICED opening key
        # per AUDIO_CONTRACT.  The Z80 stub (below, in the zip
        # writer) aliases 0x70-0x73 to the null song entry 0x5C on
        # stock hardware, so vanilla behavior is byte-identical.
        init_cue=0x70, lead_in=155,
        ending_cue=0x71,   # pack's voiced US ending key (AUDIO_CONTRACT)
        sprite_slide=True,
        repos="data/captions/us_caption_repos.json",
        # Caption schedule (reviewed): arcade story lines fired
        # at the CD voice-over timestamps (t = wav*59.92 - 1600).  0x200 =
        # scroll1-page clear before position-changing blocks; typewriter
        # lines at the same anchor overwrite (space-padded in ROM).
        captions=(  # VO-locked (whisper word times on tr23):
                  # fire = onset - 15; clears at t-6; emitter sorts
                  (254, 0x200), (260, 0x116),
                  (842, 0x200), (848, 0x117),
                  (1305, 0x200), (1311, 0x118),
                  (1593, 0x200), (1599, 0x105),
                  # +28, measured, not fitted.  This line is
                  # cd_dialogue_us.tsv's row "When they learned of Hager's
                  # plans, they took immediate action...", whose measured
                  # start is 1885, so fire = onset - 15 = 1870.  A value of
                  # 1842 implies an onset of 1857, 28 frames earlier than the
                  # table's own row boundary.  Review heard 1870 and did
                  # not object; the JP line at the same spot needed its own
                  # (much larger) correction and was bracketed by ear.
                  #
                  # The predecessor was checked and does NOT hang here, which
                  # is why only the pair moves: 0x105's voice runs to 1885 in
                  # the same row, so shifting its clear 1836 -> 1864 moves it
                  # CLOSER to its voice end (21 f before, was 49 f) -- more
                  # reading time, not less.  The US scene is a 250-frame slow
                  # dissolve (art band 85 at 1600 -> 0 at 1890) rather than
                  # JP's sharp 54-frame fade, so there is no crisp boundary
                  # here for a caption to hang past.
                  (1864, 0x200), (1870, 0x119),
                  (2602, 0x200), (2608, 0xA08),
                  (2834, 0x200),  # desk reveal: drop the A08 title
                  (3016, 0x200), (3022, 0x106),
                  (3151, 0x200), (3157, 0x107),
                  (3643, 0x200), (3649, 0x108),
                  (4154, 0x200), (4160, 0x109),
                  # 0x109 tail ("we offered before") is text-only -- the VO
                  # stops at "salary" -- and the whisper onset for the next
                  # line runs ~0.3 s early vs the measured envelope (speech
                  # burst at 80.0 s, whisper 79.73), so the clear cut the
                  # reader off.  +24f: clear rides the true pause, and 0x10A
                  # now fires at the measured "What?" burst.  (User-reported:
                  # '".. offered before." cleared slightly early'.)
                  # There is no sentence-end clear at 4483: it would drop the
                  # line 18f after "salary" while the text-only tail is still
                  # being read.  The 4583 clear (6f before "What?") covers it,
                  # buying ~1.7 s of reading time.
                  (4583, 0x200), (4589, 0x10A),
                  (4784, 0x200), (4790, 0x10B),
                  (5356, 0x200), (5362, 0x10C),
                  (5529, 0x200), (5535, 0x10D),
                  (5955, 0x200), (5961, 0x10E),
                  (7059, 0x200), (7065, 0x112),
                  (7190, 0x200), (7196, 0x113),
                  (7291, 0x200), (7297, 0x114),
                  (7462, 0x200), (7468, 0x115),
                  # VO sentence-end clears: text drops when
                  # the speech pauses, never rides a scene transition;
                  # only where a real gap precedes the next caption
                  (709, 0x200), (1227, 0x200), (2390, 0x200),
                  (5095, 0x200), (6431, 0x200),
                  (6610, 0x200), (7780, 0x200)),
        trim=((7931, 50, 8028),),
    ),
    "jp": dict(
        set_name="ffightjs01",
        p4=("ffightj.7z", "ff43.bin"),
        p4_name="c07j.p4",
        p7_name="c07js01.p7",
        credit_header="cd cutscenes",
        key_name="ffightjs01.key",
        ending_anchors=ENDING_ANCHORS_JP,
        ending_entry_fade=64,
        ending_captions_rom=JP_ENDING_CAPTIONS + JP_SCENE_CAPTIONS,
        farewell_captions_rom=FAREWELL_CAPTIONS_JP,
        jgfx=True,   # stock gfx = J content (77 SCROLL1 text tiles) in
                     # World single-bank layout, assembled from ffightj.7z
        # Ring/click match the USA build's EFFECTIVE behavior (user:
        # match the phone behavior across regions).  The shipped USA p7
        # fires the ring at t=2478 (the request said 2790, but cues fire
        # at the covering event's LOAD) and the click at 2952: an ~8 s
        # ring that starts over the knife/sketch, rides through the
        # transition, and is answered 112 frames into the desk scene.
        # JP mirror: ring on the held knife-cel boundary 2341 (sketch
        # overlap ~370f vs USA ~350f), click at 2816 -- the event right
        # as the desk wipe completes (nominal scene start 2822), per the
        # user: Haggar is already answering when the desk appears, so
        # the click belongs AT the scene start, not +112 like USA's
        # (whose desk appears before he picks up).  Ring = 475f, same
        # length as USA's 474.  (Cuts of 2705/2809 from the CD's burst
        # spacing, or 2772/2934, ring too briefly or click too late.)
        # 0x28 = Damnd's arcade laugh sample, fired as his laughing face
        # takes the TV at the split-screen tail (conv 5705 = capture
        # f9553, the reveal).  MEASURED from the stock ffightj attract:
        # its TV-laugh moment fires exactly one latch command, 0x28, in
        # both attract cycles (sound-queue tap, f2913/f9834) -- the same
        # cue the CD's own scene calls for.  JP only: the US disc's
        # villain scene keeps Damnd static and silent (user-confirmed).
        # CPS+ voiced pairing (future, AUDIO_CONTRACT): the voiced track
        # carries the CD laugh, so the backport-side alias/suppress row
        # must gate this cue there; arcade/HBMAME plays it as-is.
        # laugh nudged 5705 -> 5650 (user: "could be earlier
        # in the damnd laugh animation sequence"): onto the reveal's
        # first laughing cels rather than the settled face.
        # Melody fires once (fmloop spans the JP window, final chord on
        # the title logo slam); no restarts.
        cues=((0, 0x76), (2341, 0x75), (2816, 0x77), (5650, 0x74)),
        fm_open_loop=(12, 0),
        # 0x74 = Damnd laugh ALIAS: the Z80 stub maps it to 0x28's
        # sample, so vanilla plays the laugh; the arrange pack can
        # gate 0x74 (one planned row) without muting the game's own
        # 0x28 gameplay laugh (AUDIO_CONTRACT 1b).
        # restructure (user): the JP disc plays 2.64 s of
        # track lead over black before the map (measured 12-anchor
        # word fit), so mirror the USA structure: 0x5C (Z80-null)
        # fires at INIT as the pack's PLAY key, lead_in holds the
        # video back so the map fades in at INIT+157 (2.64 s), and
        # every caption time below carries +28: the caption pump's
        # clock starts at EVENT-0 LOAD (the override exits before the
        # pump while idx==FFFF), so the lead_in delta (+129) moves the
        # clock itself and only the residual 157-129 shifts the times
        # (a +157 shift double-counted and ran ~2.2 s late -- user:
        # "captions universally delayed").  Cue/trim
        # times ride the event stream and shift with the video
        # automatically.  PACK-SIDE FOLLOW-UP (cpsplus): move the
        # JP pack's PLAY row from 0x52 to 0x5C-at-INIT; 0x52 keeps
        # suppress=1 (see AUDIO_CONTRACT.md).
        init_cue=0x72, lead_in=130,
        ending_cue=0x73,   # pack's voiced JP ending key (AUDIO_CONTRACT)
        sprite_slide=True, jp_title=True,
        # US repos + JP kana row corrections (records 0x21/0x25 -1 row:
        # the villain split-screen 2-line captions centred in the CD
        # letterbox band; generated by gen_caption_repos_jp.py)
        repos="data/captions/jp_caption_repos.json",
        # JP caption schedule: ROM ids paired to captions
        # read off the stock ffightj attract (kana eyes-on), times from
        # cd_dialogue_jp.tsv whisper segments (t = wav*59.92 - 3847.7);
        # fire = onset - 15, clears at t-6, the US caption discipline
        # (sentence-end clears in real gaps, nothing rides a scene
        # transition, year card drops at the desk reveal like USA A08).
        # Sub-segment onsets for 0x11C/0x11D/0x121/0x126 estimated by
        # mora share inside merged whisper segments.
        # (user): the full stock narration sequence is
        # 12A -> 12B -> 12C -> 12D (12A/12B are the Metro City opener, not
        # unused variants).  12A fires at the map fade
        # (its VO onset precedes whisper segment 1, which starts
        # mid-narration at 12B's "aru no wa..."); 12B at the measured
        # segment onset; 12C ~2.6 s later (mora estimate inside the
        # merged segment); 12D at its own onset.
        # The preview wav starts at INIT; converting its times with the
        # video-mux formula (12.409 s lead) would fire everything 740 ticks
        # early (user: "shows up and changes way earlier than spoken").
        # jp_aligned_words.json uses t = W * 59.637; the onsets largely
        # reconcile with the ear-tuned values and put the Metro City
        # sentence ON the map (t247-499).  fire = onset-15; caption
        # discipline: sentence-end clears in real gaps, dialogue never
        # rides a cut (12C clamped 3 ticks to the gang-bar edge),
        # narration rides mid-sentence (12D bridges INTO the knife
        # sketch -- the 'houfuku' it depicts), year card holds to the
        # desk reveal (US A08 parity), kisamaa~ stays at its shipped
        # TV spot (its grunt is untranscribable), Cody's first line
        # (VO tail-merged) lands just after his scene cut.
        # Every entry below reads "fire = onset - 15", as this comment block
        # and the generator state.  Reading onset+13 instead double-counts a
        # +28 pump-clock shift -- jp_aligned_words.json is already in the
        # pump's own domain, so the shift must not be applied to the times as
        # well.
        # PROVEN by the ROM's own mouth animation, which is the VO (the
        # measured bursts sit on the CD's lips).  Probing the built set
        # (probes/caption_probe.lua logs the pump counter 0xFF12AE and
        # snapshots the same frame, so caption and mouth are compared with
        # NO clock conversion): Haggar's mouth opens at counter t=2953 for
        # "マイクハガーだ!" and t=3706 for "なに?" -- equal to those lines'
        # aligned onsets (2953 / 3704).  The counter domain and the aligned
        # word domain COINCIDE, so fire = onset-15 is correct with no +28 to
        # apply.
        captions=((226, 0x200), (232, 0x12A),    # Metro City (VO 247)
                  (520, 0x200),                  # 12A sentence end
                  (786, 0x200), (792, 0x12B),    # aru no wa (VO 807)
                  (978, 0x200),                  # 12B sentence end
                  (1199, 0x200), (1205, 0x12C),  # shichou (VO 1214)
                  (1490, 0x200), (1496, 0x122),  # kyodai na (VO 1511)
                  # daga: whisper's segment 7 is degenerate (both tokens
                  # pinned to the segment edges -- だ at t0 1791, が at
                  # 2006, impossible for one 2-mora word), so the onset was
                  # re-measured on the CENTER CHANNEL (the dialogue is
                  # centre-panned, the music bed is wide, so MID-SIDE in the
                  # speech band isolates the voice where a plain envelope
                  # cannot): the burst rises at t~1792 and holds to ~1816,
                  # right after the previous sentence ends at 1791.  So
                  # whisper's t0 was right and the stray が was the artifact.
                  # 0x12D fires at 2000, a DELIBERATE deviation
                  # from fire = onset-15.  The CD-faithful value against the
                  # committed table is 1776 (whisper t0 1791); it is recorded
                  # here so nobody "corrects" this back.
                  #
                  # Why it is wrong: whisper's segment 7 is degenerate -- 243
                  # frames (1791-2034) for a two-mora conjunction, with だ
                  # pinned to the previous segment's END boundary and が
                  # stranded at 2006.  The onset could not be re-measured:
                  # broadband RMS, speech-band ratio, mid/side centre
                  # dominance, syllabic modulation and YIN pitch-tracking
                  # were each validated on labelled windows and each failed
                  # to separate this narrator from the music bed.  So the
                  # instrument is the ear, and
                  # the bracket was built and reviewed: at +67 (1843) review
                  # still heard "the caption starts rendering well before the
                  # first word"; at +224 (2000) "the timing of E is good".
                  # 2000 also lands 6 frames before whisper's stranded が at
                  # 2006, which is where a conjunction introducing the next
                  # clause belongs -- consistent, though not the evidence.
                  #
                  # NOTE this now fires AFTER the knife-sketch cut (1926-1932)
                  # rather than bridging into it.  The bridge was deliberate
                  # when we believed the onset was 1791; the caption belongs
                  # with the voice.
                  #
                  # The clear does NOT move with the fire: 0x122 would
                  # otherwise hold 498 f and outlive its scene (review: "the
                  # prior caption should clear early -- it hung beyond its
                  # scene").  It goes at 1620, measured against two bounds:
                  #
                  # 1592  0x122 finishes TYPING (fires 1496, 96 f to render
                  # its 1058 px -- captured on the pump counter)
                  # 1604  its voice ends (jp_aligned_words seg 5,
                  # 巨大な暴力集団, 1511-1604)
                  # 1620  <- the clear: 28 f after the line is complete,
                  # 16 f after the voice stops
                  # 1794  the gang-shot fade-out begins, so this rides no
                  # transition with 174 f to spare
                  #
                  # the "do not cut the reader off" precedent does NOT apply
                  # here: that case has ROM TEXT continuing past its voice (a
                  # text-only tail still being read).  0x122 is fully rendered
                  # 12 f BEFORE its voice
                  # ends, so nothing is left to read and holding it longer
                  # just reads as a hang -- which is what review saw at 1786.
                  # 1620 (16 f after the VOICE) left the line
                  # readable for 20 frames -- it finishes DRAWING at ~1600,
                  # so a voice-relative clear is a text-suppressing clear for
                  # a line whose render time (104 f) exceeds its voice (93 f).
                  # 1786 was the other extreme (review: hung past its scene).
                  # 0x122's TEXT is a whole sentence spanning TWO transcript
                  # segments -- 巨大な暴力集団 (1511-1604) AND マットギアに
                  # 徹底的な攻撃を加える (1604-1791) -- so its voice ends at
                  # 1791, not 1604.  Clearing against 1604 clears while the
                  # line is still being spoken (review: "it disappears even
                  # before the voice finishes the sentence").  With 0x12D at
                  # 2000 the clear has to cover the speech itself: 1795, just
                  # past the voice and at the shot's fade-out (~1800).
                  (1795, 0x200), (2000, 0x12D),  # daga (see above)
                  (2305, 0x200),                 # 12D sentence end
                  (2419, 0x200), (2425, 0xA09),  # 1992-nen (VO 2434)
                  # (user: "the 1992 caption lingers a bit too
                  # long").  Measured on the built set by pump counter:
                  # the card appeared t2428, the scene faded out t2708,
                  # went black t2716, the desk faded in t2780-2800, and
                  # the card only cleared at t2810 -- it rode the WHOLE
                  # transition, which the discipline forbids ("nothing rides
                  # a scene transition"), and held 6.4 s against the USA A08
                  # card's 3.8 s.  The "desk reveal drop" it is named for is
                  # the USA behaviour; on the JP timeline that same value
                  # lands after the reveal, not at it.  Clear at 2700, 8 f before the fade-out
                  # begins: 4.6 s on screen, 2 s after the VO ends, and
                  # the transition plays clean.
                  (2700, 0x200),                 # year card off, pre-fade
                  # A +6 nudge here (onset+19 where its neighbours sit at
                  # +13) slips this line late.  Unlike 0x12C (clamped to the
                  # gang-bar edge) and 0xA09 (year card held to the desk
                  # reveal), nothing motivates a nudge here, so it goes on the
                  # -15 rule.  Mouth-confirmed onset.
                  (2932, 0x200), (2938, 0x11A),  # watashi da (VO 2953, mouth)
                  (3105, 0x200), (3111, 0x11B),  # shichou-san (VO 3126)
                  (3285, 0x200), (3291, 0x11C),  # otto (VO 3306)
                  (3437, 0x200), (3443, 0x11D),  # nanise (VO 3458)
                  (3683, 0x200), (3689, 0x11E),  # nani! (VO 3704, mouth 3706)
                  (4011, 0x200), (4017, 0x11F),  # maa aseru na (VO 4032)
                  (4296, 0x200),                 # 11F sentence end
                  # kisamaa~: whisper swallowed the shout into the next
                  # segment's blob, so this was parked at a guessed spot that
                  # lands AFTER the shout -- text into silence (user-reported).
                  # Envelope puts the burst at t 4606-4681; fire onset-15.
                  (4585, 0x200), (4591, 0x120),  # kisamaa~ (VO 4606, envelope)
                  # anta wa: this was anchored to the WRONG UTTERANCE.  Its
                  # whisper segment (23) is the same degenerate shape -- 'あ'
                  # parked at the segment start 4275, then a 479-frame gap to
                  # the real dense run from 4754 -- so pinning to segment 24
                  # ("街は今まで通り", t0 4854) would land on the NEXT
                  # sentence.  Centre-channel measurement: this line's
                  # burst rises at t~4764 (quiet 0.02-0.13 through 4763, then
                  # 0.24/0.45/1.00 at 4766/4769/4772) and 街は rises
                  # separately at ~4860.  Fire = 4764-15.  Mispinned, this
                  # line runs ~1.7 s late, far the worst of the three.
                  (4743, 0x200), (4749, 0x121),  # anta wa (VO 4764, measured)
                  (5122, 0x200), (5128, 0x125),  # kore ijou (VO 5137)
                  (5512, 0x200),                 # laugh gap (VO end 5491)
                  (6259, 0x200), (6265, 0x126),  # nani! sarawareta (~6280)
                  # The two GUY lines are lips-anchored, not mora-share
                  # ESTIMATES inside merged whisper segments (those run
                  # ~45-50f early).  With the carve fitted to the word data
                  # (12-anchor series fit, trim 2.64 s), the MEASURED VO
                  # bursts sit ON the lips: 0x127 voice t6462 vs lips 6465;
                  # 0x129 lips 6965-7185 with the uncaptioned shout at 7297
                  # -- so lips-15 == onset-12 here and the same times serve
                  # the silent and voiced builds alike.  0x127 at 6450 = lips
                  # 6465 - 15.
                  (6444, 0x200), (6450, 0x127),  # Jessica?! (VO 6462)
                  (6560, 0x200),                 # 0x127 line end
                  (6599, 0x200), (6605, 0x128),  # osananajimi (VO 6620)
                  (6945, 0x200), (6950, 0x129),  # sessha mo (lips 6965)
                  (7200, 0x200)),                # 0x129 end; shout (7297)
                                                 # stays unscripted
        trim=((7524, 50, 7624),),   # white flash 80f -> 30f
    ),
}




CREDITS_ADDR = DATA + 0xD0000  # relocated copy, in the free p7 tail
                               # (lifted with the other tail blobs so a
                               # growing delta chain cannot reach it)
CREDITS_PTR = 0x018E5E         # the imm32 inside that move.l














def patch_dash_tile(space: bytearray) -> None:
    """Move the 8x8 hyphen tile's bar (0x442D, rows 3-4) to rows 6-7 so it
    centers in the double-height caption line (the typewriter draws it
    top-tile-only via the $141C single-height exception)."""
    off = 0x442D * 64
    blank = b"\xff" * 8            # pen 15 everywhere = transparent row
    if bytes(space[off + 48:off + 64]) != blank * 2:
        return                     # already repainted (idempotent)
    r3, r4 = bytes(space[off + 24:off + 32]), bytes(space[off + 32:off + 40])
    space[off + 48:off + 56] = r3
    space[off + 56:off + 64] = r4
    space[off + 24:off + 40] = blank * 2


def build_j_stock_gfx():
    """Assemble ffightj's two-bank byte-interleaved gfx into the linear
    2 MB space and re-emit as World-style 64-bit-word files (the measured
    delta vs World is exactly 77 SCROLL1 text tiles)."""
    import subprocess, tempfile
    J_BYTE = [
        ("ffj_09.4b", 0x000000), ("ffj_01.4a", 0x000001),
        ("ffj_13.9b", 0x000002), ("ffj_05.9a", 0x000003),
        ("ffj_24.5e", 0x000004), ("ffj_17.5c", 0x000005),
        ("ffj_38.8h", 0x000006), ("ffj_32.8f", 0x000007),
        ("ffj_10.5b", 0x100000), ("ffj_02.5a", 0x100001),
        ("ffj_14.10b", 0x100002), ("ffj_06.10a", 0x100003),
        ("ffj_25.7e", 0x100004), ("ffj_18.7c", 0x100005),
        ("ffj_39.9h", 0x100006), ("ffj_33.9f", 0x100007),
    ]
    space = bytearray(0x200000)
    with tempfile.TemporaryDirectory() as td:
        romset.extract(ROMSET, ("ffightj", "ffight"), [r for r, _ in J_BYTE], td)
        for rom, base in J_BYTE:
            d = Path(td, rom).read_bytes()
            assert len(d) == 0x20000, rom
            space[base:base + 0x100000:8] = d
    patch_dash_tile(space)
    select_window.patch(space)
    out = {}
    for fi, name in enumerate(("c07.c01", "c07.c03", "c07.c05", "c07.c07")):
        off = fi * 2
        d = bytearray(0x80000)
        for wd in range(len(d) // 2):
            d[wd * 2:wd * 2 + 2] = space[wd * 8 + off:wd * 8 + off + 2]
        out[name] = bytes(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the cutscene ROM set for one region.")
    ap.add_argument("conv", type=Path, help="opening conv directory")
    ap.add_argument("stage0", type=Path, help="retimed stage0 program ROMs")
    ap.add_argument("parts_dir", type=Path, help="stage-1 gfx parts")
    ap.add_argument("outdir", type=Path, help="where the set is written")
    ap.add_argument("region", nargs="?", default="us", choices=("us", "jp"))
    ap.add_argument("--ending-conv", default=None,
                    help="reunion conv to embed as the second engine")
    ap.add_argument("--pan-conv", default=None,
                    help="CD-6 farewell conv to embed as the third engine")
    ap.add_argument("--caption-nudge", default=None,
                    help="re-derivation harness: move one caption, '<id>:<delta>'")
    ap.add_argument("--ending-head-delay", type=int, default=None,
                    help="re-derivation harness: override the ending head delay")
    ap.add_argument("--no-ending-anchors", action="store_true",
                    help="re-derivation harness: build without the anchor table")
    ap.add_argument("--no-vbl-detour", action="store_true",
                    help="A/B harness: leave the vblank epilogue unpatched "
                         "(was the NO_VBL_DETOUR environment variable)")
    _a = ap.parse_args()
    OPT.update(ending_conv=_a.ending_conv, pan_conv=_a.pan_conv,
               caption_nudge=_a.caption_nudge,
               ending_head_delay=_a.ending_head_delay,
               no_ending_anchors=_a.no_ending_anchors,
               no_vbl_detour=_a.no_vbl_detour)
    conv, stage0 = _a.conv, _a.stage0
    parts_dir, outdir, _rk = _a.parts_dir, _a.outdir, _a.region
    region = REGIONS[_rk]
    region.setdefault("key", _rk)
    outdir.mkdir(parents=True, exist_ok=True)

    nevents = sum(1 for l in (conv / "script.tsv").read_text().splitlines()
                  if not l.startswith("#") and l.strip())

    script_bytes = bytearray((conv / "script.bin").read_bytes())
    # Rate retime: the CD ran at 59.92 fps, CPS1 runs 59.637.
    # A 1:1 frame mapping made the video drift ~38 frames behind the
    # true-time CPS+ track by the opening's end (Cody/Guy mouths lagging
    # the audio).  Rescale durations so CD frame F lands at script frame
    # round(F * 59.637/59.92); durations stay >= 1 (rounding debt repaid
    # on later events).  Cue and trim times below are CD-frame inputs and
    # get the same scale; caption times are audio-clock and do NOT.
    RATE = 59.637 / 59.92
    cum_o = cum_n = 0
    for i in range(len(script_bytes) // 16):
        dur = struct.unpack(">H", script_bytes[i * 16 + 10:i * 16 + 12])[0]
        cum_o += dur
        nd = max(1, round(cum_o * RATE) - cum_n)
        cum_n += nd
        script_bytes[i * 16 + 10:i * 16 + 12] = be16(nd)
    # ---- OPENING ENTRY FADE (US only).  The CD fades the Metro
    # City map in from black over ~68 frames, but the US shot table's row 0
    # starts at capture f1600 -- 80% up the ramp -- so the intro's first
    # image POPPED in.  Extending the window head would shift the whole US
    # timeline against the audio track and captions; instead the engine
    # fade (tick) replays the ramp over the conv's own first
    # events, whose boundaries already fall every 3-18 frames.  JP has no
    # key: its window starts AT the fade start and the ramp is real conv
    # content -- stamping it too would double-fade.
    ofade = region.get("opening_entry_fade")
    if ofade:
        t, nst = 0, 0
        for i in range(len(script_bytes) // 16):
            dur = struct.unpack(">H", script_bytes[i * 16 + 10:i * 16 + 12])[0]
            if t >= ofade:
                break
            lvl = min(15, max(1, round(15 * (t + dur / 2) / ofade)))
            if lvl < 15:
                w0 = struct.unpack(">H", script_bytes[i * 16:i * 16 + 2])[0]
                script_bytes[i * 16:i * 16 + 2] = be16((w0 & 0x0FFF)
                                                       | ((15 - lvl) << 12))
                nst += 1
            t += dur
        print(f"  opening entry fade: {nst} steps across the first {t} frames")
    # Cue annotations applied HERE, not as a separate manual step --
    # dropped the music by rebuilding from a freshly-converted script.bin.
    # Arcade-audio soundtrack (corrected by the user and
    # consistent with every measurement): 0x52 = the story MUSIC (51 s
    # piece, restarted at its boundaries to span the 147 s script);
    # 0x35 = the phone RING, an SE that loops until answered and LAYERS
    # over the music (the WAV shows music+ring RMS stacking);
    # 0x36 = the CLICK that answers the phone and stops the ring.
    # Stock attract order 0x35 -> 0x52 -> 0x36 is ring, music enters,
    # click.  Here: music from the map; ring as the sketch cuts to the
    # desk; click ~2.7 s later as Haggar answers.
    for tcue, val in region["cues"]:
        tcue = round(tcue * RATE)
        # index the events once: (start_t, idx, pal_block)
        evs, t = [], 0
        for i in range(len(script_bytes) // 16):
            pb, dur = struct.unpack(
                ">HH", script_bytes[i * 16 + 2:i * 16 + 4] +
                script_bytes[i * 16 + 10:i * 16 + 12])
            evs.append((t, i, pb))
            t += dur
        home = max(i for (st, i, _) in evs if st <= tcue)
        # Stock behaviour: the cue rides the event containing tcue.  A fresh
        # conversion can merge two cue times into one longer event (the July
        # segmentation kept them apart), so on collision fall back to the
        # nearest event whose slot is free rather than asserting out --
        # and say by how much the cue moved, for the listening gate.
        cand = [(0 if i == home else abs(st - tcue), i)
                for (st, i, pb) in evs if (pb >> 8) in (0, val)]
        assert cand, f"no free cue slot anywhere for cue {val:#x}"
        dev, pick = min(cand)
        if pick != home:
            print(f"  cue {val:#04x}: slot at t={tcue} occupied; "
                  f"moved to event {pick} ({dev} frames away)")
        off = pick * 16
        bi, pb, doff, dcnt, dur, sx, sy = struct.unpack(
            ">HHIHHHH", script_bytes[off:off + 16])
        script_bytes[off:off + 16] = struct.pack(
            ">HHIHHHH", bi, (pb & 0xFF) | (val << 8),
            doff, dcnt, dur, sx, sy)
    # timeline trims: (event_t, cut, extend_t) -- shorten one event, grow
    # another (typically the following black) so total length is constant
    for et, cut, ext in region.get("trim", ()):
        et, ext = round(et * RATE), round(ext * RATE)
        offs = {}
        t = 0
        for r in range(0, len(script_bytes), 16):
            offs[t] = r
            t += struct.unpack(">H", script_bytes[r + 10:r + 12])[0]
        for tt, delta in ((et, -cut), (ext, cut)):
            # trim times were tuned against the July segmentation; a fresh
            # conversion rarely has an event starting at that exact frame,
            # so use the nearest event start (and say how far it moved)
            def dur_at(key):
                return struct.unpack(
                    ">H", script_bytes[offs[key] + 10:offs[key] + 12])[0]
            # eligible = event long enough to absorb the delta (a cut must
            # leave >=1 frame); pick the eligible event nearest tt
            elig = [k for k in offs if dur_at(k) + delta > 0]
            assert elig, f"no event can absorb trim delta {delta}"
            near = min(elig, key=lambda k: abs(k - tt))
            if near != tt:
                print(f"  trim: t={tt} unusable (missing or too short); "
                      f"using t={near} ({abs(near - tt)} frames away)")
            r = offs[near]
            script_bytes[r + 10:r + 12] = be16(dur_at(near) + delta)
    # ---- FADE STEPS AS PALETTE BLOCKS.  The engine copies a block verbatim
    # when the record's block index changes and never scales it at run time
    # (a 512-word read-modify-write does not fit the vblank next to a big map
    # write); every (block, fade code) the script uses becomes its own block
    # here.  Done BEFORE the ending engine is laid out because the ending's
    # blocks land right after these.
    script_bytes, open_pal, _ = vbsched.materialize_fades(
        bytes(script_bytes), (conv / "palblocks.bin").read_bytes(), "opening")
    script_bytes = bytearray(script_bytes)

    # ---- optional ENDING instance (ARC-1 <- CD-1): a second engine, driven
    # from engine1's vblank chain, rendering the CD reunion art over the
    # arcade ending's seg A.  Data rides the same p7 regions, placed
    # additively after the opening's blobs.
    import os
    ending_conv = OPT["ending_conv"]
    ENGINE2 = 0x0D7000
    e2 = None
    if ending_conv:
        ec = Path(ending_conv)
        nev2 = sum(1 for l in (ec / "script.tsv").read_text().splitlines()
                   if not l.startswith("#") and l.strip())
        script_e = SCRIPT + nevents * 16
        if script_e + nev2 * 16 > PALBLOCKS:
            # the JP ending (reunion + the layered Guy/Cody scene) is 440
            # events and does not fit behind the opening's 1904 in the fixed
            # SCRIPT window.  The script is position-independent data -- put
            # it in the free p7 tail instead, the same escape the CD-6
            # engine's blobs already use.
            script_e = TAIL_SCRIPT
        pal_e = PALBLOCKS + len(open_pal)
        base_e = BASEMAPS + (conv / "basemaps.bin").stat().st_size
        if base_e + (ec / "basemaps.bin").stat().st_size > DELTAS:
            # CD-native ending convs carry more shots than the fixed window
            # holds; the basemaps are position-independent like the script,
            # so use the same free-tail escape (a NAMED tail address above
            # the delta chain; the literal 0x180000 sits inside the opening's
            # own deltas).
            base_e = TAIL_BASEMAP
        # (+SPILL_SLACK: vbsched may grow a delta blob by its spill lists)
        delta_e = DELTAS + (conv / "deltas.bin").stat().st_size + vbsched.SPILL_SLACK
        caps_e = delta_e + (ec / "deltas.bin").stat().st_size + vbsched.SPILL_SLACK
        if region.get("ending_captions_rom"):
            ecaps = build_ending_captions_rom(
                sys.argv[5] if len(sys.argv) > 5 else "us",
                region["ending_captions_rom"])
        elif region.get("ending_captions"):
            ecaps = build_ending_captions(region["ending_captions"])
        else:
            ecaps = None
        # layered Guy/Cody scene (JP): sprite tables ride after the captions
        gcspr = None
        gcblob = None
        if (ec / "gcspr.bin").exists():
            import struct as _st
            gcblob = (ec / "gcspr.bin").read_bytes()
            man2 = json.loads((ec / "manifest.json").read_text())
            gc_p = caps_e + (len(ecaps) if ecaps else 0)
            h = _st.unpack(">4H", gcblob[:8])
            gcspr = dict(celoff=gc_p + h[0], celtab=gc_p + h[1],
                         sprevt=gc_p + h[2], opals=gc_p + h[3],
                         first_evt=man2["gc_first_evt"],
                         n_evt=man2["gc_n_evt"], maxc=man2["gc_max_cells"],
                         base=gc_p)
        eng2, e2_init, _, _, e2_eovr, e2_blk, _, _, _, _ = build_engine(
            nev2, init_cue=region["ending_cue"], lead_in=1, magic=0xCAFD,
            script_base=script_e, palblocks_base=pal_e, deltas_base=delta_e,
            arm_restore=(0x1B7C, 0x0001, 0x008D),  # 0x186BA's displaced insn
            ending_mode=True,
            gcspr=gcspr,
            ecaps_base=caps_e if ecaps else None)
        assert len(eng2) <= 0x1000, f"engine2 {len(eng2):#x} overflows its slot"
        e2 = dict(eng=eng2, init=e2_init, eovr=e2_eovr, blk=e2_blk, nev=nev2,
                  script=script_e, pal=pal_e, base=base_e, delta=delta_e,
                  caps=caps_e, ecaps=ecaps, gcspr=gcspr, gcblob=gcblob)
        print(f"ending engine {len(eng2)} bytes at {ENGINE2:#x}, "
              f"INIT +{e2_init:#x}, EOVR +{e2_eovr:#x}, {nev2} events, "
              f"{len(ecaps) if ecaps else 0} caption bytes")

    # ---- optional PAN instance (ARC-7 <- CD-6, the Cody/Jessica farewell).
    # Same shape as ENGINE2, but its art is a tall panorama scrolled by the
    # script's sy (convert_pan.py) and it wears the letterbox mask.  Its
    # data goes in the free tail after the ending blobs -- the fixed
    # SCRIPT/PALBLOCKS/BASEMAPS regions are full.
    # Default to the in-tree conversion.  This was env-only, and a build
    # that simply forgot to set it produced a set with NO CD-6 scene at all
    # -- silently, because the stock farewell still runs and still hands
    # back correctly.  A phase trace of such a build looks almost right.
    pan_conv = OPT["pan_conv"]
    if not pan_conv:
        default_pan = conv.parent / "e6conv"
        if (default_pan / "script.bin").exists():
            pan_conv = str(default_pan)
        else:
            # Make the omission LOUD.  the vmsweep sets were
            # hand-built without --pan-conv and this default does not
            # exist under a flat build dir, so both sets shipped without
            # ENGINE3 and nothing said so.
            print("WARNING: no pan conv (--pan-conv unset, no "
                  f"{default_pan}) -- CD-6 farewell OMITTED, the stock "
                  "arcade scene will play")
    ENGINE3 = 0x0D8000
    e3 = None
    if pan_conv and e2:
        pc = Path(pan_conv)
        nev3 = (pc / "script.bin").stat().st_size // 16
        pan_script, pan_pal, _ = vbsched.materialize_fades(
            (pc / "script.bin").read_bytes(), (pc / "palblocks.bin").read_bytes(),
            "pan")
        # start AFTER every ENGINE2 blob.  Ending at the caption blob is not
        # enough: the JP Guy/Cody scene adds E-GCSPR after it, and the pan
        # blobs would then land 12 bytes into the sprite table and silently
        # overwrite it.
        tail = e2["caps"] + (len(e2["ecaps"]) if e2["ecaps"] else 0)
        if e2.get("gcspr") and e2.get("gcblob"):
            tail = max(tail, e2["gcspr"]["base"] + len(e2["gcblob"]))
        script_p = (tail + 15) & ~15
        pal_p = script_p + nev3 * 16
        delta_p = pal_p + len(pan_pal)
        patch_p = delta_p + (pc / "deltas.bin").stat().st_size + vbsched.SPILL_SLACK
        has_patch = (pc / "patches.bin").exists()
        objpal_p = patch_p + ((pc / "patches.bin").stat().st_size if has_patch else 0)
        # JP carries its farewell text as ROM records (kana); USA authors
        # Latin lines.  Same blob format either way.
        if region.get("farewell_captions_rom"):
            fcaps = build_ending_captions_rom(region["key"],
                                              region["farewell_captions_rom"])
        elif region.get("farewell_captions"):
            fcaps = build_ending_captions(region["farewell_captions"])
        else:
            fcaps = None
        fcaps_p = objpal_p + ((pc / "objpals.bin").stat().st_size
                              if has_patch else 0)
        # letterbox tile: a single-pen tile in the pan art whose pen is
        # neither 15 nor 0 -- BOTH read as transparent for sprites (a pen-0
        # tile emitted a correct-looking sprite list that drew nothing).
        # Palette does not matter: the engine forces OBJ palette 0 black.
        ptiles = (pc / "tiles.bin").read_bytes()
        pman = json.loads((pc / "manifest.json").read_text())
        lb = None
        for ti in range(len(ptiles) // 128):
            t = ptiles[ti * 128:(ti + 1) * 128]
            if (len(set(t)) == 1 and (t[0] >> 4) == (t[0] & 15)
                    and (t[0] >> 4) not in (0, 15)):
                lb = pman["base_code"] + ti
                break
        assert lb is not None, "pan art has no single-pen letterbox tile"
        print(f"  letterbox tile {lb:#06x}")
        eng3, e3_init, _, _, e3_eovr, e3_blk, _, _, _, _ = build_engine(
            nev3, init_cue=0, lead_in=1, magic=0xCAFC,
            script_base=script_p, palblocks_base=pal_p, deltas_base=delta_p,
            arm_restore=(0x1B7C, 0x0001, 0x008D),   # 0x18AD2's displaced insn
            ending_mode=True, letterbox=True, lb_tile=lb, lb_open_at=1004,
            patch_base=patch_p if has_patch else None,
            objpal_base=objpal_p if has_patch else None,
            objpal_words=((pc / "objpals.bin").stat().st_size // 2
                          if has_patch else 12 * 16),
            ecaps_base=fcaps_p if fcaps else None,
            clear_cell=(pman["base_code"] << 16), blk_skip=0x52,
            # seg G hands to outer 0x0E (the gag cards) with layer 0x1380 --
            # phase map rel 5388.  The default 0x08 is seg A's boundary and
            # sent the farewell back into the credits, looping forever.
            handback_outer=0x000E, handback_layer=0x1380)
        assert len(eng3) <= 0x1000, f"engine3 {len(eng3):#x} overflows its slot"
        e3 = dict(eng=eng3, init=e3_init, eovr=e3_eovr, blk=e3_blk, nev=nev3,
                  script=script_p, pal=pal_p, delta=delta_p, conv=pc,
                  patch=patch_p, objpal=objpal_p,
                  fcaps=fcaps_p, fcapsblob=fcaps,
                  script_bytes=pan_script, pal_bytes=pan_pal,
                  clear_code=pman["base_code"])
        print(f"pan engine {len(eng3)} bytes at {ENGINE3:#x}, "
              f"INIT +{e3_init:#x}, {nev3} events, data at {script_p:#x}")

    chain = []
    if e2:
        chain.append((0xCAFD, ENGINE2 + e2["eovr"]))
    if e3:
        chain.append((0xCAFC, ENGINE3 + e3["eovr"]))
    engine, init_off, ovr_off, blink_off, _, _, objhook_off, palhook_off, conthook_off, boothook_off = build_engine(nevents,
        init_cue=region["init_cue"], lead_in=region["lead_in"],
        sprite_slide=region.get("sprite_slide", False),
        jp_title=region.get("jp_title", False),
        ovr_chain=chain or None)
    print(f"engine {len(engine)} bytes, INIT at +{init_off:#x}, {nevents} events")
    ex_title_entry = ENGINE + len(engine)
    engine += title_ex.init_code()

    # engine.lst is a disassembly listing for reading the emitted code.  It
    # is a development aid, not part of any ROM, so capstone is optional:
    # a reconstruction should not fail for want of a disassembler.
    try:
        from capstone import Cs, CS_ARCH_M68K, CS_MODE_M68K_000
        md = Cs(CS_ARCH_M68K, CS_MODE_M68K_000)
        listing = [f"{i.address:06X}  {i.mnemonic:9s} {i.op_str}"
                   for i in md.disasm(engine, ENGINE)]
        (outdir / "engine.lst").write_text("\n".join(listing) + "\n")
    except ImportError:
        print("  (no capstone: skipping the engine disassembly listing)")
    import engine as _eng
    _lbl = {"ENGINE1": {"base": ENGINE, "len": len(engine),
                        "labels": _eng.LABELS[-1]}}
    if e2:
        _lbl["ENGINE2"] = {"base": ENGINE2, "len": len(e2["eng"]),
                           "labels": _eng.LABELS[0]}
    if e3:
        _lbl["ENGINE3"] = {"base": ENGINE3, "len": len(e3["eng"]),
                           "labels": _eng.LABELS[1]}
    (outdir / "engine_labels.json").write_text(json.dumps(_lbl, indent=1))

    # ---- data ROM
    offtab = b""
    for cy in range(14):
        for cx in range(20):
            row, col = ROW0 + cy, COL0 + cx
            idx = (row & 0x0F) + ((col & 0x3F) << 4) + ((row & 0x30) << 6)
            offtab += be16(idx * 4)
    data = bytearray(b"\xFF" * 0x100000)   # 1 MB p7

    placed = []      # (addr, len, name) of everything written, for overlap

    def place(addr, blob, name):
        """Write a blob into the data ROM, refusing to write over another.

        Checking only the ROM end would let a blob that lands inside an
        already-placed one be written silently -- e.g. the pan blobs 12 bytes
        into the Guy/Cody sprite table (see the ENGINE3 tail comment), or the
        capture-free JP chain running past the script escape.  A collision is
        always a layout bug, so it fails here, naming both blobs and the
        overlap.
        """
        off = addr - DATA
        if off < 0 or off + len(blob) > len(data):
            raise SystemExit(
                f"LAYOUT: {name} at {addr:#08X} +{len(blob)} falls outside "
                f"the p7 data ROM {DATA:#08X}..{DATA + len(data):#08X}")
        for a2, n2, nm2 in placed:
            if addr < a2 + n2 and a2 < addr + len(blob):
                ov = min(addr + len(blob), a2 + n2) - max(addr, a2)
                raise SystemExit(
                    f"LAYOUT: {name} at {addr:#08X} +{len(blob)} OVERLAPS "
                    f"{nm2} at {a2:#08X} +{n2} by {ov} bytes -- raise the "
                    f"region sizes in the layout block at the top of this "
                    f"file")
        placed.append((addr, len(blob), name))
        data[off:off + len(blob)] = blob
        print(f"  {name:10s} @{addr:06X} +{len(blob)}")

    def region_check(name, base, used, limit):
        """Fail loudly, and by how much, when a fixed window is exceeded."""
        if base + used > limit:
            raise SystemExit(
                f"LAYOUT: {name} needs {used} bytes from {base:#08X} but its "
                f"region ends at {limit:#08X} ({limit - base} bytes available)"
                f" -- SHORT BY {base + used - limit} bytes.  Raise {name} in "
                f"the layout block at the top of this file.")

    place(OFFTAB, offtab, "OFFTAB")
    margtab = b""
    for col in (0x24, 0x25, 0x3A, 0x3B):
        for cy in range(14):
            row = 0x11 + cy
            idx = (row & 0x0F) + (col << 4) + ((row & 0x30) << 6)
            margtab += struct.pack(">H", idx * 4)
    place(MARGTAB, margtab, "MARGTAB")
    # stable-sort by t: the pump walks the table sequentially, so an
    # out-of-order entry silently defers to the NEXT fire (the "text on the
    # CAPCOM card" + eaten-first-glyph symptom); same-t pairs keep literal
    # order (clear before line)
    caps = list(region["captions"])
    if OPT["caption_nudge"]:
        # A/B knob for ONE line's timing, for review candidates.  Format
        # "0x12D:+45": shifts that ROM id's fire AND the clear that belongs
        # with it (the entry 6 frames before it, per the schedule's
        # "clears at t-6" rule) -- nothing else moves, so the -28 correction
        # and the year-card clear are untouched.
        spec = OPT["caption_nudge"]
        rid, delta = spec.split(":")
        rid, delta = int(rid, 16), int(delta)
        fire = [t for t, i in caps if i == rid]
        if not fire:
            raise SystemExit(f"--caption-nudge: id {rid:#x} not scheduled")
        f0 = fire[0]
        out = []
        for t, i in caps:
            if i == rid:
                out.append((t + delta, i))
            elif i == 0x200 and t == f0 - 6:
                out.append((t + delta, i))
            else:
                out.append((t, i))
        caps = out
        print(f"  caption nudge: {rid:#05x} {f0} -> {f0+delta} "
              f"(clear {f0-6} -> {f0-6+delta})")
    captab = b"".join(be16(t) + be16(i)
                      for t, i in sorted(caps, key=lambda e: e[0]))
    captab += be16(0xFFFF) + be16(0)
    assert len(captab) <= CAPPAL - CAPTAB, "caption table overflows gap"
    place(CAPTAB, captab, "CAPTAB")
    # caption palettes at full fade (live-dumped from stock attract):
    # pal 6 then pals 14+15 contiguous -- engine re-asserts every vblank
    pals = {}
    for ln in (TRACK / "data/captions/scroll1_palettes.txt"
               ).read_text().splitlines():
        parts_ = ln.split()
        pals[int(parts_[1])] = b"".join(be16(int(w, 16)) for w in parts_[2:])
    place(CAPPAL, pals[6] + pals[14] + pals[15], "CAPPAL")
    # (the opening script's retime/fade/cue/trim edits now happen BEFORE the
    # ending engine's layout, up in the ENGINE2 block: the materialised
    # fade-step palette blocks change where the ending's own blocks land)
    # The OPENING's own blobs get the same named check, so an opening that
    # fills a whole region is caught here -- not surfaced as the ENDING's
    # assert failing on the wrong blob.
    region_check("SCRIPT", SCRIPT, len(script_bytes), PALBLOCKS)
    region_check("PALBLOCKS", PALBLOCKS, len(open_pal), BASEMAPS)
    region_check("BASEMAPS", BASEMAPS,
                 (conv / "basemaps.bin").stat().st_size, DELTAS)
    # ---- VBLANK SCHEDULE (vbsched.py): drop duplicate/no-op cells, mark
    # the >=64-cell records "late" (bit 13: the catch-up timing), and
    # spread not-yet-visible cells over later vblanks so every vblank's map
    # work fits the blanking budget.  Fails loudly when it cannot.
    script_bytes, open_deltas, _ = vbsched.schedule(
        bytes(script_bytes), (conv / "deltas.bin").read_bytes(),
        clear_code=0x4000, late_rule=True, fixed_cyc=2_600, name="opening")
    place(SCRIPT, bytes(script_bytes), "SCRIPT")
    place(PALBLOCKS, open_pal, "PALBLOCKS")
    place(BASEMAPS, (conv / "basemaps.bin").read_bytes(), "BASEMAPS")
    place(DELTAS, open_deltas, "DELTAS")
    if e2:
        # ending blobs, additive after the opening; same clock retime
        esb = bytearray((ec / "script.bin").read_bytes())
        # Piecewise retime against the VO word clock (ending_anchors:
        # capture-cum-frame -> CPS frame).  The E6 attract playback ran
        # the scene's talk sections at distorted pacing (needed shifts
        # measured 340/192/144 across the three sections), so a uniform
        # RATE cannot lip-sync it -- each flap section is pinned to its
        # voiced span instead, and the silent holds absorb the slack.
        anchors = region.get("ending_anchors")
        if OPT["ending_head_delay"] is not None:
            # The "one offset, no warping" candidate.  Review: "why is
            # there any warping at all?"  The disc's animation and its dialogue
            # were authored together, so a faithful scene plus a correctly
            # placed audio start should need no piecewise map -- and measured,
            # it does not: a single head delay puts 98.6% of all voiced
            # duration inside a mouth block (plateau D=249..256), against 93.8%
            # for the shipped piecewise table.  Everything after the head runs
            # at the plain rate conversion.
            D = OPT["ending_head_delay"]
            end = 1557
            anchors = ((0, D), (end, D + round(end * RATE)))
            print(f"  ending retime: HEAD DELAY {D}, uniform after "
                  f"(re-derivation build)")
        elif OPT["no_ending_anchors"]:
            # The re-anchoring METHOD: the table must be derived against a
            # NO-ANCHOR build of the CURRENT conv (never against a capture,
            # and never by editing an existing table), because what it pins
            # is where THIS conv's mouth blocks actually land.
            # Measure with probes/ending_metrics.lua + tools/measure_ending_v2.py,
            # then pin each block to its VO span.
            print("  ending retime: ANCHORS DISABLED (re-derivation build)")
            anchors = None

        def emap(t: float) -> int:
            if not anchors:
                return round(t * RATE)
            for (s0, d0), (s1, d1) in zip(anchors, anchors[1:]):
                if t <= s1:
                    return round(d0 + (t - s0) * (d1 - d0) / (s1 - s0))
            s1, d1 = anchors[-1]
            return round(d1 + (t - s1) * RATE)

        # ---- SPREAD THE HEAD DELAY over presplit's lead events.
        # The anchors' first pin is (0, D) -- a HEAD DELAY, D CPS frames of
        # the first painted image before capture frame 0's content is due
        # (us 228, jp 256).  A plain mapping charges that whole step to the
        # FIRST event, because cum_n starts at 0 and emap(1) is already D+1.
        # That would leave retime.py's one-frame lead slices at ~1 frame each
        # behind one 229/257-frame monster, the entry fade with no boundary
        # inside its 64-frame window, and the stamper below emitting ZERO
        # steps in both regions -- no fade at all.
        #
        # This is an anchor effect, not a fade-code one: a single head step
        # at t=0 replaces a piecewise head (a ~5.5x STRETCH across many
        # capture frames, which spread itself over the slices for free).
        #
        # Fix: the lead run shares the head delay evenly.  Durations only --
        # the event COUNT is untouched, so gc_first_evt and every other event
        # index stay put, and the cumulative at the end of the run is exactly
        # emap(nlead), so nothing downstream of the head moves by a single
        # frame.
        nlead = 0
        try:
            nlead = int(json.loads((ec / "manifest.json").read_text())
                        .get("entry_slices", 0))
        except Exception:
            nlead = 0
        nlead = min(nlead, len(esb) // 16)
        head_total = emap(nlead) if nlead else 0
        cum_o = cum_n = 0
        for i in range(len(esb) // 16):
            dur = struct.unpack(">H", esb[i * 16 + 10:i * 16 + 12])[0]
            cum_o += dur
            if i < nlead:
                nd = max(1, round(head_total * (i + 1) / nlead) - cum_n)
            else:
                nd = max(1, emap(cum_o) - cum_n)
            cum_n += nd
            esb[i * 16 + 10:i * 16 + 12] = be16(nd)
        print(f"  ending retime: {cum_o} capture frames -> {cum_n} CPS frames"
              f" ({'anchored' if anchors else 'uniform'})"
              + (f"; head delay {head_total} spread over {nlead} lead events "
                 f"({head_total / nlead:.1f}f each)" if nlead else ""))
        # ---- ENTRY FADE.  The CD fades the reunion in from black over ~64
        # frames (VM sweep f72-136); both esnaps exclude the ramp (the cd1
        # stepped-fade rule), so the built scene would POP 0->59 in <=3
        # frames.  Stamp per-event fade codes across the first N CPS frames.
        # Stamped POST-retime because the anchors stretch the entry ~9.5x --
        # capture-side codes would make a 10-second fade.  retime.py must have
        # sliced the paint event so boundaries exist at fade cadence.
        efade = region.get("ending_entry_fade")
        if efade:
            t, nst = 0, 0
            for i in range(len(esb) // 16):
                dur = struct.unpack(">H", esb[i * 16 + 10:i * 16 + 12])[0]
                if t >= efade:
                    break
                lvl = min(15, max(1, round(15 * (t + dur / 2) / efade)))
                if lvl < 15:
                    w0 = struct.unpack(">H", esb[i * 16:i * 16 + 2])[0]
                    esb[i * 16:i * 16 + 2] = be16((w0 & 0x0FFF)
                                                  | ((15 - lvl) << 12))
                    nst += 1
                t += dur
            if nst < 4:
                print(f"  WARNING: entry fade got only {nst} step(s) -- "
                      "run retime.py on the ending conv first")
            print(f"  entry fade: {nst} steps across the first {t} frames")
        esb, epal, _ = vbsched.materialize_fades(
            bytes(esb), (ec / "palblocks.bin").read_bytes(), "ending")
        esb = bytearray(esb)
        ebase = (ec / "basemaps.bin").read_bytes()
        # per-vblank fixed work of the ending override beyond the tick:
        # caption palettes, margin palette, subtitle records, and (JP) the
        # Guy/Cody walker; event 0 also draws the margin bars once
        _gc = e2.get("gcspr")
        esb, edelta, _ = vbsched.schedule(
            bytes(esb), (ec / "deltas.bin").read_bytes(),
            clear_code=0x4000, late_rule=False, fixed_cyc=2_500,
            # event 0 also draws the margin bars + (JP) uploads the walker's
            # OBJ palettes; inside the Guy/Cody scene the walker itself
            extra_cyc=lambda ev, loading: (5_500 if ev == 0 and loading else 0) + (
                7_000 if _gc and _gc["first_evt"] <= ev < _gc["first_evt"] + _gc["n_evt"]
                else 0),
            dark_frames=2, margin_bars=True, palblocks=epal,
            tiles=(ec / "tiles.bin").read_bytes(), name="ending")
        esb = bytearray(esb)
        region_check("PALBLOCKS", PALBLOCKS,
                     (e2["pal"] - PALBLOCKS) + len(epal), BASEMAPS)
        if e2["base"] < TAIL_BASEMAP:
            region_check("BASEMAPS", BASEMAPS,
                         (e2["base"] - BASEMAPS) + len(ebase), DELTAS)
        if e2["script"] < TAIL_SCRIPT:
            region_check("SCRIPT", SCRIPT,
                         (e2["script"] - SCRIPT) + len(esb), PALBLOCKS)
        place(e2["script"], bytes(esb), "E-SCRIPT")
        place(e2["pal"], epal, "E-PALBLK")
        place(e2["base"], ebase, "E-BASEMAP")
        place(e2["delta"], edelta, "E-DELTAS")
        if e2["ecaps"]:
            place(e2["caps"], e2["ecaps"], "E-CAPS")
        if e2.get("gcblob"):
            place(e2["gcspr"]["base"], e2["gcblob"], "E-GCSPR")
    if e3:
        pc = e3["conv"]
        # fixed per-vblank work of the pan override: letterbox bars, the
        # patch walker, the slot park, captions, caption palettes; event 0
        # also uploads the OBJ palettes
        p_script, p_deltas, _ = vbsched.schedule(
            e3["script_bytes"], (pc / "deltas.bin").read_bytes(),
            clear_code=e3["clear_code"], late_rule=False, fixed_cyc=13_000,
            extra_cyc=lambda ev, loading: 4_500 if ev == 0 and loading else 0,
            dark_frames=3, name="pan")
        place(e3["script"], p_script, "P-SCRIPT")
        place(e3["pal"], e3["pal_bytes"], "P-PALBLK")
        place(e3["delta"], p_deltas, "P-DELTAS")
        if (pc / "patches.bin").exists():
            place(e3["patch"], (pc / "patches.bin").read_bytes(), "P-PATCH")
            # OBJ palettes pre-masked to level 15: the engine's unfaded
            # upload is then a verbatim movem copy (its faded path still
            # re-masks per word)
            opw = struct.unpack(f">{(pc / 'objpals.bin').stat().st_size // 2}H",
                                (pc / "objpals.bin").read_bytes())
            place(e3["objpal"], struct.pack(f">{len(opw)}H",
                                            *((w & 0x0FFF) | 0xF000 for w in opw)),
                  "P-OBJPAL")
            pend = e3["objpal"] + (pc / "objpals.bin").stat().st_size
        else:
            pend = e3["delta"] + (pc / "deltas.bin").stat().st_size + vbsched.SPILL_SLACK
        if e3["fcapsblob"]:
            place(e3["fcaps"], e3["fcapsblob"], "P-FCAPS")
            pend = e3["fcaps"] + len(e3["fcapsblob"])
        assert pend <= DATA + 0x100000, f"pan data overruns p7 at {pend:#x}"
    # ---- ENDING CREDITS: relocated + extended (see build_credits)
    _s0a = (stage0 / "ff_36.11f").read_bytes()
    _s0b = (stage0 / "ff_42.11h").read_bytes()

    def _stage0_read(addr, n):
        return bytes((_s0a if (addr + i) % 2 == 0 else _s0b)[(addr + i) // 2]
                     for i in range(n))

    credits_blob = build_credits(_stage0_read, region["credit_header"])
    _co = CREDITS_ADDR - DATA
    assert all(b == 0xFF for b in data[_co:_co + len(credits_blob)]), \
        f"CREDITS at {CREDITS_ADDR:#x} would overwrite a placed blob"
    place(CREDITS_ADDR, credits_blob, "CREDITS")
    place(title_ex.DATA, title_ex.packed()[1], "EX_FRAMES")

    # 16_WORD_SWAP file layout
    p7 = bytearray(len(data))
    for i in range(0, len(data), 2):
        p7[i] = data[i + 1]
        p7[i + 1] = data[i]
    (outdir / region["p7_name"]).write_bytes(p7)

    # ---- gfx parts: art tiles over 4 MB of appended space (6 MB region).
    # Beyond the art: transparent fill (0xFF planar = pen 15 everywhere).
    chunky = (conv / "tiles.bin").read_bytes()
    if e2:
        # ending tiles ride after the opening's; the ending conversion must
        # have been run with --base-code 0x4000 + <opening tile count>
        em = json.loads((ec / "manifest.json").read_text())
        want = 0x4000 + len(chunky) // 128
        got = em.get("base_code", 0x4000)
        assert got == want, (
            f"ending conv base_code {got:#x} != required {want:#x}; "
            f"reconvert with --base-code {want:#x}")
        chunky = chunky + (ec / "tiles.bin").read_bytes()
    if e3:
        pm = json.loads((e3["conv"] / "manifest.json").read_text())
        want3 = 0x4000 + len(chunky) // 128
        got3 = pm.get("base_code", 0x4000)
        assert got3 == want3, (
            f"pan conv base_code {got3:#x} != required {want3:#x}; "
            f"reconvert with --base-code {want3:#x}")
        chunky = chunky + (e3["conv"] / "tiles.bin").read_bytes()
    planar = chunky_to_planar(chunky)
    assert len(planar) <= 0x400000, f"art {len(planar):#x} exceeds 4 MB appended"
    space = bytearray(b"\xff" * 0x400000)
    space[0:len(planar)] = planar
    ex_start = title_ex.GFX_OFFSET - 0x200000
    ex_gfx = title_ex.graphics()
    assert len(planar) <= ex_start, "cutscene art overlaps EX title tiles"
    assert space[ex_start:ex_start + len(ex_gfx)] == b"\xff" * len(ex_gfx)
    space[ex_start:ex_start + len(ex_gfx)] = ex_gfx
    # The mapper-neutral continue-screen fill (see the $2F12 patch below)
    # relies on code $2F12 decoding transparent on EVERY layer.  SCR3 reads
    # its 512-byte 32x32 tile at code*512 = gfx 0x5E2400, which is inside the
    # appended region (space offset 0x3E2400) -- the art must stop short of
    # it, or the packer has to keep it blank.  Fail loudly rather than let
    # a bigger art build turn the continue screen into that tile.
    SCR3_2F12 = 0x5E2400 - 0x200000
    assert space[SCR3_2F12:SCR3_2F12 + 0x200] == b"\xff" * 0x200, (
        f"art reaches gfx 0x5E2400 (space {SCR3_2F12:#x}); code $2F12 is no "
        f"longer transparent on SCR3 -- move the art or pick another fill code")
    gfx_parts = {}
    prefix = region["p7_name"].split(".")[0]   # c07us01 / c07js01
    for half, files in enumerate((GFX_FILES, GFX_FILES6)):
        for fi, name in enumerate(files):
            name = prefix + "." + name.split(".")[1]
            off = fi * 2
            d = bytearray(0x80000)
            base = half * 0x200000
            for wd in range(len(d) // 2):
                d[wd * 2:wd * 2 + 2] = space[base + wd * 8 + off:
                                             base + wd * 8 + off + 2]
            gfx_parts[name] = bytes(d)
            (outdir / name).write_bytes(d)

    # ---- program ROMs: stage0 p1/p2 + detours; engine into p5
    import subprocess, tempfile
    with tempfile.TemporaryDirectory() as td:
        romset.extract(ROMSET, ("ffight",), ["ff-32m.8h", "ff_37.12f"], td)
        p4_arc, p4_member = region["p4"]
        # the clone's own archive first, then a merged parent set
        romset.extract(ROMSET, (Path(p4_arc).stem, "ffight"), [p4_member], td)
        p1 = bytearray((stage0 / "ff_36.11f").read_bytes())
        p2 = bytearray((stage0 / "ff_42.11h").read_bytes())
        p3 = bytearray(Path(td, "ff_37.12f").read_bytes())
        p4 = bytearray(Path(td, p4_member).read_bytes())
        # caption reposition patches (row-only cell edits in the string
        # tables; all low bytes = p4): center narration in the letterbox,
        # pitch-2 line spacing everywhere, A08 title below the image
        repos = region.get("repos")
        p3_patched = None
        if repos:
            rj = json.load(open(TRACK / repos))
            for off, oldb, newb in rj["p4"]:
                assert p4[off] == oldb, (hex(off), hex(p4[off]), hex(oldb))
                p4[off] = newb
            if rj.get("p3"):
                romset.extract(ROMSET, ("ffight",), ["ff_37.12f"], td)
                p3_patched = bytearray(Path(td, "ff_37.12f").read_bytes())
                for off, oldb, newb in rj["p3"]:
                    assert p3_patched[off] == oldb, (hex(off), hex(p3_patched[off]), hex(oldb))
                    p3_patched[off] = newb
        p5 = bytearray(Path(td, "ff-32m.8h").read_bytes())

    # p3_patched is a SEPARATE fresh read, so writes to `p3` would be
    # silently dropped whenever repos carries p3 edits.  Collapse to one
    # array and always emit it -- unpatched it is byte-identical to stock.
    p3_out = p3_patched if p3_patched is not None else p3

    def w_prog(addr, old_b, new_b):
        """Patch one byte of the >=0x40000 program space (p3 even/p4 odd)."""
        i = (addr - 0x40000) // 2
        part = p3_out if addr % 2 == 0 else p4
        assert part[i] == old_b, (hex(addr), hex(part[i]), hex(old_b))
        part[i] = new_b

    # ---- "CODY!" not "CODY !" (user) ----------------------
    # The gag card's text is a plain ASCII byte string at 0x06853A in the
    # story-caption record format: [u16 cell][u16 attr][chars][0x00].  It
    # draws as 2x2 double-height blocks, two columns per character.
    #
    # Deleting the space is not an option -- the record is byte-packed and
    # every record after it would shift.  Instead SWAP the space and the
    # '!' and start the record one column earlier:
    #
    # before  cell 0x0D28 (col 26) "CODY !"  -> C O D Y _ !  cols 26..37
    # after   cell 0x0DA8 (col 27) "CODY! "  -> C O D Y !    cols 27..36
    #
    # Same byte count, and the word stays centred: both spans centre on
    # column 31.5.  The trailing space draws nothing (verified on the
    # shipped build -- the original space wrote no tiles at cols 34/35),
    # so it costs only a column of blank card.
    w_prog(0x068537, 0x28, 0xA8)        # cell low byte: col 26 -> 27
    w_prog(0x06853E, 0x20, 0x21)        # ' ' -> '!'
    w_prog(0x06853F, 0x21, 0x20)        # '!' -> ' '

    # >= 0x40000 goes through p3_OUT, never p3: p3_out is the array that is
    # emitted, and it is a separate fresh read whenever the caption repos
    # carry p3 edits, so a write to `p3` here would be silently dropped --
    # even bytes unpatched, odd bytes patched, a half-applied instruction
    # stream.
    def w_pair(addr, blob):
        for i, v in enumerate(blob):
            a = addr + i
            if a < 0x40000:
                (p1 if a % 2 == 0 else p2)[a // 2] = v
            else:
                (p3_out if a % 2 == 0 else p4)[(a - 0x40000) // 2] = v

    def r_pair(addr, n):
        out = []
        for i in range(n):
            a = addr + i
            if a < 0x40000:
                out.append((p1 if a % 2 == 0 else p2)[a // 2])
            else:
                out.append((p3_out if a % 2 == 0 else p4)[(a - 0x40000) // 2])
        return bytes(out)

    assert r_pair(0x18170, 6) == bytes.fromhex("207c000ca040")
    w_pair(0x18170, be16(0x4EB9) + be32(ex_title_entry))

    # ---- MAPPER-NEUTRAL FILLS.  Two stock sites fill a shared 16 KB map with
    # tile codes the ffight PAL leaves UNMAPPED, relying on "unmapped draws
    # nothing": the continue/game-over screen fills $90B000-$90BFFF with code
    # $0000 (SCR2 rows 49-62 + SCR3 rows 56-63 windows), and the resume-after-
    # continue restore stamps eight SCR1 cells with code $0020.  Under jtcps1
    # game id 0x1E those are blank; under the unrestricted block 0x20 -- which
    # this ROM needs so its own OBJ >= 0x8000 render (letterbox, walker cels,
    # sprite patches) -- SCR2/SCR3 draw OBJ tile 0 tiled across the screen and
    # SCR1 draws tile 0x10 -- a defective tile fills the continue screen, and
    # garbage stays after continuing.
    #
    # So make the fills mapper-neutral: $0020 -> $4420 (SCR1's own blank tile,
    # in range), and the $90B000 fill value $0000 -> $2F12, the code whose tile
    # data is all-0xFF (pen 15, transparent) in EVERY layer's decode of it --
    # SCR1 64 B @0xBC480 and SCR2 128 B @0x178900 are stock, SCR3 512 B @
    # 0x5E2400 lies in the appended region (asserted blank at gfx assembly).
    # Under 0x1E $2F12 is unmapped on SCR2 (<0x3000) and SCR3 (code[13:7]=0x5E)
    # exactly like $0000, so real hardware / MAME are byte-identical.  The fill
    # loop cannot take it via moveq, hence the 3-instruction rewrite; the
    # redundant second fill call (its range is inside the first) is dropped to
    # make room.  Proven: patched set under a 0x20-faithful HBMAME key vs stock
    # under the 0x1E key, pixel-identical across the FULL GAME (R1-R6, both
    # bonus stages, 12+ continues, ending, name entry, staff roll), both regions.
    for addr, old, new in (
        (0x5DFEC, "0020", "4420"),                     # resume: SCR1 cell, row 24
        (0x5DFF4, "0020", "4420"),                     # resume: SCR1 cell, row 25
        (0x5E008, "610000E0", "4E714E71"),             # drop bsr.w fill_90B800
        (0x5E0E8, "600C207C0090B8003A3C00FF4E71",      # fill_90B000:
                  "203C2F12000020C051CDFFFC4E75"),     #   move.l #$2F120000,d0 / move.l d0,(a0)+ / dbra d5 / rts
    ):
        got = r_pair(addr, len(old) // 2)
        assert got == bytes.fromhex(old), f"{addr:#x}: {got.hex()} != {old}"
        w_pair(addr, bytes.fromhex(new))

    # ---- CONTINUE-ENTRY BLANK.  The same screen's setup (0x5E004) flips the
    # SCR2/SCR3 bases to the shared map with direct register writes while
    # its scroll/layer control travel through the a5 shadows (one vblank
    # behind), so ONE render shows the shared map at the play scroll: when
    # that scroll aliases map cols 32-47 the SCR2 window reads the SCR3
    # frame codes 0x0980-0x0A3F -- blank under 0x1E, OBJ-tile fragments
    # under 0x20 (US f53730/f103080 of the full-game sweep, JP twins) --
    # and when it lands on the SCR1 rows it draws SCR2 tile $4420 (our
    # appended art; unpopulated ROM = blank on a stock board).  The SCR2
    # base write becomes jsr conthook + nop: the hook clears the S2/S3
    # enable bits in the hardware layer-control register (from $70(a5),
    # the value the next vblank restores) and then does the displaced
    # write.  No shadow is touched, so from the next vblank on every write
    # is stock.  See the conthook comment in engine.py.  Same site in both
    # regions (asserted).
    if True:
        ce = r_pair(0x5E010, 8)
        assert ce == bytes.fromhex("33FC908000800104"), ce.hex()
        w_pair(0x5E010, be16(0x4EB9) + be32(ENGINE + conthook_off) + be16(0x4E71))

    # ---- BOOT FLASH.  The reset path (0x5E7AC) enables all three scroll
    # layers at its seventh instruction (move.w #$12CE,$80016E, 0x5E7D8)
    # and only then clears OBJ/palette and fills the maps (the a4-return
    # calls at 0x5E810..0x5E83C -- ~3 frames of straight-line fills, SCR1
    # $4420 / SCR2 $3000 / SCR3 $0980).  Until the SCR2/SCR3 fills land
    # the maps hold power-on $0000: unmapped (black) under 0x1E, but OBJ
    # tile 0 tiled across the screen under 0x20 -- the grid flash MiSTer
    # shows before the ROM check (SLOT0_ERASE leaves VRAM zeroed, same as
    # MAME).  Same defect class as the continue fills above but in the
    # boot path, so instead of refilling, DEFER the enable: the boot write
    # keeps every bit except the three scroll enables ($12CE -> $12C0,
    # CPS-B-04 enables are bits 1-3), and the first instruction after the
    # SCR3 fill returns -- 0x5E840, reached only via jmp (a4), no other
    # refs (scanned) -- detours to boothook, which writes the stock $12CE
    # and re-executes the displaced CPS-B ID read.  jmp in / jmp out, no
    # stack: work RAM is untested this early.  Under 0x1E every frame of
    # the widened window is the backdrop pen stock already draws, so real
    # PAL / stock MAME are pixel-identical (proven: f=1..400 every frame
    # and ROM check + attract to f=2000 every 30, both regions, both
    # keys).  Same bytes both regions (asserted).
    bd = r_pair(0x5E7D8, 8)
    assert bd == bytes.fromhex("33FC12CE0080016E"), bd.hex()
    w_pair(0x5E7DA, be16(0x12C0))            # boot: scroll enables off
    bh = r_pair(0x5E840, 6)
    assert bh == bytes.fromhex("303900800160"), bh.hex()
    w_pair(0x5E840, be16(0x4EF9) + be32(ENGINE + boothook_off))

    # repoint the credits roll at the relocated, extended table.  ONE
    # instruction owns it, so this is the whole relocation.
    assert r_pair(CREDITS_PTR, 4) == be32(CREDITS_SRC), \
        f"credits pointer at {CREDITS_PTR:#x} is not the stock one"
    w_pair(CREDITS_PTR, be32(CREDITS_ADDR))

    # own the story block: outer scene table 0x170CC, 11 entries; keep the
    # scene-0x00 init handler (arms cue counters), point 0x02..0x14 at a
    # stub in dead handler space that jumps to the engine
    OUTER, STUB = 0x170CC, 0x17182
    ents = [struct.unpack(">H", r_pair(OUTER + i * 2, 2))[0] for i in range(11)]
    assert ents[0] == 0x170E2 - OUTER and ents[1] == 0x17140 - OUTER, ents
    # story block only: scenes 0x02..0x08 (measured: 0x0A=title, 0x0C+=demo)
    for i in range(1, 5):
        w_pair(OUTER + i * 2, be16(STUB - OUTER))
    w_pair(STUB, be16(0x4EF9) + be32(ENGINE))
    # one-shot INIT on the story-block arm (inside the kept init handler)
    arm6 = r_pair(0x170F6, 6)
    assert arm6[:2] == bytes.fromhex("3B7C") and arm6[4:] == \
        bytes.fromhex("92B0"), arm6.hex()   # value is region-retimed
    w_pair(0x170F6, be16(0x4EB9) + be32(ENGINE + init_off))
    # NOP the game's two RUNTIME scroll2-base writes (scene setups at
    # 0x0021FE and 0x05DAA0): they land between our per-tick base pins and
    # display buffer A for one frame (the scene-entry flashes).  The boot
    # write at 0x05E7F0 stays, so stock demo keeps its base.
    # vblank detour: the handler's `jsr $984.l` at 0x5A0 is hooked -- right
    # after its register/shadow copies ($554..$59C) and BEFORE the sound /
    # input calls ($984, $F72, $E42, $50E) -- with a jsr to the engine's
    # override block, whose exit tail-jumps to the displaced $984.  Hooking
    # the movem+rte at 0x5E2 instead, i.e. after those calls, leaves the
    # override only ~19 lines of blanking: on jtcps1 those calls take ~13
    # lines against MAME's ~4.  The 0x5E2 epilogue stays stock.
    orig = r_pair(0x5E2, 6)
    assert orig == bytes.fromhex("4CDF7FFF4E73"), orig.hex()
    hook = r_pair(0x5A0, 6)
    assert hook == bytes.fromhex("4EB900000984"), hook.hex()
    if not OPT["no_vbl_detour"]:
        w_pair(0x5A0, be16(0x4EB9) + be32(ENGINE + ovr_off))  # jsr override
    if region.get("sprite_slide"):
        # gate the string stamper ($1258: INSERT COIN blink ON/OFF, and
        # the title init's first stamp) behind the slide: the blinker
        # runs mid-frame on its own clock, so one stamped frame renders
        # before any vblank-end cell clear can catch it.  The stub in
        # the engine skips the stamp only while magic==51DE AND a
        # travel is nonzero (actively sliding); the title-hold blink
        # (measured: ~37-frame cycle through the whole hold) and every
        # other screen's text run stock.
        ent = r_pair(0x1258, 6)
        assert ent == bytes.fromhex("D00065000052"), ent.hex()
        w_pair(0x1258, be16(0x4EF9) + be32(ENGINE + blink_off))
        # OBJ-base hook: the handler's `move.w $9e(a5),$800100.l` (8 bytes
        # at $584) becomes jsr objhook + nop.  The hook re-executes that
        # move.w itself, so every non-slide vblank is byte-for-byte the
        # same write; in slide mode it edits the OBJ list and picks the
        # base BEFORE the write -- the one $800100 pulse per frame that
        # jtcps1's OBJ DMA (one entry per line, bank swap per write)
        # tolerates.  See the objhook comment in engine.py.
        ob = r_pair(0x584, 8)
        assert ob == bytes.fromhex("33ED009E00800100"), ob.hex()
        w_pair(0x584, be16(0x4EB9) + be32(ENGINE + objhook_off) + be16(0x4E71))
    # palette-DMA hook: the handler's `move.w #$9140,$80010A.l` (8 bytes at
    # $594) becomes jsr palhook + nop.  The hook re-executes that write
    # except while a cutscene engine is rendering, when the engine starts
    # the palette copy itself right after its own palette writes (a25 RTL
    # read: the copy stalls the 68000 ~12 lines on jtcps1 and would run
    # before our writes).  See the palhook comment in engine.py.
    ph = r_pair(0x594, 8)
    assert ph == bytes.fromhex("33FC91400080010A"), ph.hex()
    if not OPT["no_vbl_detour"]:
        w_pair(0x594, be16(0x4EB9) + be32(ENGINE + palhook_off) + be16(0x4E71))

    if e2:
        # ending INIT arm at the REAL post-game entry (traced live: the
        # attract-context entry 0x1978A never runs after a genuine clear).
        # 0x186A0 is the true seg-A entry -- it fires text 0x139/0x13A and
        # cue 0x54 -- and 0x186BA (move.b #1,$8d(a5), 6 bytes) is the arm
        # site; INIT re-executes it via arm_restore.
        e_arm = r_pair(0x186BA, 6)
        assert e_arm == bytes.fromhex("1B7C0001008D"), e_arm.hex()
        w_pair(0x186BA, be16(0x4EB9) + be32(ENGINE2 + e2["init"]))
        # silence the stock ending typewriter for the reunion beat: the
        # engine draws the same dialogue as CD-timed subtitles instead.
        # 0x186C0: jsr $283a.w -- the entry's 0x139/0x13a enqueue, right
        # after the arm site.  0x1871A: jmp $283a.w -- the closing-line
        # (0x13b/0x13c) tail-call fire; rts is the equivalent return.
        # The farewell fire (0x18ADA area) is deliberately untouched.
        e_enq = r_pair(0x186C0, 4)
        assert e_enq == bytes.fromhex("4EB8283A"), e_enq.hex()
        w_pair(0x186C0, bytes.fromhex("4E714E71"))       # nop; nop
        e_cls = r_pair(0x1871A, 4)
        assert e_cls == bytes.fromhex("4EF8283A"), e_cls.hex()
        w_pair(0x1871A, bytes.fromhex("4E754E71"))       # rts; (pad)
        # pre-roll blackout at the TRUE ending-scene start: outer-6
        # phase 0 (0x18646) is a COROUTINE -- it yields across ~19
        # frames, building the stock reunion cel and fading it in
        # mid-handler, and only reaches the text-fire/arm tail (0x186BA)
        # ~18 frames later (PC-attributed write taps).
        # Displace its layer-shadow write (move.w #$138E,$6e(a5) at
        # 0x18650, 3 instructions in) with a jsr to the blackout stub:
        # scrolls off + obj fetch page parked from the sequence's first
        # frame, so the whole pre-roll runs invisibly at stock timing.
        # (Not outer-4 inner-0 (0x18518): that is the start of the whole
        # post-game block, and the bonus screens' later phases legitimately
        # restore video.)
        e_blk = r_pair(0x18650, 6)
        assert e_blk == bytes.fromhex("3B7C138E006E"), e_blk.hex()
        w_pair(0x18650, be16(0x4EB9) + be32(ENGINE2 + e2["blk"]))
        # ENDING MUSIC ALIAS.  The stock ending fires music cue 0x54 --
        # JP at 0x186CC (2 frames after INIT, on the arm site's return
        # path; $72600==0 branch), US/World at 0x18632 (INIT+901, the
        # 0x384 countdown armed at 0x1864A).  The arrange pack maps 0x54
        # to tr26 (the arranged INSTRUMENTAL ending), so under the voiced
        # pairing it replaced the voiced track fired at INIT.  Emit alias
        # 0x78 instead: gated (verb=none suppress=1) in both packs, and
        # the Z80 stub maps 0x78 -> 0x54 so stock hardware is unchanged.
        # Both sites are patched in both regions; each region reaches
        # only its own.
        for site in (0x186CC, 0x18632):
            e_cue = r_pair(site, 4)
            assert e_cue == bytes.fromhex("303C0054"), (hex(site), e_cue.hex())
            w_pair(site, bytes.fromhex("303C0078"))

    if e3:
        # ARC-7 <- CD-6, the farewell.  Structurally identical to the
        # reunion phase one table entry along: blackout+skip at the phase's
        # layer-shadow write, arm at its move.b #1,$8d(a5), and the text
        # fire is a TAIL CALL (jmp, not jsr) so its stub is a bare rts.
        p_blk = r_pair(0x18A6A, 6)
        assert p_blk == bytes.fromhex("3B7C138E006E"), p_blk.hex()
        w_pair(0x18A6A, be16(0x4EB9) + be32(ENGINE3 + e3["blk"]))
        p_arm = r_pair(0x18AD2, 6)
        assert p_arm == bytes.fromhex("1B7C0001008D"), p_arm.hex()
        w_pair(0x18AD2, be16(0x4EB9) + be32(ENGINE3 + e3["init"]))
        p_txt = r_pair(0x18AD8, 4)
        assert p_txt == bytes.fromhex("4EF8283A"), p_txt.hex()
        w_pair(0x18AD8, bytes.fromhex("4E754E71"))       # rts; (pad)

    # No base-write NOPs are needed: the base is constant 0x90C0, so the
    # game's scene-setup rewrites are same-value no-ops
    for i, v in enumerate(engine):
        a = ENGINE + i - 0x80000
        p5[a ^ 1] = v
    if e2:
        assert ENGINE + len(engine) <= ENGINE2, "engine1 grew into engine2"
        for i, v in enumerate(e2["eng"]):
            a = ENGINE2 + i - 0x80000
            p5[a ^ 1] = v
    if e3:
        assert ENGINE2 + len(e2["eng"]) <= ENGINE3, "engine2 grew into engine3"
        for i, v in enumerate(e3["eng"]):
            a = ENGINE3 + i - 0x80000
            p5[a ^ 1] = v

    (outdir / "ff_36.11f").write_bytes(p1)
    (outdir / "ff_42.11h").write_bytes(p2)
    (outdir / "ff-32m.8h").write_bytes(p5)

    # ---- zip (base members always from the canonical US zip; region
    # overrides layer on top: program, key, p7 name, J stock gfx)
    # ---- Z80 sound-driver patch (contract Option B, minimal form).
    # The "version 2.00" driver's ISR reads the sound latch with the ONE
    # `LD A,(F008)` at 0x52; every byte after it is a plain load (no flag
    # dependence -- verified), so a CALL fits over it exactly.  The stub
    # (at 0x0B70, inside the 0x0B65-0x0BFF slice of the zero block that
    # nothing references -- immediate-operand scan) translates:
    # 0x70-0x73 -> 0x5C  the voiced-cutscene keys: NULL song entry, so
    # stock hardware stays silent at INIT and the
    # ROM's own ear-approved FM schedule plays.
    # (The contract drafted "alias to FM music"; that
    # assumed the voiced cue REPLACED the music cues.
    # This ROM keeps them, so null is the benign map.
    # Also fixes the deployed endings' 0x71/0x73
    # firing arbitrary SFX on stock hardware.)
    # 0x74      -> 0x28  Damnd-laugh alias (AUDIO_CONTRACT 1b).
    # Anything else passes untouched, control bytes 0xF0+ included.
    # ---- STOCK MEMBERS COME FROM THE ARCADE ROMSET, NOT FROM A PRIOR BUILD.
    # Reading them out of roms/hbmame/ffightus01.zip -- a prior build of this
    # very set -- would mean a machine that has never built the set cannot
    # build it.  Every one of them is stock data the romset already carries,
    # verified member by member:
    # c07.m1           ffight.7z:ff_09.12b     Z80 driver, pre-patch
    # c07.v1 / .v2     ffightj.7z:ffj_30/31    OKI samples
    # c07.c01/03/05/07 ffight.7z's four gfx mask ROMs, in the order this
    # set names them -- the only delta was the 8/4/8/4
    # bytes patch_dash_tile writes, and that is idempotent
    # so applying it to genuinely stock data lands in the
    # same place.
    WORLD_GFX = [("c07.c01", "ff-5m.7a"), ("c07.c03", "ff-7m.9a"),
                 ("c07.c05", "ff-1m.3a"), ("c07.c07", "ff-3m.5a")]
    with tempfile.TemporaryDirectory() as td:
        romset.extract(ROMSET, ("ffight",), ["ff_09.12b"] + [r for _, r in WORLD_GFX], td)
        romset.extract(ROMSET, ("ffightj", "ffight"), ["ffj_30.bin", "ffj_31.bin"], td)
        m1 = bytearray(Path(td, "ff_09.12b").read_bytes())
        stock_base = {"c07.v1": Path(td, "ffj_30.bin").read_bytes(),
                      "c07.v2": Path(td, "ffj_31.bin").read_bytes()}
        world_gfx = {n: Path(td, r).read_bytes() for n, r in WORLD_GFX}
    key6 = (TRACK / "control" /
            region["key_name"]).read_bytes()
    assert m1[0x52:0x55] == b"\x3a\x08\xf0", "m1 latch read moved"
    assert m1[0x0B70:0x0B9F] == b"\x00" * 0x2F, "m1 stub site not free"
    STUB = 0x0B70
    # Alias range widened to 0x77 (the phone-answer click).  Each alias is
    # gated in the arrange packs and mapped back to its stock key here, so
    # vanilla hardware is byte-identical; without a mapping the key would
    # play an arbitrary sample (a MAME sweep found NO inert bytes in this
    # driver).  Offsets below are byte positions in `stub`; a JR
    # displacement is target - (its own offset) - 2.
    stub = bytes([
        0x3A, 0x08, 0xF0,        # 00  LD  A,(F008)
        0xFE, 0x70,              # 03  CP  70
        0xD8,                    # 05  RET C          ; <70 pass
        0xFE, 0x79,              # 06  CP  79
        0xD0,                    # 08  RET NC         ; >=79 pass
        0xFE, 0x74,              # 09  CP  74
        0x28, 0x13,              # 11  JR  Z,+13      ; -> 32 laugh
        0xFE, 0x75,              # 13  CP  75
        0x28, 0x12,              # 15  JR  Z,+12      ; -> 35 ring
        0xFE, 0x76,              # 17  CP  76
        0x28, 0x11,              # 19  JR  Z,+11      ; -> 38 song
        0xFE, 0x77,              # 21  CP  77
        0x28, 0x10,              # 23  JR  Z,+10      ; -> 41 click
        0xFE, 0x78,              # 25  CP  78
        0x28, 0x0F,              # 27  JR  Z,+0F      ; -> 44 ending music
        0x3E, 0x5C,              # 29  LD  A,5C       ; 70-73 -> null cue
        0xC9,                    # 31  RET
        0x3E, 0x28,              # 32  LD  A,28       ; 74 -> laugh sample
        0xC9,                    # 34  RET
        0x3E, 0x35,              # 35  LD  A,35       ; 75 -> phone ring
        0xC9,                    # 37  RET
        0x3E, 0x52,              # 38  LD  A,52       ; 76 -> opening song
        0xC9,                    # 40  RET
        0x3E, 0x36,              # 41  LD  A,36       ; 77 -> phone answer
        0xC9,                    # 43  RET
        0x3E, 0x54,              # 44  LD  A,54       ; 78 -> ending music
        0xC9,                    # 46  RET
    ])
    m1[STUB:STUB + len(stub)] = stub
    m1[0x52:0x55] = bytes([0xCD, STUB & 0xFF, STUB >> 8])   # CALL stub
    # The opening song itself is restructured to span the cutscene, its
    # cadence fitted to the title animate-in (fmloop.py); the cue schedule
    # fires 0x76 once.
    m1_out, fm_note = fmloop.apply_opening_loop(bytes(m1),
                                                *region["fm_open_loop"])
    print(f"  m1 {fm_note}")
    ren = {"c07.p1": bytes(p1), "c07.p2": bytes(p2),
           "c07.m1": m1_out,
           region["p4_name"]: bytes(p4), "c07.p5": bytes(p5),
           "c07.p3": bytes(p3_out),
           region["p7_name"]: bytes(p7), region["key_name"]: key6,
           **gfx_parts}
    if region["jgfx"]:
        ren.update(build_j_stock_gfx())
    else:
        stock4 = [bytearray(world_gfx[f"c07.c{n:02d}"]) for n in (1, 3, 5, 7)]
        n4 = len(stock4[0])
        space4 = bytearray(n4 * 4)
        for fi, rom in enumerate(stock4):
            space4[fi * 2::8] = rom[0::2]
            space4[fi * 2 + 1::8] = rom[1::2]
        patch_dash_tile(space4)
        select_window.patch(space4)
        for fi, name in enumerate(("c07.c01", "c07.c03", "c07.c05", "c07.c07")):
            d = bytearray(n4)
            d[0::2] = space4[fi * 2::8]
            d[1::2] = space4[fi * 2 + 1::8]
            ren[name] = bytes(d)
    # The member list is STATED, not inherited.  Inheriting whatever
    # roms/hbmame/ffightus01.zip contains (minus a `drop` set of US-specific
    # names when building JP) would define the set's own contents by a prior
    # build of it.  Everything is either built above or stock, and both are
    # in hand, so write exactly those.
    members = dict(stock_base)           # c07.v1 / c07.v2, stock OKI samples
    members.update(ren)
    with zipfile.ZipFile(outdir / (region["set_name"] + ".zip"), "w",
                         zipfile.ZIP_DEFLATED) as zout:
        for name in sorted(members):
            zout.writestr(name, members[name])
    print(f"wrote {outdir}/{region['set_name']}.zip")
    # ---- MAKE THE SET SELF-DESCRIBING.
    # Written BESIDE the zip, never inside it: an unexpected member would
    # change what HBMAME validates against the driver's member list, and the
    # archive has to stay exactly what the driver expects.
    # Why it exists: the set needs to say what produced it, and the CRC
    # re-pin pass needs to know which build it is pinning.  It records argv --
    # the knobs that change output bytes ARE argv -- which is the complete
    # story rather than an FFCD_* namespace scrape that could miss anything
    # set another way.
    # NOTE: pin the MEMBER CRCs recorded here, never the zip hash --
    # the archive is not byte-reproducible (timestamps) while every member
    # is (both regions).
    try:
        import hashlib as _hl, datetime as _dt, os as _os, json as _js
        import sys as _sys
        _knobs = {k: v for k, v in OPT.items() if v not in (None, False)}
        _stray = {k: v for k, v in _os.environ.items()
                  if k.startswith("FFCD_") or k == "NO_VBL_DETOUR"}
        # (a tripwire for stray FFCD_* variables -- the knobs are argv, not
        # the environment)
        _prov = {
            "set": region["set_name"],
            "built": _dt.datetime.now().isoformat(timespec="seconds"),
            "region": region.get("set_name", "?"),
            "convs": {"opening": str(conv)},
            "argv": _sys.argv[1:],
            "knobs": _knobs,
            # Should always be empty.  Nothing reads FFCD_* any more, so a
            # non-empty value here means the environment is carrying a stale
            # variable from an older build script -- worth seeing, not acting on.
            "stray_env": _stray,
            "frontend": {
                "title": "FINAL FIGHT EX",
                "revision": "260917",
                "ex_design": "arcade lettering",
                "ex_png_sha256": _hl.sha256((title_ex.ART / "ex.png").read_bytes()).hexdigest(),
                "ex_grid_sha256": _hl.sha256((title_ex.ART / "ex_pixels.json").read_bytes()).hexdigest(),
                "ex_letter_sha256": {_n: _hl.sha256((title_ex.ART / f"{_n}.png").read_bytes()).hexdigest()
                                     for _n in ("e", "x")},
                "ex_gfx_offset": title_ex.GFX_OFFSET,
                "ex_gfx_sha256": _hl.sha256(ex_gfx).hexdigest(),
                "ex_position": list(title_ex.HOME),
                "ex_tiles": title_ex.packed()[2],
                "ex_layer": "SCR3 (topmost)",
                "ex_motion_sha256": _hl.sha256((title_ex.MOTION / "cd_title_motion.json").read_bytes()).hexdigest(),
                "window_pixels": list(select_window.PIXELS),
            },
            "members": {},
        }
        for _n, _b in sorted(members.items()):
            _prov["members"][_n] = {"bytes": len(_b),
                                    "crc32": format(zlib.crc32(_b) & 0xFFFFFFFF, "08x"),
                                    "md5": _hl.md5(_b).hexdigest(),
                                    "sha256": _hl.sha256(_b).hexdigest()}
        (outdir / "build_provenance.json").write_text(_js.dumps(_prov, indent=2))
        print(f"wrote {outdir}/build_provenance.json "
              f"({len(_prov['members'])} members"
              + (f", {len(_knobs)} knob(s)" if _knobs else "")
              + (f", {len(_stray)} STRAY FFCD_* env var(s)" if _stray else "")
              + ")")
    except Exception as _e:      # provenance must never break a build
        print(f"  (provenance not written: {_e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
