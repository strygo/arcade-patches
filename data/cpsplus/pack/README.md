# pack/ — CPS2 arranged-audio pack toolchain

Builds, verifies, and auditions `.cpk` packs per `../PACK_FORMAT.md`
(§Binary layout v0 — `format.py` is the normative byte-level reference).
CLI entry point: `../build_pack.py`.  Python ≥ 3.11 plus ffmpeg on PATH
(ADX encode/decode and authoring-mode format conversion); numpy is needed
only by the builders that re-encode audio rather than copying it off the
disc.

## Automatic builders

```sh
# HSF2 AE (PS2 JP disc zip or extracted iso) -> Arrange / CPS2 / CPS1 bank
python3 build_pack.py hsf2 --iso "roms/ps2/Hyper Street Fighter II - The Anniversary Edition (Japan).zip"
python3 build_pack.py hsf2 --iso ... --bank cps1

# SFA1 from the Saturn disc
python3 build_pack.py sfa1-arrange --disc "roms/saturn/Street Fighter Alpha - Warriors' Dreams (USA).zip"

# Ghouls'n Ghosts / SF2CE / SSF2 from the X68000 external-MIDI renders
# (the FLAC set tools/export_x68k_midi_flac.py produces from your disks;
# the MiSTer kit's make_x68k_flac.py drives the whole chain)
python3 build_pack.py ghouls-x68k-midi --flac-dir <daimakaimura flac dir>
python3 build_pack.py sf2ce-x68k-midi --flac-dir <sf2ce flac dir>
python3 build_pack.py ssf2-x68k-midi --flac-dir <ssf2 flac dir>

# UN Squadron / Area 88 from the Capcom Music Generation Area 88 album rip
python3 build_pack.py unsquad-snes --ost "path/to/area_88_ost/"

# Final Fight 30th Anniversary CPS2 Edition from the US Final Fight CD rip
python3 build_pack.py ffightae-cps2 --us-disc "path/to/Final Fight CD (USA).cue"

# SFZ2 Saturn MUS -> sfz2al pack (both discs => includes the Cammy song)
python3 build_pack.py sfa2-arrange \
    --iso "roms/saturn/Street Fighter Zero 2 (Japan).zip" \
    --also-iso "roms/saturn/Street Fighter Zero 2' (Japan).zip"
```

Outputs land in `work/packs/<name>.cpk` (+ a `.cpk.json` provenance
sidecar).  Zip inputs are extracted once into `work/cache/` and reused, and
decoded audio is kept under `work/intermediate/`.  Those caches make re-runs
much faster but reach ~13 GB after a full build, so a successful
`make_packs.py` run deletes `work/` unless you pass `--cache`.

What each mode does:

* **hsf2** — walks the ISO, reads the boot-ELF dispatch table (file offset
  0x63a0f0, 8-byte rows keyed by arcade QSound command; bank offsets
  +0x000/+0x300/+0x400), and stores the AFS ADX streams verbatim (headers
  stripped, truncated at loop_end_byte).  Cross-checks the generated map
  against `manifests/hsf2_bgm_command_map.tsv` and fails on any mismatch.
* **sfa1-arrange** — Saturn CD-DA (whole soundtrack) + tracked trigger/loop
  manifests, against the zero1/comp2 dispatch table (+0xf0b0).
  **Trigger rows are keyed by ARCADE command = table_key − 0x38**: the arcade
  sfa/sfau never emits the Anthology table keys (traced, 8/8 join hits).  The
  generated table is validated against that trace inventory.
  **Music rows only** — the PS2 table's silence rows (type-1 entry 0, the
  "unmapped music command stops BGM" idiom) are NOT re-keyed: Phase 2
  proved arcade cmd 0x0006 (= silence key 0x3e) does not stop arcade music
 (gated-vs-control WAV difference), so those commands pass
  through unmapped; arcade stops arrive as 0xff00/0xff05 controls.
