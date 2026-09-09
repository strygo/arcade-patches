"""Per-game protocol descriptors and verified command-map data.

The latch protocol fields are common to the whole QSound family (verified for
driver 1.06b in internal research notes Q4; Phase 0 owns revisions
1.05A..1.71): commands are read from shared-RAM odd bytes
0x618001/0x618003, the argument word from 0x618007/09, the argument byte
from 0x618005, and the handshake byte lives at +0x1f (game writes 0x00 =
pending, sound side writes 0xff = ready).

Fade laws (per-game, measured):
  * HSF2 AE:          steps = (0x444 / arg) * 60      (internal research, HSF2 Q5)
  * Anthology family: steps =  0xffff / arg, per frame (internal research, Q4)

Control-verb maps are the arcade-native 0xffxx vocabulary each game's own
console port honors.
"""
from __future__ import annotations

import dataclasses

from .format import (Protocol, FADE_ANTHOLOGY, FADE_HSF2, FADE_NONE, VERB_NONE,
                     VERB_STOP, VERB_FADE_OUT, VERB_FADE_KEEP, VERB_RESTORE,
                     VERB_MASTER_FADE)

# Anthology control table (zero1 comp2 +0x12bd50; identical shape in zero6).
# Explicit no-ops (ff01-ff04, ff08-ff0b) are Z80/SFX-side controls: the Z80
# must still see them, and the player must not react.
CONTROL_ANTHOLOGY = {
    0xff00: VERB_STOP, 0xff01: VERB_NONE, 0xff02: VERB_NONE,
    0xff03: VERB_NONE, 0xff04: VERB_NONE, 0xff05: VERB_STOP,
    0xff06: VERB_FADE_OUT, 0xff07: VERB_FADE_KEEP,
    0xff08: VERB_NONE, 0xff09: VERB_NONE, 0xff0a: VERB_NONE,
    0xff0b: VERB_NONE, 0xff0c: VERB_RESTORE, 0xff0d: VERB_MASTER_FADE,
}

# The ARCADE Alpha 2 driver (sfa2 / sfz2al boards, 1.05C) shares the Anthology
# record layout but NOT its control vocabulary.  Measured on the board (68K
# producer -> Z80 handler, a driven command census plus a hardware readout):
# 0xff00 stops the BGM channel (boot, attract restart after game over);
# 0xff05 releases an SFX slot and never touches the BGM (coin-in, START,
# 2P-join abort) -- the Anthology map stopped on it, which cut the arranged
# title music at coin-in; 0xff01-04/08-0d have no callers in the 68K image, so
# the default is none and they pass through to the Z80 untouched.
#
# The PS2 port's table is not this table: under it 0xff06 meant "fade out and
# switch looping off", where the board uses it to fade the BGM back IN.  That
# one row is what silenced the arranged track for the rest of a fight after any
# super or special finish (the sfz2al hardware report).
CONTROL_ALPHA2_ARCADE = {
    0xff00: VERB_STOP,
    # The dramatic-finish pair, measured on the board: 0xff07 fades the BGM
    # OUT (argw 0xffff = one frame at a super/special finish, 0x0280 = the
    # win pose) and 0xff06 fades it back IN (argw 0x0200 argb 0xff = 128
    # frames to full).  Both are verb 4: the fade target comes from the
    # command's own arg byte, so one verb expresses both directions
    # (argb 0x00 -> silence, 0xff -> unity).  Verb 3 would be wrong twice
    # over -- it switches looping off and ends the track at silence.
    0xff06: VERB_FADE_KEEP,
    0xff07: VERB_FADE_KEEP,
}

# SF Alpha 1 (arcade set sfau, driver 1.05A).  Same family, but its 68K
# never calls the fade-in: the only fade is the win pose (0xff07 0x0280).
# 0xff05 is an SFX-slot release and 0xff0c/0xff0d have no callers -- both
# pass through.  0xff01 is NOT mapped: a disassembly reading called it a
# stop-all, but capturing the board's own audio on the three sibling
# drivers showed the music playing straight through it (see CONTROL_HSF2),
# so it stays a pass-through here until this driver is measured too.
CONTROL_ALPHA1_ARCADE = {
    0xff00: VERB_STOP,
    0xff07: VERB_FADE_KEEP,
}

