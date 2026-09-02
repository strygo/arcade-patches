# Rebuilding the packs

Every shipped pack rebuilds **byte-identically** from (a) this repository and
(b) disc images or source renders you own.  Builders never search for or
name-match disc files — you pass each source path explicitly.  Keep a small wrapper script with
your local disc paths; this table is the reference.  Every pack builds through the ONE entry
point `cpsplus/build_pack.py` (the per-module CLIs still exist and take the
same flags).  Where a region column says **or**, the interchangeability was
measured — the alternative disc produces a byte-identical pack; everywhere
else the named disc is the one the shipped pack was byte-gated against.

Requirements: the repo venv (numpy), ffmpeg on PATH for ADX packs (or
`$CPSPLUS_FFMPEG`; NOTE pack ADX bytes depend on ffmpeg's adx encoder --
unchanged upstream for years, and a differing build fails the byte-gates
loudly rather than shipping different audio), and, only for `.7z` rips,
a 7-Zip binary (`brew install sevenzip`).

| Pack (dist zip) | Command (`python3 cpsplus/build_pack.py …`) | Source disc(s) you need |
|---|---|---|
| ffight_arrange | `ffight --variant adx --region us --us-disc <US rip>` | Final Fight CD (Sega CD) — USA only |
| ffightae_cps2_arrange | `ffightae-cps2` (same US disc / shared extraction cache) | Final Fight CD (Sega CD) — the 30th Anniversary CPS2 Edition re-key (`ffightae_cps2`, jtcps2): same 19 tracks and imported loop authoring, CPS2 0x1200-row table keyed at the hack's 0x01xx commands, JP-wrap opening on the song cue, measured gain 0x2d.  Deliberately its OWN pack (a shared CPS1+CPS2 pack was built and reverted — build_ffightae_cps2_arrange.py docstring).  MAME oracle: hbmame `ffightaec2` — built by renaming the MiSTer `ffightae_cps2.zip` parts per hbmame's ROM_START (p1←ff-23m.8h, p2←ff-22m.7h, c1..c4←ff-5m/7m/1m/3m, m1←sz3.01, q1←sz3.11m) plus stock `sz3.12m`→c78.q2, `sz3.02`→c78.m2 and `dl-1425.bin` from sfa3.zip |
| ffight_arrange_jp | `ffight --variant adx --region jp --jp-disc <JP rip>` | Final Fight CD (Sega CD) — Japan only.  One pack per region: each carries only the voiced-cutscene pair its own ROM emits (0x70/0x71 US, 0x72/0x73 JP).  Both gate only 0x52, the board's song — the phone ringing (0x35) and the click that answers it (0x36) pass through and are heard.  Each enters on the first cue its own ROM issues (US 0x35 at 26.9 s, Japan 0x52 at 12.7 s) and wraps the groove by however much lands the master's own outro on that ROM's title screen (US 10.24 s, Japan 2.88 s).  The two discs carry the same masters — measured r=1.0000 per music cue at a constant 2.7 ms offset, the JP rip simply trimming 1.99 s of trailing digital silence — so the packs differ in language and opening entry, not in the arrangement |
| ffight_snes / ffight_x68k_fm / ffight_x68k_midi | `ffight-ost --ost <dir>` | Final Fight OST box rip (.flac, discs 1–2) |
| ghouls_x68k_midi | `ghouls-x68k-midi --flac-dir <dir>` | The 27 stereo 48 kHz Daimakaimura FLACs produced by `tools/export_x68k_midi_flac.py`; embedded MIDI loop tags are checked against the tracked loop manifest |
| sf2ce_x68k_midi | `sf2ce-x68k-midi --flac-dir <dir>` | The 46 stereo 48 kHz SF2CE FLACs produced by `tools/export_x68k_midi_flac.py`; all command joins passed the two-file A/B gate, the measured SC-55 DC pedestal is validated and removed without changing timing, and embedded MIDI loop tags are checked against the tracked 37-loop manifest |
| ssf2_x68k_midi | `ssf2-x68k-midi --flac-dir <dir>` | The 63 stereo 48 kHz SSF2 FLACs produced by `tools/export_x68k_midi_flac.py`; all command joins passed the two-file A/B gate, the SC-55 DC/pre-roll cleanup is pinned, and embedded MIDI loop tags are checked against the tracked 46-loop manifest |
| unsquad_snes | `unsquad-snes --ost <dir>` | Capcom Music Generation Area 88 OST rip (.flac, 1 disc; the SFC score is tracks 28-49) — loop points come from the tracked `manifests/unsquad_snes_loops.tsv` (structure-first) |
| hsf2_arrange | `hsf2 --iso <AE disc>` | Hyper Street Fighter II AE (PS2) — Japan, Europe **or** the US *Street Fighter Anniversary Collection* (HSF2.AFS byte-identical on all three; the dispatch table is auto-located by signature — on the US collection it sits in the launcher ELF, with HSF2.AFS nested under /HYPER/ — and cross-checked; each builds the identical pack, measured) |
| hsf2_cps1 | `hsf2 --iso <AE disc> --bank cps1` | 〃 |
| sf2_arrange (+ sf2ce/sf2hf copies) | `sf2-arrange --iso <AE disc>` | 〃 |
| ssf2_arrange / ssf2t_arrange | `ssf2-arrange --game ssf2\|ssf2t --iso <AE disc>` | 〃 |
| sfa1_arrange | `sfa1-arrange --disc <Saturn rip>` | Saturn SF Alpha: Warriors' Dreams (USA, byte-exact) **or** SF Zero (Japan, audio-equivalent: same masters at a ≤5-sample shift with +27 ms pads — a JP-disc build passed the split-window seam gate at r=+0.9994 with zero joint shift, just not byte-identical). The EU pressing has an irregular per-track cut (offsets that even onset anchoring cannot recover, pads up to +17 s) and is NOT supported |
| sfa2_snes | `sfa2-snes --iso <Anthology iso>` | 〃 |
| sfa2_arrange | `sfa2-arrange --game sfa2 --iso <rip>` | Any of: SF Zero 2 (Japan), SF Alpha 2 (USA), SF Alpha 2 (Europe), SF Zero 2' (Japan), SF Collection Disc 2 (Europe) — the base 59 songs are byte-identical on all five Saturn carriers; Gold carriers' extra Cammy song is excluded for this pack (measured, each builds the identical pack) |
| sfz2al_arrange | `sfa2-arrange --iso <rip>` | Any Saturn carrier of Alpha 2 Gold: SF Zero 2' (Japan), or SF Collection Disc 2 (Europe / Japan / USA-Saturn) — all four carry the 60 songs byte-identical (measured). Saturn only — PSX SF Collection rips (a different audio engine) are rejected with a clear error; in this library the USA-Saturn rip is the "(Disc 2) (1)" variant |
| spf2t_arrange | `spf2t --disc <rip>` | Super Puzzle Fighter II Turbo (Saturn) — USA, Europe **or** X (Japan): all 23 bank-B songs byte-identical on all three, each builds the identical pack (measured) |
| mtwins_arrange | `mtwins --disc <rip>` | Chiki Chiki Boys (PCE CD — Japan-only release) |
| forgottn_arrange | `forgottn --disc <rip>` | Forgotten Worlds (PCE CD) — USA **or** Japan (the two discs differ only on tracks the pack does not use; either builds the identical pack, measured) |
| mbomber_arrange | `mbomber --disc <rip>` | Muscle Bomber (FM Towns, .mds/.mdf — Japan-only release) |