* **sfa2-arrange** — decodes the MUS streams (s16BE stereo 32 kHz blk4096,
  first block = Left), ADX-encodes at 32 kHz (exactly one lossy step),
  loop points = the intro/loop file boundaries (sample-exact), one-shot vs
  loop-forever from each disc's own 0.BIN control table (located by blind
  pattern search).  sfz2al trigger table is AUTO-derived from the zero6
  comp2 table joined via `adx_entry = 169 + saturn_song_id`
  (`manifests/sfz2_saturn_zero6_map.tsv`).  Other vocabularies need an
  explicit `--trigger-map` TSV (cmd \t song [\t gain]) from a Phase-0
  trace.

### Byte-exactness acceptance (hsf2 / sfa1)

After writing, the builder re-opens the pack and the source image
independently and (1) compares every track's bytes against the source AFS
entry's frame stream (header stripped, loop truncation applied) and (2)
ffmpeg-decodes 3 tracks both ways — pack stream under a synthesized v3
header vs the untouched source .adx — requiring sample-exact PCM equality.
`--no-crosscheck` skips this.  For sfa2-arrange (lossy encode) the check is
an NCC ≥ 0.90 decode-vs-source-PCM comparison instead.

## Audition and verify — the listening gate

```sh
python3 build_pack.py audition work/packs/hsf2_arrange.cpk --cmd 0x01 --loops 2
python3 build_pack.py audition work/packs/sfa1_arrange.cpk --track 0 --loops 3
python3 build_pack.py verify work/packs/hsf2_arrange.cpk   # --quick: no decode
```

`audition` renders intro + N loop passes to WAV exactly as the FPGA player
will wrap (byte-pointer reset ≡ PCM splice at the loop samples) — listen to
the seams.  `verify` checks structure (magic/CRCs/alignment/loop-field
coherence), reports pack size vs the ~240 MB DDR budget, and computes a
loop-seam discontinuity metric per looped track (joint step vs local step
scale; ratios ≲ a few = seam moves like the surrounding music).  The metric
is a *screening heuristic* — loops that land on a downbeat transient can
legitimately score 10-20 — the WAVs are the acceptance instrument.

## Community authoring

```sh
python3 build_pack.py init ssf2t my_ssf2t_pack/   # scaffold + command worksheet
# drop audio files in my_ssf2t_pack/audio/, edit pack.toml
python3 build_pack.py build my_ssf2t_pack/        # -> my_ssf2t_pack/ssf2t.cpk
```

`init` pre-fills the trigger worksheet from the game's known command
inventory (hsf2 / sfa1 / sfz2al today; other games get a blank worksheet
until their Phase-0 census lands).  pack.toml semantics: loop points as
integer samples or `"m:ss.mmm"` strings; `gain` linear (1.0 = unity);
`codec = "adx"` (default) or `"pcm"`; trigger values are a track name,
`"stop"`, or `{ track = "x", gain = 0.9, suppress = false }`.  ADX loop
points are rounded to 32-sample frames (warned when adjusted).

## Self test

```sh
python3 build_pack.py selftest
```

Round-trips the writer/reader on synthetic data, checks CRC corruption
detection, verifies the pure-Python reference ADX decoder is bit-exact
against ffmpeg, and validates audition loop assembly.

## Module map

| module | role |
|---|---|
| `format.py` | frozen binary layout v0 (writer/reader) — normative |
| `adxcodec.py` | ADX headers, coefficients, ffmpeg bridge, reference decoder |
| `protocols.py` | per-game protocol descriptors + verified command maps |
| `isofs.py` / `afs.py` / `mus.py` | ISO9660 (2048/2352), CRI AFS, Saturn MUS |
| `sources.py` | zip-member extraction cache |
| `build_hsf2.py` / `build_sfa1_arrange.py` / `build_sfa2_arrange.py` | automatic modes |
| `authoring.py` | init/build pack projects |
| `audition.py` | audition + verify |
| `selftest.py` | synthetic round-trip |

RTL-facing notes: ffmpeg decodes ADX with `scale` where the multimedia.cx
wiki (and reportedly CRI) uses `scale+1` — everything here is
ffmpeg-consistent; the Phase-3 decoder + testbench must pick one and be
compared against the same choice.  Track gain and trigger gain are both
linear /127 in v0; the PS2 used a dB curve (open item).