# Super Puzzle Fighter II Turbo (driver 2.01b).  Driven route: 0xff05 is an
# SFX-slot release that the old map stopped on -- twice with the next song
# 10.3 s and 3.3 s away, so the arranged track was being cut early.  0xff06
# is a fade-IN and never fires in play; 0xff08 (7 sightings) is not music.
CONTROL_SPF2T_ARCADE = {
    0xff00: VERB_STOP,
    0xff07: VERB_FADE_KEEP,
}

# CPS1.5 QSound (wof, dino, punisher, slammast, mbombrd).  On the 1.00/1.01
# drivers BOTH 0xff06 and 0xff07 are fade-OUTs (engine A = all channels,
# engine B = BGM) -- neither is a fade-in, and the +0x05 byte is a sequence
# counter here (mbombrd increments it 0x01..0xff), NOT a fade target.  So
# master_fade, which forces the target to silence and ignores that byte;
# verb 3 would read the counter as a volume and switch looping off.
CONTROL_CPS15_ARCADE = {
    0xff00: VERB_STOP,
    0xff06: VERB_MASTER_FADE,
    0xff07: VERB_MASTER_FADE,
}

# Hyper SF2 AE (driver 1.06b).  The AE PORT's table said 0xff06 was a fade with
# looping preserved; the arcade Z80 handler fades the BGM IN.
CONTROL_HSF2 = {
    # 0xff06 is a fade-IN on this driver and never fires in play -- mapping
    # it as a fade meant fading OUR track to silence, and with no +0x05 byte
    # in the 1.06b record the target was always 0.  0xff07 is the real
    # fade-out (a new song follows 2.5 s later).
    #
    # 0xff01 is deliberately NOT a stop.  A disassembly reading called it a
    # stop-all and the record placement looked like one (no new song for
    # 7.0 s after it), but capturing the board's own audio settles it: the
    # music plays straight through, at the same level, until the next song
    #     hsf2  ff01@6060  before 1280 -> 1412 / 1507 / 1640 over 7.0 s
    #     ssf2  ff01@6024  before 1176 -> 1303 / 1371 / 1545 over 10.7 s
    #     ssf2t ff01@6223  before 1257 -> 1441 / 1413 / 1477 over 14.9 s
    # Mapping it would have muted the arranged track for 7-15 s of music.
    0xff00: VERB_STOP, 0xff07: VERB_FADE_KEEP,
}

# Standalone Super SF2 (ssf2) / Super SF2 Turbo (ssf2t) — the ORIGINAL SF2
# QSound driver (Z80 banner "version 1.04 /CPS2 1993"; Phase-0
# manifests/protocol/{ssf2,ssf2t}.json).  Same SF2 QSound driver lineage as
# HSF2 AE, so the HSF2 control vocabulary is mirrored; the Phase-0 latch trace
# observed 0xff00 (stop) in-game and confirms the record layout matches HSF2
# (cmd_hi +0x01, cmd_lo +0x03, handshake +0x1f, NO +0x05 arg byte -> off_arg_byte
# = 0).  Every other command — attract, vs, select, continue, SFX, voice —
# passes through untouched to real QSound.
CONTROL_SSF2 = {
    # As HSF2: 0xff06 is a fade-IN, and 0xff01 is NOT a stop -- the audio
    # capture shows the music playing through it for 10.7 s (ssf2) and
    # 14.9 s (ssf2t); see CONTROL_HSF2 for the levels.  0xff07 stays
    # UNMAPPED on purpose: this driver re-sends it on ~80 consecutive
    # frames, and a fade re-armed every frame from the current level never
    # reaches its target.
    0xff00: VERB_STOP,
}


def _proto(game_id: str, fade_law: int, fc1: int, fc2: int,
           control: dict, default_verb: int) -> Protocol:
    return Protocol(game_id=game_id, fade_law=fade_law,
                    fade_const1=fc1, fade_const2=fc2,
                    control_verbs=dict(control),
                    control_default_verb=default_verb)