Disc arguments accept a `.cue` (or `.mds`), or a `.zip`/`.7z` containing
exactly one.  CD-DA extraction is raw sectors cut at cue boundaries — nothing
is trimmed, faded or resampled -- with one deliberate, documented exception
class: Final Fight's two voiced ending cues (0x71 US, 0x73 JP) are lead-in
padded and spliced to the arcade ending's length, because the cutscene ROMs'
caption and lip timings are measured against the processed tracks
(`ARCADE_CUT` in `pack/build_ffight_arrange.py`; fixed parameters and a pinned
output hash, so the pack stays byte-reproducible and the transform cannot
silently drift).  Two boundary conventions exist and are
explicit (`--pregap trim|keep` on `tools/extract_cdda.py`): Final Fight rips
cut at INDEX 01, PCE rips keep each track's own pregap (whole redump bin
verbatim), the FM Towns `.mds` path drops each 2.00 s inter-track gap.  Every
convention was chosen by byte-comparing against the shipped, ear-approved
packs — all 105 cached source tracks reproduce exactly.

Extraction caches live under `work/` (`work/listening/*/cd_full`,
`work/discs/`) and are safe to delete; builders refill them from the disc
arguments.  Trigger maps, loop points and protocol descriptors are tracked
manifests in `manifests/` — no game data is required beyond the discs.

MRAs embed only a pack POINTER (1 kB-aligned offset + pad), never the pack
length, so pack rebuilds never require MRA regeneration
(`integration/embed_pack_mra.py`).

## Gates

Run both after any pack rebuild; each exits non-zero on failure.

```bash
python3 cpsplus/integration/verify_dist.py
```

Checks that everything staged under `dist/mister_sd` still matches the
canonical packs — each pack ZIP byte-for-byte against `work/packs/<name>.cpk`,
each cue sheet against a freshly generated one, and that no canonical pack is
missing a ZIP.  Nothing regenerates `dist/` automatically, so it drifts
silently; `--fix` regenerates whatever is stale.  This matters most for the
cue sheets, which are the *tester's* reference during a hardware pass — a stale
one does not fail loudly, it quietly describes cues the pack no longer has.
(When this gate was written, `sfa1_arrange.txt` still claimed 13 cues against a
37-cue pack, and the three ffight OST sheets predated the one-shot opening fix.)

```bash
python3 cpsplus/integration/verify_mra_pointer.py --rom-dir <dir> [--rom-dir <dir> …]
```

Recomputes each MRA's assembled image from the real ROM archives and checks the
embedded pack pointer against it.  Sets whose archives are absent are reported
UNVERIFIED, never OK.
