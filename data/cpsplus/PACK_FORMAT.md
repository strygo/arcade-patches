# CPS+ pack format (draft v0)

One file per game version, consumed FPGA-side with no ARM
involvement. Delivery: appended to the MRA ROM image (DDR-resident at
0x30000000 via the existing CPS fast-load); alternative `F`-load path later.
Worked example = HSF2 AE
(`manifests/hsf2_bgm_command_map.tsv`).

## Layout

```
┌─────────────────────────────────────────────────────────────┐
│ 1. HEADER (4 KB, fixed)                                     │
│    magic "CP2A" · format version · game id                  │
│    section offsets/sizes · pack rate · codec                │
│    PROTOCOL DESCRIPTOR (per-game contract knobs):           │
│      latch page (0x618000) · cmd byte offsets (+0x01/+0x03) │
│      arg word offsets (+0x07/+0x09) · handshake (+0x1f,     │
│      pending=0x00, ready=0xff)                              │
│      fade law id + constants (HSF2: 0x444/arg × 60 steps;   │
│      Anthology-family: 0xffff/arg per frame)                │
│      control-verb map (ff00=stop, ff06=fade, ...)           │
├─────────────────────────────────────────────────────────────┤
│ 2. TRIGGER TABLE (direct-mapped, one row per command,       │
│    0x000..0x11FF × 4 B ≈ 18 KB → BRAM at boot)              │
│    row = { verb (none/play/stop), track#, gain,             │
│            suppress-from-Z80 flag }                         │
├─────────────────────────────────────────────────────────────┤
│ 3. TRACK INDEX (32 B/track → BRAM at boot)                  │
│    DDR byte offset · length · loop_start_sample/byte ·      │
│    loop_end_sample/byte · channels · gain ·                 │
│    ADX predictor coefficients c1/c2 (precomputed —          │
│    RTL never parses stream headers)                         │
├─────────────────────────────────────────────────────────────┤
│ 4. TRACK DATA — raw ADX frame streams, headers stripped     │
│    (HSF2 Arrange: 181 MB; SFA1 Anthology: ~188 MB)          │
└─────────────────────────────────────────────────────────────┘
```

## Binary layout v0 (frozen)

Implemented by `pack/format.py` (writer + reader — that file is the
normative reference; this section mirrors it).  File extension `.cpk` ("CPS+ pack").
**All integers little-endian.**  A `<pack>.cpk.json` sidecar (build
provenance, track names/sources) accompanies every built pack; the binary
is self-sufficient without it.

```
0x0000  HEADER            4096 B fixed
0x1000  TRIGGER TABLE     trigger_rows × 4 B   (default 0x1200 rows = 18 KB)
......  TRACK INDEX       track_count × 32 B
......  TRACK DATA        64 B-aligned per track
```

### Header

| off | type | field |
|---|---|---|
| 0x000 | 4s  | magic `"CP2A"` |
| 0x004 | u16 | format_version = 1 (0 = pre-crossfade; readers accept ≤ current) |
| 0x006 | u16 | header_size = 4096 |
| 0x008 | 16s | game id, NUL-padded ASCII (`"hsf2"`, `"sfa1"`, `"sfz2al"`…) |
| 0x018 | 64s | title, NUL-padded UTF-8 |
| 0x058 | u16 | builder_version |
| 0x05a | u16 | default sample rate (informational; tracks carry their own) |
| 0x05c | u32 | trigger table offset |
| 0x060 | u32 | trigger table rows |
| 0x064 | u32 | track index offset |
| 0x068 | u32 | track count |
| 0x06c | u64 | track data offset |
| 0x074 | u64 | track data size |
| 0x07c | u64 | total file size |
| 0x084 | u32 | crc32(trigger table ‖ track index) |
| 0x088 | u32 | crc32(track data section, incl. alignment padding) |
| 0x08c | u32 | protocol: latch page (0x618000) |
| 0x090 | u8×6 | record byte offsets: cmd_hi (0x01), cmd_lo (0x03), arg_hi (0x07), arg_lo (0x09), arg_byte (0x05; **0 = field not present**, e.g. HSF2 1.06b), handshake (0x1f) |
| 0x096 | u8,u8 | handshake pending (0x00), ready (0xff) |
| 0x098 | u8  | fade law: 0 none · 1 Anthology (steps = const1/arg per frame) · 2 HSF2 (steps = const1/arg × const2) |
| 0x099 | u8  | control default verb (unmatched cmd ≥ control region start) |
| 0x09a | u16 | control region start (0xff00) |
| 0x09c | u32 | fade const1 (Anthology 0xffff · HSF2 0x444) |
| 0x0a0 | u32 | fade const2 (HSF2 60) |
| 0x0a4 | u32 | control verb count N (≤ 32) |
| 0x0a8 | N × {u16 cmd, u8 verb, u8 0} | control verb map (≤ 32 → ends by 0x128) |
| 0x128 | u16 | **v1** global loop crossfade length `xfade_samples` (0 = none) |
| ... | — | zero fill to 0x1000 |