# --- CPS1.5 QSound (Warriors of Fate, Cadillacs, Punisher, Slam Masters,
#     Muscle Bomber Duo) ------------------------------------------------------
# CPS1.5 uses the SAME QSound board as CPS2 — jtcores instantiates jtcps15_sound
# in BOTH the cps2 and cps15 cores — and Phase-0 confirms the SAME latch
# contract as the CPS2 Anthology family: command bytes at +0x01/+0x03, arg word
# at +0x07/+0x09, handshake at +0x1f (68K posts 0x00 = pending, Z80 acks 0xff =
# ready), record-then-handshake on every driver.  The ONLY difference is the
# 68K latch page — 0xf18000 on CPS1.5 vs 0x618000 on CPS2 (MAME
# capcom/cps1.cpp qsound_main_map; jtcps1_main main2qs_cs decodes
# 0xf18000-0xf19fff).  Ground truth: manifests/protocol/{wof,dino,punisher,
# slammast,mbombrd}.json + internal protocol traces "CPS1.5 QSound sets".
#
# Because latch_page is a per-pack config field (header 0x8c -> trigger cfg
# registers 0x00/0x01), the CPS2 RTL tap (cpsplus_top + cpsplus_trigger, the
# 16-bit-keyed 4608-row table) is reused verbatim by the jtcps15_cpsplus core:
# no new RTL, only this descriptor changes the page.
#
# off_arg_byte (the +0x05 fade-target volume byte) is present only on the
# /MB 1.01 driver (slammast, mbombrd); the 1.00 driver (wof, dino) and
# punisher's 1.01 driver omit it (observed_record_offsets has no 0x05) — set
# per game.  The fade LAW (arg -> steps) was not disassembled for these early
# 1.00/1.01 drivers; FADE_ANTHOLOGY (0xffff/arg) is the family default and is
# revisable per pack if a fade trace warrants it.  control_default_verb is
# VERB_NONE (an unmapped 0xffxx passes through to the Z80 and does not disturb
# the arranged player); the control bytes actually observed — 0xff00 (stop),
# 0xff06 (fade), 0xff08/0xff0b (Z80-side no-ops) — are all in CONTROL_ANTHOLOGY.
def _cps15(game_id: str, off_arg_byte: int) -> Protocol:
    return Protocol(
        game_id=game_id, latch_page=0xf18000,
        off_cmd_hi=0x01, off_cmd_lo=0x03, off_arg_hi=0x07, off_arg_lo=0x09,
        off_arg_byte=off_arg_byte, off_handshake=0x1f,
        handshake_pending=0x00, handshake_ready=0xff,
        fade_law=FADE_ANTHOLOGY, fade_const1=0xffff, fade_const2=0,
        control_default_verb=VERB_NONE, control_region_start=0xff00,
        control_verbs=dict(CONTROL_CPS15_ARCADE))


