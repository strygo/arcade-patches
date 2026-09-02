# CPS+ pack-embedded MRAs

Installable MRAs that load a CPS arcade ROM **plus** a CPS+ arranged-audio
pack into a `-cpsplus` core in one shot.

## Layout (mirrors dist `_Arcade/_CPS+/`)

```
CPS+/                 USA / World MRAs (19, no region suffix)
CPS+/Japan/           Japanese variants (20) -- named "(…, Japan)"
CPS+/Asia/            Asian variants (10) -- named "(…, Asia)"; includes
                      Zero 2 Alpha, whose sfz2al set is itself the Asian
                      release (no USA/World arcade release exists)
CPS+/Europe/          European variants (1) -- named "(…, Europe)"
```

Every MRA here is GENERATED -- `integration/gen_{root,regional,gold,backport}_mras.py`
cover all 52, and the tree rebuilds from empty.  Generating the root tier
(rather than hand-making it) keeps it reachable by tree-wide changes -- a
`<region>` sweep or a pack consolidation touches every MRA at once.

All MRAs are derived from jotego's official base MRAs (jtbin and its
`_alternatives` collection, GPLv2, attribution preserved) by embedding the
pack part and pointer patch.  One exception tier: `base_offset/` vendors a
base from the Arcade_Offset project instead (hack sets that never existed in
jtbin — currently the Final Fight 30th Anniversary CPS2 Edition, which
carries its own ffightae_cps2_arrange pack; see `base_offset/README.md` for
provenance).

**`<region>` is inherited verbatim and must not be rewritten.**  Every one of
the 40 bases states `World` -- jtbin, `_alternatives` and the Gold collection
alike, including the Japanese ones -- so it is boilerplate upstream, not
curated data, and it is not load-bearing (MiSTer resolves by `<setname>` and
the zip reference).  It looks wrong on a `(…, Japan)` MRA and it is tempting
to "fix"; doing so only makes these files diverge from the ones they are
derived from, for a field nothing reads, so it is left as inherited.  The bases and the generators live in the
private work repo; the built MRAs here are complete, installable artifacts
and need no regeneration.

**sfa2gold backport**: MiSTer support is the four 8 MiB expanded-audio
sets ONLY (`sfa2g`, `sfz2d`, `sfz2da`, `sfa2d` — generated from the
canonical backport MRAs).  The 4 MiB compatibility archives are for MAME
and original hardware and get NO MiSTer MRAs, by design.  Those four ROM
zips are hbmame sets and live in
**`games/hbmame/`**, not `games/mame/`; MiSTer searches both.

## What each MRA does

The pack is appended to the `<rom index="0">` image (padded to a 1 kB
boundary, then the `.cpk` as a final `<part>`), and a `<patch offset="8">`
writes the pack's 1 kB-unit offset into the image header's reserved
`FF FF` slot (bytes 8-9).  MiSTer assembles ROM+pack into DDR at
0x30000000; `cpsplus_ddr` reads bytes 8-9 to find the pack.  The base
MRA's `asm_md5` is dropped (the assembled image now includes the pack).
The arcade ROM zip is never modified — the `.cpk` comes from a separate
pack zip named by a per-part `zip=` attribute.

## Install (MiSTer)

1. MRAs go in `_Arcade/_CPS+/` (and `_Japan`/`_Asia`/`_Europe` beneath it);
   the **leading underscore** makes MiSTer show a folder as an arcade group.
2. Pack zips go in **`games/CPS+/`** (next to `games/mame/`).  MRAs
   reference them with the leading-slash games-root form
   (`zip="/CPS+/<pack>.zip"`).  Packs live under `games/CPS+/`; this is on
   the hardware pass checklist for re-verification.
3. Regional arcade ROM zips (`ffightj.zip`, `spf2xj.zip`, …) live in
   `games/mame/` as usual.
4. Requires a `-cpsplus` core build; toggle "Arranged audio" in the OSD.

Pack zips are stored uncompressed:
`(cd cpsplus/work/packs && zip -0 -X hsf2_arrange.zip hsf2_arrange.cpk)`

## Embed mechanics (for new games)

`embed_pack_mra.py <base.mra> <pack.cpk> --rom-len <Total> --pack-zip
/CPS+/<name.zip> --zip-name <pack.cpk> -o <out.mra>`

`--rom-len` is the `Total 0x… bytes` comment in the base MRA.  **The
leading MRA header is NOT in `Total`** — a jtcores image opens with an
inline header (64 B CPS1, 44 B CPS2) and jotego's region comments and
`Total` are measured *after* it.  `embed_pack_mra.py` measures the header
and adds it automatically, so always pass the raw `Total`.  Getting this
wrong puts the pack `hdr` bytes past its advertised offset: the loader
finds no `CP2A` and silently fails open to arcade audio (this shipped
once, and shows on hardware).  Invariant to check after
generating: `hdr + Total + pad == pointer * 1024`.

The pointer is only as good as the ROM length it came from, and a wrong one
fails in two ways at once: the core follows it, finds no `CP2A`, and shows the
MAGENTA "bad magic" square, *and* the loader streams the surplus bytes past the
last ROM region into the wrapping 8 kB QSound DSP firmware window, which kills
arcade audio too — a game that runs with no sound at all.  The classic
trigger is a ROM length that sums a member's whole file (e.g. `dl-1425.bin`'s
full size) instead of the `length` the MRA loads, putting the pointer kB past
the pack.  Recompute the invariant against the real ROM archives rather
than trusting a number typed by hand:

    hdr + Total + pad == pointer * 1024

`--rbf` must not be `<stock>_<suffix>`: MiSTer's `get_rbf()` treats `_` as
a version separator and the alphabetically-last match hijacks stock MRAs
(measured — `jtcps1_cpsplus.rbf` captured every stock `jtcps1` MRA).  The
`-cpsplus` hyphen naming avoids it.

## Size caveat (Phase-4 on-target check)

HSF2: 45 MB ROM + 185 MB pack ≈ 230 MB — near the assumed ~240 MB
FPGA-visible DDR budget (0x30000000 window).  The exact MiSTer-main size
cap for DDR-addressed images is research §4's open question; confirm on
hardware.  Everything else is far smaller and safe.