**v1 loop crossfade** (`format_version` 1).  `xfade_samples` is the single
crossfade length (in samples, a multiple of 32) shared by every crossfade
track in the pack; it MUST equal the RTL player `XFADE_N` parameter, which
the fitter builds the equal-power weight LUT for.  Per-track opt-in is the
track-index crossfade-enable bit (below).  A v0 reader sees this region as
zero (`xfade_samples` = 0 = no crossfade); a v1 pack with no crossfade track
is byte-identical to its v0 form except for `format_version`.

Verbs: 0 none · 1 play · 2 stop · 3 fade-out (loop off; Anthology 0xff06) ·
4 fade-keep-loop (0xff07 / HSF2 0xff06) · 5 restore volume (0xff0c) ·
6 master fade (0xff0d).  Fade target/speed come from the command's own
argument bytes at runtime; the pack only carries the law + constants.

### Trigger row (4 B, direct-mapped by 16-bit command)

| byte | field |
|---|---|
| 0 | verb (0 none / 1 play / 2 stop) |
| 1 | track index low byte |
| 2 | gain, linear 0..0x7f (0x7f = unity) |
| 3 | bit0 = suppress-from-Z80 (gate the handshake write); bits4-7 = track index high nibble (12-bit track index total) |

### Track index entry (32 B)

| off | type | field |
|---|---|---|
| 0x00 | u32 | data offset (relative to track data section; 64 B aligned) |
| 0x04 | u32 | data length in bytes |
| 0x08 | u32 | loop start sample |
| 0x0c | u32 | loop start byte (relative to track stream; ADX frame-aligned) |
| 0x10 | u32 | loop end sample |
| 0x14 | u32 | loop end byte — **0 = track does not loop** |
| 0x18 | u16 | sample rate (Hz) |
| 0x1a | u8  | bits0-3 channels (1/2) · bit4 codec (0 = ADX frames, 1 = PCM s16le) · bit5 **v1** loop crossfade enable · bits6-7 **loop_count** (0 = loop forever; 1-3 = wrap N times then play THROUGH loop_end to the stream end -- finite-count tracks store the WHOLE stream, and the crossfade blend is gated off on the final pass) |
| 0x1b | u8  | track gain, linear 0..0x7f (effective gain = trigger gain × track gain) |
| 0x1c | s16 | ADX predictor coef1 (0 for PCM) |
| 0x1e | s16 | ADX predictor coef2 |

Loop semantics: the player wraps its read pointer after consuming
`loop_end_byte` bytes and continues at `loop_start_byte`, restoring the
ADX predictor history latched when it first crossed `loop_start_byte`.
The byte fields are the authoritative wrap points; the sample fields are
their sample-domain mirror (start exact; end may differ by <32 samples for
CRI-header imports, where loop_end is mid-frame).  ADX streams are stored
with container headers stripped and truncated at loop_end_byte for looped
tracks (validated safe — playback never reaches past the loop end).

### Loop crossfade (v1)