PROTOCOLS: dict[str, Protocol] = {
    # Hyper SF2 AE contract (internal research notes)
    "hsf2": _proto("hsf2", FADE_HSF2, 0x444, 60, CONTROL_HSF2, VERB_NONE),
    # (adjusted below: 1.06b omits the +0x05 record byte)
    # SF Alpha 1 / Anthology zero1 contract (internal research notes)
    "sfa1": _proto("sfa1", FADE_ANTHOLOGY, 0xffff, 0,
                   CONTROL_ALPHA1_ARCADE, VERB_NONE),
    # SF Zero 2 Alpha (arcade set sfz2al): Anthology zero6 record layout and
    # trigger vocabulary, but the ARCADE control map (see CONTROL_ALPHA2_ARCADE).
    "sfz2al": _proto("sfz2al", FADE_ANTHOLOGY, 0xffff, 0,
                     CONTROL_ALPHA2_ARCADE, VERB_NONE),
    # SF Alpha 2 (arcade set sfa2 / sfz2 / sfz2j) — base version of the same
    # game as sfz2al; identical CPS2 QSound driver, so the same latch protocol,
    # fade law, and arcade control vocabulary apply.  Used by the
    # Saturn-arrange pack (build_pack.py saturn-mus --game sfa2 --trigger-map).
    "sfa2": _proto("sfa2", FADE_ANTHOLOGY, 0xffff, 0,
                   CONTROL_ALPHA2_ARCADE, VERB_NONE),
    # Super Street Fighter II (ssf2) and Super SF2 Turbo (ssf2t) — original SF2
    # QSound driver 1.04.  Mirror the HSF2 descriptor (fade law + control map);
    # one shared descriptor per game_id (identical Phase-0 record layout, banner
    # "1.04 /CPS2 1993").  Used by build_ssf2_arrange (HSF2 arrange ADX re-keyed
    # to the standalone stage commands 0x01..0x10).
    "ssf2": _proto("ssf2", FADE_HSF2, 0x444, 60, CONTROL_SSF2, VERB_NONE),
    "ssf2t": _proto("ssf2t", FADE_HSF2, 0x444, 60, CONTROL_SSF2, VERB_NONE),
    # Street Fighter II World Warrior (sf2) — the CPS1 byte-latch dialect.
    # Ground truth: manifests/protocol/sf2.json (Z80 driver 4.25) +
    # internal research notes.  Unlike every entry above (QSound
    # shared-RAM record + handshake), CPS1 has NO shared RAM and NO
    # handshake: the 68K drain ($6284) writes a single command byte to the
    # Z80 latch at $800181 (jtcps1 snd_latch0) and a fade byte to $800189
    # (snd_latch1); the Z80 polls the command at 0xf008.  0x800180-0x80018f
    # is write-only on the 68K side, so there is nothing to read back or
    # ack.  Suppression = the RTL tap substitutes the idle/terminator byte
    # (0xff, carried in handshake_ready) into what the Z80 reads for a
    # mapped music command, so the native YM2151/OKI music never starts;
    # SFX (0x21-0x32), voices (0x56-0x87) and the 0xf0/0xf7/0xff control
    # family pass through unchanged (control_verbs empty -> no arranged
    # action, implicit stop-on-start replaces the current arranged track).
    # No fade-arg law: the fade latch is a separate channel and is not
    # wired to the player (fade_law = FADE_NONE).  Consumed by the
    # jtcps1_cpsplus core / cpsplus_cps1_tap.v (a separate RTL front-end),
    # so no pack "dialect" bit is needed; the CPS1 fields below (single cmd
    # byte at page+1, no record args/handshake) are descriptive — the tap
    # taps snd_latch0 directly, not the 68K bus.  A CPS1 pack uses a 256-row
    # (8-bit-keyed) trigger table.
    "sf2": Protocol(
        game_id="sf2",
        latch_page=0x800180,        # command latch base; cmd byte at +0x01
        off_cmd_hi=0x00,            # single-byte command: no high byte
        off_cmd_lo=0x01,           # $800181 (jtcps1 snd_latch0)
        off_arg_hi=0x00, off_arg_lo=0x00,   # fire-and-forget: no arg word
        off_arg_byte=0x00,         # no arg byte
        off_handshake=0x00,        # no handshake / no ack to synthesise
        handshake_pending=0x00,
        handshake_ready=0xf7,      # REPURPOSED: the byte the tap substitutes to
                                   # suppress a music command.  0xff (idle) does
                                   # NOTHING to the native driver, so with a
                                   # PARTIAL map it keeps playing whatever cue it
                                   # last received -- it never sees the command
                                   # that would have changed its music (measured:
                                   # SF2 select 0x0f unmapped -> native plays it,
                                   # VS 0x08 mapped+suppressed -> native never
                                   # told, select music runs under our track).
                                   # 0xf7 (section stop) silences it instead;
                                   # verified in MAME that holding 0xf7 stops the
                                   # music and a later command still starts a song.
        fade_law=FADE_NONE, fade_const1=0, fade_const2=0,
        control_default_verb=VERB_NONE,
        control_region_start=0x00f0,  # 0xf0 boot / 0xf7 stop / 0xff idle
        # 0xf7 (section stop) MUST map to VERB_STOP: the control family passes
        # through to the Z80 either way, but without this row the player never
        # hears about the stop and keeps playing past the points where the game
        # silences its own music (observed on SF2/SF2CE hardware).
        # The DEFAULT stays VERB_NONE on purpose -- 0xff (idle) follows every
        # command one frame later, so a STOP default would cut every cue short.
        control_verbs={0xf7: VERB_STOP}),
    # CPS1.5 QSound titles — SAME contract as CPS2, only the latch page moves
    # to 0xf18000 (see the _cps15 note above).  These unblock the Warriors of
    # Fate and Muscle Bomber / Slam Masters arrange packs on jtcps15_cpsplus;
    # they use the standard 16-bit-keyed (0x1200-row) QSound trigger table, so
    # the CPS2 tap RTL is reused as-is.
    "wof":      _cps15("wof",      off_arg_byte=0x00),  # driver 1.00,    no +0x05
    "dino":     _cps15("dino",     off_arg_byte=0x00),  # driver 1.00,    no +0x05
    "punisher": _cps15("punisher", off_arg_byte=0x00),  # driver 1.01,    no +0x05
    "slammast": _cps15("slammast", off_arg_byte=0x05),  # driver 1.01/MB, has +0x05
    "mbombrd":  _cps15("mbombrd",  off_arg_byte=0x05),  # driver 1.01/MB, has +0x05
}

