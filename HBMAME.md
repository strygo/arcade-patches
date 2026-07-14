# HBMAME distribution process

How the arcade-patches translations get into [HBMAME](https://github.com/Robbbert/hbmame),
one game per commit/PR. This complements the site's IPS bundles and the MiSTer
MRA overlays: HBMAME carries only the *set definition* (filenames + checksums +
a GAME entry); no ROM data ever goes into the PR.

Repos involved:

- `../hbmame` — local clone of the fork `strygo/hbmame`; upstream is
  `Robbbert/hbmame` (actively merges small community PRs, e.g. #31, #34–#38).
- `../capcom` — work repo; source of the acceptance-tested patched zips.
- this repo — patch manifests (`data/generated/*.json`) with stock/patched CRCs.

## How HBMAME models a hack (verified 2026-07-13)

- CPS-2 hacks live in `src/hbmame/drivers/cps2mis.cpp`, which is textually
  `#include`d at the bottom of `src/hbmame/drivers/cps2.cpp` (same translation
  unit). No build-script (`.lua`) changes are needed to add a set there.
- Every runnable set must also be listed in `src/hbmame/hbmame.lst` under the
  `@source:cps2mis.cpp` section. **A new hack = exactly two files changed.**
- Setnames: `<parent>sNN`, zero-padded, sequential per parent
  (`vsav2s01`–`s03` exist; `vhunt2s01`, `vsav2s04` are the next free names).
  These are HBMAME-internal names — they intentionally differ from our MiSTer
  MRA setnames (`nwarr2en`, `vsav2en`).
- HBMAME renames CPS-2 ROMs to normalized chip labels (vhunt2 = `c75.*`,
  vsav2 = `c76.*`), not the mainline MAME names (`vh2j.*`, `vs2j.*`). Modified
  ROMs get the set suffix spliced into the chip label: `c75s01.p1`. Slot
  numbers follow **load offset**, not the mainline filename — match by CRC
  against the parent `ROM_START` in hbmame's `cps2.cpp`.
- The hack's `ROM_START` re-lists every unchanged parent ROM verbatim
  (same name/CRC/SHA1, including the gfx, audiocpu, qsound regions and the
  `ROM_REGION(0x20, "key", 0)` with the parent's `.key` file). Modified
  program ROMs stay in **encrypted** CPS-2 space; `init_cps2` decrypts at
  runtime with the parent key — exactly how our IPS/MRA patches already work.
  Do **not** use the `dead_cps2` (pre-decrypted "Phoenix") pattern.
- GAME line template (columns: year = hack release year, company = hack
  author credit, description = parent title + `(datecode+region, hack
  description, YYYY-MM-DD)`):

  ```c
  GAME( 2026, vhunt2s01, vhunt2, cps2, cps2_2p6b, cps2_state, init_cps2, ROT0,
        "strygo",
        "Night Warriors 2: Darkstalkers Revenge (970929J, English, 2026-MM-DD)",
        MACHINE_SUPPORTS_SAVE )
  ```

  Decided 2026-07-13: company/credit field is `strygo`; the NW2 description
  uses the translated title (`Night Warriors 2: ...`), keeping the parent's
  datecode so the lineage stays obvious.

  GAME lines sit under a `// <Game Name>` comment group; ROM_STARTs under a
  matching banner comment. Add next to the existing group for the parent, or
  create a new group in parent-alphabetical position.

## One-time setup

1. Fork + remotes (done: `origin` = strygo/hbmame, `upstream` = Robbbert/hbmame).
2. Keep `master` a clean mirror of upstream; never commit to it directly:
   `git fetch upstream && git checkout master && git merge --ff-only upstream/master && git push origin master`
3. One full build to prime the toolchain (slow; later incremental builds are
   fast): `make TARGET=hbmame SYMBOLS=0 NO_SYMBOLS=1 DEPRECATED=0 -j<cores>`

   `NOWERROR=1` is no longer required as of 2026-07-14 — our upstream PR #39
   ("Fix build with Clang 21") removed the stale warnings that `-Werror`
   turned into hard errors on current Apple clang. Keep the flag in mind if
   a future upstream merge introduces new warnings. Note for any make-option
   change: options like NOWERROR are genie parameters — add `REGENIE=1` the
   first time you set or change one, otherwise the cached project files keep
   the old flags.

## Per-game checklist

Prerequisite: the patch is acceptance-tested in `../capcom` and has a
`data/generated/<slug>.json` manifest here (stock + patched CRC32 per member).

1. **Reserve the set identity.** Find the parent's `ROM_START` in hbmame's
   `cps2.cpp`; note the chip label and match each of our modified ROMs to its
   slot by stock CRC. Pick the next free `sNN` suffix (grep `cps2mis.cpp` and
   `hbmame.lst`).
2. **Compute checksums** of the patched ROMs (CRC32 must match the manifest;
   SHA1 from the patched zip in `../capcom`).
3. **Branch**: `git checkout -b add/<setname> master` (synced with upstream first).
4. **Edit `src/hbmame/drivers/cps2mis.cpp`**: copy the parent's `ROM_START`
   block wholesale, rename it to `<setname>`, swap in the renamed+re-checksummed
   modified ROMs, keep everything else byte-identical to the parent block.
   Add the GAME line per the template above.
5. **Edit `src/hbmame/hbmame.lst`**: add `<setname>` under `@source:cps2mis.cpp`
   (alphabetical), and check the parent is listed under `@source:cps2.cpp` —
   parents are only listed once some hack needs them, so a first-ever hack of
   a game must add its parent too (vhunt2 wasn't listed until vhunt2s01;
   PR #31 likewise added `mmatrix`). `-validate` catches this:
   "clone of nonexistent driver".
6. **Build** (full target — see note below; incremental after the first build,
   so touching `cps2mis.cpp` only recompiles one translation unit + link):
   `make TARGET=hbmame SYMBOLS=0 NO_SYMBOLS=1 DEPRECATED=0 -j<cores>`

   Do **not** use `SOURCES=src/hbmame/drivers/cps2.cpp` — filtered builds are
   broken for the hbmame target (verified 2026-07-13: `scripts/build/makedep.py`
   hardcodes `src/mame` path components, so it finds no system drivers under
   `src/hbmame/` and genie aborts).
7. **Static verification** (no ROMs needed):
   - `./hbmame -validate` — driver validity pass (dup names, bad regions, clone links)
   - `./hbmame -listxml <setname>` and `-listroms <setname>` — confirm parent
     link, ROM names/CRCs, key region.
8. **Runtime verification.** Build a test clone zip containing only the
   renamed modified ROMs (e.g. `vhunt2s01.zip` with `c75s01.p1/.p2/.p6`),
   place it in the rompath next to the parent set, then
   `./hbmame <setname>` — verify clean boot (no bad-CRC warnings), title
   screen shows the translation, quick attract/gameplay sanity pass.
   Screenshot the title for the PR. (MAME's loader falls back to CRC-matching
   inside zips, so a mainline-named parent zip works for local testing.)
9. **Commit** the two files, subject in house style — `Added <setname>` —
   with a body naming the hack and the public patch page. Steve's rules
   (2026-07-13): no version strings (no "rc1") and no Co-Authored-By
   trailers in upstream-bound commits.

   ```
   Added vhunt2s01

   Night Warriors 2 - English translation of Vampire Hunter 2:
   Darkstalkers Revenge (970929J) by strygo. The patch (IPS against
   the stock vhunt2 set) is available at:
   https://strygo.github.io/arcade-patches/vhunt2-english/
   ```

10. **PR to `Robbbert/hbmame`** from the fork branch. Title = commit subject
    prefixed with the driver file (`cps2mis - Added <setname>`, matching
    merged PR #38); body: one-paragraph description of the hack, a note that
    patches are available at https://strygo.github.io/arcade-patches/ (plus
    the per-game page), what was verified (validate + verifyroms + boot),
    title screenshot (the site's Pages-hosted screenshots can be embedded
    directly). One game per PR.
11. **After merge**: sync `master`, delete the branch, and update the site —
    add the HBMAME setname to the patch entry / apply instructions so users
    know the IPS bundle output maps to `<setname>` in HBMAME.

## Version policy

HBMAME pins exact CRCs, so every patch version bump needs a follow-up PR
(upstream handles these routinely — "Updated sf2prime to 0.77").
Decided 2026-07-13: submitting at rc1 is fine; we send a CRC-update PR
whenever a patch version bumps.

## Ready-to-go queue

### vhunt2s01 — Night Warriors 2 (vhunt2-english rc1)

Merged 2026-07-14: https://github.com/Robbbert/hbmame/pull/40

Parent `vhunt2`, chip label `c75`, key `vhunt2.key` (verbatim from parent).
Patched zip: `../capcom/translations/vhunt2/out/vhunt2_combined_english/vhunt2.zip`

| mainline name | hbmame name | size | CRC32 | SHA1 |
|---|---|---|---|---|
| vh2j.03a | c75s01.p1 | 0x80000 | 1ce8d926 | 9352d3a2d7180f39b93df794822bc4609e8c3951 |
| vh2j.04a | c75s01.p2 | 0x80000 | 6b232eae | 46562af80f9824cccd27a9efab948519eae1bc1b |
| vh2j.08  | c75s01.p6 | 0x80000 | be9d8a6c | 5202c72df883ca41ec1abe8277bce9fc5b28eaaf |

Description: `"Night Warriors 2: Darkstalkers Revenge (970929J, English, <date>)"`.

### vsav2s04 — Vampire Savior 2 English (vsav2-english rc1)

Merged 2026-07-14: https://github.com/Robbbert/hbmame/pull/41

Parent `vsav2`, chip label `c76`, key `vsav2.key` (verbatim from parent).
Patched zip: `../capcom/translations/vsav2/out/narrative_english/vsav2.zip`

| mainline name | hbmame name | size | CRC32 | SHA1 |
|---|---|---|---|---|
| vs2j.03 | c76s04.p1 | 0x80000 | a211712c | 3f5c2e997b2a25e8b5ece4ac9354a1cef1986821 |
| vs2j.04 | c76s04.p2 | 0x80000 | 974896f9 | 42e715478c02fd70498834a9ea35c997dcf56efe |
| vs2j.08 | c76s04.p6 | 0x80000 | cb8b85c6 | aa7fee9409d5c4eec5ee7d2985d3871b3b853476 |

Description: `"Vampire Savior 2: The Lord of Vampire (970913J, English, <date>)"`.

## Scope: NAOMI / disc-based patches (settled 2026-07-13)

The pipeline above covers platforms the hbmame target already builds (CPS-2
today). The SFZ3U NAOMI patch does not fit as-is: HBMAME cannot run any
NAOMI/Atomiswave/Dreamcast game (0 of its 9,601 machines; no SH4/AICA/
PowerVR/GD-ROM devices in `scripts/target/hbmame/hbmame.lua`; no CHD-based
set has ever existed in HBMAME). The NAOMI *source* is present and current —
the fork merges mainline MAME continuously — so inclusion is a moderate
target expansion (enable the devices, wire in the naomi driver family, add
the `naomi` BIOS + stock `sfz3ugd` parent + our clone), not a port.

Why bother: HBMAME inclusion drives distribution — collectors mirror
dat-complete HBMAME sets, so listed sets propagate without anyone visiting
the patch site. That is the main reason the CPS-2 translations are there.

Plan: after the CPS-2 PRs merge (track record first), ask Robbbert directly
(issue or 1emulation forum) whether he'd take NAOMI hack sets, offering to do
all the wiring to his conventions with sfz3ugd multiregion as the pilot.
Note the ask honestly: first disc-era platform ever, and the dat would pull
the NAOMI BIOS + ~1GB stock parent CHD into collections as new dependencies.
If declined: publish our own clrmamepro-style dat for the patched sets as
the fallback distribution channel.

## Site follow-ups (per merged game)

- Add the HBMAME setname to the patch's `data/patches.json` entry (new field,
  rendered on the patch page alongside the MiSTer instructions).
- Optionally extend the IPS bundle's `apply.py` with an `--hbmame` mode that
  emits `<setname>.zip` with the renamed modified ROMs, so HBMAME users can
  build the clone set directly from a stock dump.