A track with the crossfade-enable bit (0x1a bit5) is stored **byte-exact**
but is NOT truncated at loop_end: it retains `xfade_samples` (header 0x128)
of the natural continuation past loop_end — the "tail" — so the stored
stream is `loop_end_byte + tail_bytes` long, where `tail_bytes =
xfade_samples/32 × 18 × channels`.  loop_start/loop_end stay the
good-continuity musical points (their raw hard-cut joint is intentionally
NOT click-free; the crossfade is what smooths it).

The player makes the loop seamless at PLAYBACK — Capcom's original ADX
frames are never re-encoded:

1. It decodes linearly through loop_end into the tail
   [loop_end, loop_end+N), then wraps the read pointer to loop_start+N.
   The ADX predictor snapshot/restore point moves to loop_start+N, so the
   wrapped decode is bit-identical to a linear decode; the steady-state loop
   period stays exactly loop_end − loop_start.
2. On the first pass it captures the first N decoded samples of the loop
   body [loop_start, loop_start+N) (the "head"), then blends each tail
   sample against the matching head sample with an equal-power weighting:
   `out[k] = w_out(k)·tail[k] + w_in(k)·head[k]`, k = 0..N−1, with
   `w_out(k) = cos((k+0.5)/N·π/2)`, `w_in(k) = sin(…)` (Q15).

The reference software model is `pack/xfade.py` (used by `audition` and by
the RTL testbench golden); the FPGA implementation in
`rtl/cpsplus_player.v` matches it bit-for-bit.  Requirements: the track must
loop, loop_end − loop_start > N, and `xfade_samples > 0`.  Crossfade-disabled
tracks are the plain hard-cut loop above, bit-identical to v0.
Coefficients are the ffmpeg `lrint` variants of the 500 Hz-cutoff formula
(reference/ffmpeg/adx.c); note ffmpeg decodes with `scale`, the
multimedia.cx wiki with `scale+1` — the RTL decoder must pick one and be
tested against the same choice (≤1 LSB per residual step).

## Runtime contract (summary)

Sniffer latches record bytes as the 68K writes the QSound latch; on the
handshake write it looks up the 16-bit command in the trigger table.
`play` row → gate the handshake write (suppress flag), start the indexed
track: DDR burst reads → ADX decode (two-tap predictor; predictor state
latched at first loop-point crossing, restored on each wrap at
loop_end_byte → loop_start_byte). Control commands (≥0xff00) use the
header's verb map and fade law; they are NOT suppressed (Z80-side behavior
on them is correct for SFX). Non-matching commands pass through untouched.
OSD toggle off / no pack = core behaves stock.

## Builder modes (implemented — `pack/README.md` is the manual)

- `build_pack.py hsf2 --iso <HSF2 AE zip/iso> [--bank arrange|cps2|cps1]` —
  fully automatic (AFS walk + ELF table 0x63a0f0; generated map
  cross-checked against `manifests/hsf2_bgm_command_map.tsv`; every track
  byte-exact vs the AFS source).
- `build_pack.py zero1 --iso <SF Alpha Anthology iso>` — fully automatic
  (comp2 table + Y_DATA ADX; byte-exact).  **Trigger rows keyed by ARCADE
  command = comp2 table key − 0x38** (Phase-0 sfau finding:
  `manifests/protocol/sfau.json` known_table_crosscheck, 8/8 join hits;
  the arcade never emits the raw Anthology keys 0x41-0x69).
- `build_pack.py saturn-mus --iso <SFZ2 zip> [--also-iso <SFZ2' zip>]` —
  MUS pairs decoded (s16BE stereo 32kHz, 4096-byte block de-interleave)
  + ADX-encoded; loop points from file boundaries; one-shot/loop-forever
  flags from each disc's 0.BIN control table (blind-search located).  The
  sfz2al trigger table is AUTO-derived from the zero6 comp2 table joined
  via `adx_entry = 169 + saturn_song_id`
  (`manifests/sfz2_saturn_zero6_map.tsv`); other MUS games (sfz3, spf2t,
  base sfz2/sfa2 vocabulary) need `--trigger-map <tsv>` from a Phase-0
  command trace.