# Phase-0 finding (manifests/protocol/hsf2.json, driver 1.06b): the HSF2
# drain routine never writes the +0x05 record byte (the Anthology-family
# fade-target volume).  off_arg_byte == 0 means "field not present".
PROTOCOLS["hsf2"].off_arg_byte = 0
# ssf2/ssf2t driver 1.04 likewise omits the +0x05 record byte (Phase-0
# observed_record_offsets = {01,03,07,09,0d,0f,11,13,15,17,19}; no 0x05).
PROTOCOLS["ssf2"].off_arg_byte = 0
PROTOCOLS["ssf2t"].off_arg_byte = 0

# Forgotten Worlds: the CPS1 byte latch like sf2, but a DIFFERENT Z80 driver --
# its 0xf7 stops the OKI ADPCM channels, not the music (sf2's 0xf7 is the music
# section stop).  A driven route shows the cost of having copied sf2's row:
# 0xf7 appears twice in 10.8k records and one of them is 3.9 s before the next
# song, so the arranged track was being cut early.  No stop row until a route
# shows which byte this game uses at music transitions (0xf0 reset and 0xf1
# both appear, each followed by a song within a few frames).
# NOTE handshake_ready -- the byte substituted to suppress a mapped cue -- is
# inherited as 0xf7 and does NOT silence this driver's music, so a partial map
# can still desync it; that is a separate, pre-existing question.
#
# Which byte DOES stop it was then measured, by capturing the board's own audio
# through a driven route (no suppression) and reading the level around each
# control byte:
#     0xf7 @f4002  3129 -> 3053      0xf7 @f8438  2904 -> 1341   music plays on
#     0xf1 @f8518  2432 ->    0      silent for the next 2.6 s
#     0xf0 @f3352  4076 ->    0      silent until the next song, 7.8 s later
#     0xf0 @f6876  3992 ->  715
# So 0xf7 is not a music stop here (it takes the sample channels down, which is
# why the level dips without stopping), while 0xf0 and 0xf1 are.  Without those
# two rows the arranged track would play over 7.8 s of intended silence.
PROTOCOLS["forgottn"] = dataclasses.replace(
    PROTOCOLS["sf2"], game_id="forgottn",
    control_verbs={0xf0: VERB_STOP, 0xf1: VERB_STOP})

# UN Squadron / Area 88 (unsquad, area88 -- sweeps byte-identical across all
# 256 commands, so one descriptor and one pack serve both sets).  Everything
# below MEASURED 2026-08-28 (MAME 0.288, one boot per probe; see
# manifests/unsquad_snes_trigger_map.tsv header for the full census):
#   * Stop probe (play 0x04, then each 0xf0-0xff five seconds later, read the
#     level): 0xf0 0xf1 0xf2 0xfa each silence the driver from ONE write;
#     sf2's 0xf7 does NOT stop music here (forgottn lesson repeated), so
#     handshake_ready is replaced, not inherited -- 0xf0 both stops current
#     music and starts nothing, exactly what the suppression substitute needs.
#   * The 68K itself was seen sending 0xf0 (hard stop before stage music) and
#     0xf4/0xf7/0xfb, which do not stop music (unmapped: they pass through to
#     the Z80 untouched).  0xfa pause-mute concern RETIRED: the arcade game
#     has no pause (hardware pass 2026-08-31).
PROTOCOLS["unsquad"] = dataclasses.replace(
    PROTOCOLS["sf2"], game_id="unsquad", handshake_ready=0xf0,
    control_verbs={0xf0: VERB_STOP, 0xf1: VERB_STOP, 0xf2: VERB_STOP,
                   0xfa: VERB_STOP})

# Magic Sword, Z80 3.50: a service-hold census (22 boots on each of msword,
# mswordj and mswordu). 0x00 and 0x01 are music; only
# f0 and f7 stop the positive-control cue, both singly and when held.
PROTOCOLS["msword"] = dataclasses.replace(
    PROTOCOLS["sf2"], game_id="msword", handshake_ready=0xf7,
    control_verbs={0xf0: VERB_STOP, 0xf7: VERB_STOP})

# Strider USA complete-music revision / Japan Resale. Driver 0x0304 clears
# the saved music bank and stops current music, preserving FM/OKI effects.
# Paired 0xf1 controls and substituted-gameplay runs established the
# behaviour below.
# ff is harmless idle but cannot silence an already-playing fallback cue.
PROTOCOLS["strider"] = dataclasses.replace(
    PROTOCOLS["sf2"], game_id="strider", handshake_ready=0xf1,
    control_verbs={0xf0: VERB_STOP, 0xf1: VERB_STOP})

# Ghouls'n Ghosts USA (ghoulsu; pack game id follows the parent `ghouls`).
# Measured 2026-08-31 with MAME 0.288, one clean boot per candidate: play the
# sustained Stage 1 cue 0x0c, then issue one byte from 0xf0..0xff five seconds
# later.  0xf0/0xf1/0xf2 immediately produce digital silence; 0xf3..0xf9 and
# 0xfb..0xff leave the music unchanged.  0xfa performs a gradual native fade,
# but no 68K caller was observed and the CPS1 byte-latch pack protocol has no
# fade argument, so it remains pass-through pending a driven gameplay trace.
# A follow-up 0x0c -> 0xf0 -> 0x0c run proved that 0xf0 also starts nothing and
# leaves the driver able to start a later song.  It is therefore both the safe
# suppression substitute and an arranged-player STOP command.
PROTOCOLS["ghouls"] = dataclasses.replace(
    PROTOCOLS["sf2"], game_id="ghouls", handshake_ready=0xf0,
    control_verbs={0xf0: VERB_STOP, 0xf1: VERB_STOP, 0xf2: VERB_STOP})


# Final Fight 30th Anniversary CPS2 Edition (MiSTer set ffightae_cps2, hbmame
# ffightaec2) -- grego2d's CPS2 conversion, sound rebuilt on the SFA3 (sz3)
# QSound driver 1.71 (modified sz3.01 + sz3.11m).  Record layout measured
# (manifests/protocol/ffightaec2.json): the sfa3ud shape verbatim -- cmd at
# +0x01/+0x03, arg word +0x07/+0x09, +0x05 arg byte PRESENT, handshake +0x1f,
# record-then-handshake.  Controls MEASURED on this driver (ctrl_probe,
# 2026-09-01): 0xff00 stops the BGM and the driver survives it; 0xff05 does
# NOT stop music (coin-in SFX-slot release -- the sfz2al lesson, do not map);
# 0xff07 argb=0x00 fades the BGM out and 0xff06 argb=0xff holds unity, i.e.
# the fade target rides the record's own arg byte -> VERB_FADE_KEEP for both,
# the CONTROL_ALPHA2_ARCADE semantics.  Neither fade has been observed emitted
# by this 68K yet (attract/coin/gameplay traces show only ff00/ff05); the rows
# are correct if it ever does.  Fade LAW: family default FADE_ANTHOLOGY
# (0xffff/arg) -- not disassembled for 1.71, revisable if a fade trace ever
# warrants it.
PROTOCOLS["ffightae_cps2"] = _proto("ffightae_cps2", FADE_ANTHOLOGY, 0xffff, 0,
                                    CONTROL_ALPHA2_ARCADE, VERB_NONE)


# The only control code confirmed to mean the same thing on every QSound
# driver examined (1.00 .. 2.01b): stop the BGM.  Everything else differs by
# family -- 0xff06 fades IN on 1.04-2.01b and OUT on 1.00/1.01 -- so a game
# with no measurements of its own gets this and nothing more.
CONTROL_QSOUND_MINIMAL = {0xff00: VERB_STOP}


def generic_protocol(game_id: str) -> Protocol:
    """Fallback descriptor for games without Phase-0 data yet: family latch
    protocol, Anthology fade law, and only the control code whose meaning is
    common to every driver in the family.  A guessed stop/fade cuts the
    arranged track where the board keeps playing, so unknown codes pass
    through -- see CONTROL_QSOUND_MINIMAL."""
    return _proto(game_id, FADE_ANTHOLOGY, 0xffff, 0,
                  CONTROL_QSOUND_MINIMAL, VERB_NONE)


def get_protocol(game_id: str) -> Protocol:
    p = PROTOCOLS.get(game_id)
    return p if p else generic_protocol(game_id)