- `--audio-override <track>=<file.wav>` — per-track swap for fan/OCR packs,
  keeping the game's trigger table.  (Planned; today: authoring mode.)

### Enhancement modes (upgrade imperfect sources — planned, not yet built)

- `--rate-from <ps1 chd/xa>` — optional bandwidth upgrade: substitute a PS1
  XA render (stereo 37.8kHz) of the same performance, cut at the
  Saturn-derived loop points (SFZ2, SPF2T candidates; Saturn MUS is already
  stereo 32kHz, so this is a fidelity choice, not a necessity);
  cross-correlation alignment + per-track QA report.
- `--find-loops` — autocorrelation loop-point recovery for hard-cut renders
  containing unrolled repeats (e.g. X-Men COTA Saturn's uniform 211s
  tracks); emits confidence per track, refuses below threshold.
- `--trim-fades` — best-effort looping of fade-master sources: detect fade
  onset, search for a loop join before it; always flagged in the manifest
  as approximate.

## Authoring (community packs)

Anyone should be able to build a pack for ANY CPS2 game from their own
audio — including original arrangements for games with no official source.
The unit of authoring is a **pack project**: a directory of ordinary audio
files plus one human-editable manifest.

```
mypack/
  pack.toml
  audio/ryu_stage.flac
  audio/ken_stage.flac
  ...
```

```toml
[pack]
game    = "ssf2t"          # selects protocol descriptor + command inventory
title   = "My SSF2T arrange"
author  = "..."

[tracks.ryu_stage]
file        = "audio/ryu_stage.flac"   # any rate/channels; builder converts
loop        = true
loop_start  = "1:23.456"               # time or samples; omit = whole-file loop
gain        = 0.9

[triggers]
0x03 = "ryu_stage"        # command -> track (template pre-fills labels)
0x13 = { track = "ryu_stage_critical", note = "low-health variant" }
```

Workflow:
1. `build_pack.py init ssf2t` — emits a template `pack.toml` from the
   game's Phase-0 command inventory: every observed music command with its
   context label ("attract", "Ryu stage", "continue"...), plus the
   control-verb map, already filled in. The author maps commands to files
   instead of reverse-engineering anything.
2. Fill in audio + loop points (`--find-loops` can propose them).
3. `build_pack.py build mypack/` → `ssf2t.cpk` + a validation report
   (unmapped in-game commands, loop-seam checks, size vs DDR budget).
4. `build_pack.py audition mypack/ --route <mame trace>` — renders the pack
   against a recorded command trace to WAV for listening before any
   hardware.

The per-game protocol descriptors and labeled command inventories ship with
the tooling (generated by the Phase-0 catalog census), so authoring never
requires binary analysis — the hard part of a community pack is the music,
as it should be.

## Design principles

- Game-specific behavior is data, not logic: one RTL implementation, new
  game = new pack.
- Per-row suppress flag lets packs replace music only (SFX/voices stay on
  real QSound).
- All loop metadata normalized to samples+bytes at build time; the player
  does no header parsing.

## Open format questions

- ~~Exact row/field bit packing~~ — frozen in §Binary layout v0 for the
  toolchain; Phase 3 may still revise for RTL convenience (would bump the
  format version, builder regenerates).
- ~~Codec field~~ — per-track codec nibble with PCM s16le escape hatch,
  implemented.
- ADX decode `scale` vs `scale+1` (ffmpeg vs multimedia.cx/CRI): toolchain
  is ffmpeg-consistent end-to-end; Phase 3 RTL must pick one and be tested
  against the same choice (≤1 LSB difference per residual step).
- Gain law: v0 stores linear /127 (trigger × track); the PS2 maps volume
  bytes through a dB table — revisit if arranged-vs-SFX balance sounds off.
- Whether the trigger table needs address-qualified matching for games
  whose drain routines use different record layouts (none seen yet:
  1.05A/1.06b verified identical field offsets).
- Multi-pack selection UX (e.g. SFA2: Anthology arrangement vs Saturn 1996
  arrangement) — OSD or filename convention.