# --- zero1 (SFA1) verified command map ---------------------------------------
# internal research notes Q3: commands 0x41..0x61 -> Y_DATA entries
# 97..129, 0x68->130, 0x69->131; all volume 0x60; every other row in
# 0x001..0x07f is type 1 -> entry 0 (1 s silence = stop).
ZERO1_MUSIC_MAP = {0x41 + i: 97 + i for i in range(0x61 - 0x41 + 1)}
ZERO1_MUSIC_MAP.update({0x68: 130, 0x69: 131})
ZERO1_TABLE_OFFSET = 0xf0b0        # in alpha1_comp2.bin (US; JP identical)
ZERO1_CLEAN_ROWS = 0x800
ZERO1_TYPE_HISTOGRAM = {0: 1921, 1: 127}

# --- HSF2 AE dispatch table ----------------------------------------------------
HSF2_ELF_NAME = "SLPM_654.96"       # JP disc (SLPM-65496)
HSF2_ELF_MD5 = "18c61372fc86c57b60f5e62212aaf734"
HSF2_TABLE_OFFSET = 0x63a0f0        # ELF *file* offset of the cmd*8 table
# First 64 bytes of the verified dispatch table (row 0 guard + rows 1-7).
# Region ELFs relocate the table (EU SLES_524.44: +0x1200 vs JP) but carry it
# byte-identical; builders locate it by this signature
# when the ELF is not the verified JP one.  Unique in the JP ELF.
HSF2_TABLE_NEEDLE = bytes.fromhex(
    "0000000000000000010000000168000001000000026800000100000003 6c0000"
    "0100000004680000010000000568000001000000067000000100000007 680000"
    .replace(" ", ""))
HSF2_TABLE_ROWS = 0x500             # dispatcher bound: cmd < 0x500
HSF2_BANKS = {"arrange": 0x000, "cps2": 0x300, "cps1": 0x400}
HSF2_AFS_NAME = "HSF2.AFS"
HSF2_TYPE_BGM = 1

# --- sf2 (CPS1 World Warrior) verified command map -----------------------------
# internal research notes: the CPS1-SF2 stage-music table at 68K $6378
# is keyed by character; the HSF2 AE CPS1/Arrange banks are keyed by HSF2's OWN
# command numbers, so the mapping is a small static re-key (NOT identity — Ryu
# is 0x01 on CPS1-SF2 but 0x02 on HSF2, Guile 0x05 vs 0x07, Ken 0x04 vs 0x01).
# These are the values a CPS1-SF2 arranged pack keys as PLAY + suppress; every
# other latch value (SFX 0x21-0x32, voices 0x56-0x87) and the 0xf0/0xf7/0xff
# control family passes through to the Z80 untouched (join doc gating note).
SF2_STAGE_MUSIC = {          # cps1-sf2 cmd -> HSF2 arrange-bank cmd (fm_XX)
    0x01: 0x02,  # Ryu
    0x02: 0x03,  # E.Honda
    0x03: 0x05,  # Blanka
    0x05: 0x07,  # Guile
    0x04: 0x01,  # Ken
    0x06: 0x04,  # Chun-Li
    0x07: 0x06,  # Zangief
    0x08: 0x08,  # Dhalsim
    # bosses — IDENTITY by command (sound-test NCC + in-game nameplate + order-
    # table verified, 2026-07): 0x09 boxer, 0x0a claw, 0x0b Sagat, 0x0c dictator;
    # HSF2 fm_09..0c are JP-named, so cmd N -> fm_N is role-correct.
    0x09: 0x09, 0x0a: 0x0a, 0x0b: 0x0b, 0x0c: 0x0c,
    0x0d: 0x3b,  # bonus stage
}
SF2_ATTRACT_MUSIC = {0x16: 0x33, 0x0e: 0x34, 0x0f: 0x35, 0x14: 0x3a}
# title / select / VS / ranking.  0x0f (VS., 4.5 s) MUST have a row: an
# unmapped command neither stops the player nor gates the native chip, so the
# select theme keeps playing over the VS screen and both sound at once
# (confirmed on hardware).  A <10 s window files 0x0f as 'sfx' -- the same
# short-jingle blind spot that hides 0x14 (ranking).  A PLAY row fixes both halves: it replaces the select track and
# suppresses the native cue.
# Per-character endings.  Order is the SELECT-SCREEN reading order (Ryu E.Honda
# Blanka Guile / Ken Chun-Li Zangief Dhalsim), NOT the stage-theme order above --
# 0x04/0x05 swap Ken and Guile there.  Two orders coexist in this ROM; both are
# measured (align_ncc, r 0.38-0.52 over a 0.07-0.19 runner-up), so neither is a
# mistake to reconcile.
SF2_ENDING_MUSIC = {
    0x18: 0x23,  # Ryu
    0x19: 0x24,  # E.Honda
    0x1a: 0x27,  # Blanka
    0x1b: 0x29,  # Guile
    0x1c: 0x22,  # Ken
    0x1d: 0x25,  # Chun-Li      -- ear-confirmed; floors at 0.089 for the matcher
    0x1e: 0x28,  # Zangief
    0x1f: 0x2a,  # Dhalsim
    0x34: 0x21,  # Ken(2)
    0x35: 0x26,  # Chun-Li(2)   -- ear-confirmed; floors at 0.123 for the matcher
}
# Time-low "hurry up" stage variants.  Invisible until the 0x50 crowd loop was
# skipped in the sweep: certified by a perfect BIJECTION onto twelve distinct
# STAGE <name>(2) tracks, exactly the twelve SF2 characters, none of the four
# SSF2 newcomers or Gouki drawn from the sixteen on offer.
SF2_HURRY_MUSIC = {
    0x79: 0x12,  # Ryu          0x7f: Zangief
    0x7a: 0x13,  # E.Honda
    0x7b: 0x15,  # Blanka
    0x7c: 0x17,  # Guile
    0x7d: 0x11,  # Ken
    0x7e: 0x14,  # Chun-Li
    0x7f: 0x16,  # Zangief
    0x80: 0x18,  # Dhalsim
    0x81: 0x19,  # M.Bison
    0x82: 0x1a,  # Balrog
    0x83: 0x1b,  # Sagat
    0x84: 0x1c,  # Vega
}
SF2_CONTINUE_MUSIC = {0x11: 0x37}              # continue screen
# CE's credits roll.  Found by BEATING THE GAME (lua/sf2ce_beat.lua), not by
# sweep: after the ending, the board sends 0x8d and the music matches the
# album's STAFF ROLL at r=0.408 (everything else floors).  The driver is
# STATEFUL -- the same 0x8d injected on a cold boot plays a Ryu-flavoured
# variant instead (r=0.056 vs the real credits), which is why every sweep
# missed it.  0x8d fires exactly twice in a 2400 s double playthrough, both at
# credits, so the row cannot misfire in normal play.
SF2_CREDITS_MUSIC = {0x8d: 0x3c}               # credits roll -> STAFF ROLL
# NOT mapped, deliberately: 0x8e-0xab (the rest of the +0x8c shadow bank) never
# fire in real play -- a full double playthrough shows 0x8d and nothing else
# from that range -- and their cold-boot content is bank-dependent (see above),
# so rows for them would encode service-mode behaviour, not gameplay.  0x38 is
# garbage data, 0x50/0xde are crowd-cheer loops (both ear-verified), and 0x8c
# is CE's four-boss ending, which has no counterpart in the arrange album.
# Full set of CPS1-SF2 music commands (PLAY + suppress in the trigger table).
SF2_MUSIC_COMMANDS = (set(SF2_STAGE_MUSIC) | set(SF2_ATTRACT_MUSIC)
                      | set(SF2_ENDING_MUSIC) | set(SF2_HURRY_MUSIC)
                      | set(SF2_CONTINUE_MUSIC) | set(SF2_CREDITS_MUSIC))
SF2_CONTROL_BYTES = {0xf0, 0xf7, 0xff}         # boot / section-stop / idle
SF2_IDLE_BYTE = 0xff                           # terminator the tap substitutes
SF2_TRIGGER_ROWS = 256                         # CPS1 byte command -> 256 rows


def cps1_protocol() -> "Protocol":
    """The `ffight` descriptor if protocols.py ever grows one, else the tracked
    CPS1 byte-latch descriptor re-labelled.  Never generic_protocol() — that is
    the CPS2 QSound family and would write a CPS2 latch page into the header."""
    if "ffight" in PROTOCOLS:
        return PROTOCOLS["ffight"]
    proto = dataclasses.replace(PROTOCOLS["sf2"], game_id="ffight")
    # Validate on the LATCH PAGE, not on an empty control-verb map: sf2's
    # descriptor carries 0xf7=stop, the CPS1 section-stop every game in this
    # family uses, and the latch page is what actually distinguishes the CPS1
    # dialect from the CPS2 QSound one.
    if proto.latch_page != 0x800180:
        raise ValueError("sf2 descriptor is not the CPS1 byte latch")
    return proto
