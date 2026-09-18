# arcade-patches

Static website for fan-made arcade game patches, published via GitHub Pages
from the `docs/` directory.

The site lists translation and restoration patches (currently the Darkstalkers
CPS-2 translations, plus in-development NAOMI and CPS-2 projects), with
before/after screenshots, download bundles, checksums, and apply instructions.

## Layout

- `data/patches.json` — all site content: site config plus one entry per patch
  (status, version, description, changes, screenshot sources, artifact
  sources, MiSTer MRA config). Entries with `"hidden": true` are kept but not
  built or listed.
- `data/mra/` — vendored base MRAs from [jotego/jtbin](https://github.com/jotego/jtbin)
  (GPLv2, attribution headers preserved) that the MiSTer patch overlays are
  derived from.
- `data/generated/` — per-patch member manifests captured at build time, so the
  site can be rebuilt without the work repo present.
- `tools/build.py` — the whole build: generates patch bundles and MiSTer MRAs,
  copies screenshots, renders HTML into `docs/`.
- `tools/ipsutil.py` — IPS encoder/decoder (has a self-test: `python3 tools/ipsutil.py`).
- `tools/mra.py` — MiSTer MRA assembler (faithful port of Main_MiSTer's
  loader) and patch-overlay generator.
- `tools/chdpatch.py` — data-free CHD patch generator for GD-ROM games: it
  locates the byte-level change between two dumps via chdman extraction, and
  the shipped apply script replays it against the user's own dump.
- `tools/bundle_apply.py` — the standalone `apply.py` shipped inside every
  IPS bundle.
- `tools/bundle_apply_chd.py` — the standalone `apply.py` shipped inside CHD
  patch downloads (drives the user's chdman; verifies before and after).
- `data/screenshots/` — screenshots captured for the site that have no home
  in the work repo (e.g. region-lock proof shots).
- `site/style.css` — the stylesheet, copied into `docs/` at build time.
- `docs/` — generated output, committed, served by GitHub Pages.

## Building

```bash
python3 tools/build.py
```

No dependencies beyond Python 3.9+.

Patch downloads and screenshots are sourced from the sibling work repo
(`../capcom`) using the paths recorded in `data/patches.json`. When those
sources are present, everything is regenerated and **round-trip verified**,
or the build fails:

- IPS bundles: the patches applied to the stock romset must reproduce the
  acceptance-tested build byte-for-byte.
- MiSTer MRAs: the stock romset assembly must match the base MRA's published
  `asm_md5`, and the generated patch-overlay MRA assembled over the stock set
  must equal the official MRA assembled over the patched set, byte-for-byte.
- CHD patches: the shipped apply script is run for real against the stock
  dump (chdman extract → patch → rebuild) and the result must carry the
  patched CHD's SHA1. Regeneration is cached on the CHDs' header SHA1s, since
  extraction takes minutes; `chdman` must be on PATH only when the source
  CHDs actually change.

When the sources are absent, the previously generated downloads, images, and
manifests are reused, so the site still rebuilds from this repository alone.

Every download contains a `readme.txt` describing the project, the changes,
apply instructions, and legal notes. IPS bundles additionally contain a
checksum manifest and an `apply.py` that verifies every file before and after
patching. No ROM data is ever included.

## Versioning

Patch versions (currently `rc1`) are set manually in `data/patches.json` and
are **only bumped on explicit instruction**. Re-running the build regenerates
the current version in place from the latest work-repo outputs — this is the
normal workflow while a release candidate is being finalized.

Superseded kits stay in `docs/downloads`. When a version is bumped, the new zip
is added alongside the old one and the old one is **not** deleted: romhacking.net
entries, forum posts and bookmarks link the exact filename of the version they
were written against, so removing it breaks every one of those links. The pages
only ever link the current version, so an old zip is invisible on the site and
costs nothing but disk.

## Adding a patch

Add an entry to `data/patches.json` (copy an existing one), point
`artifact.stock_zip` / `artifact.patched_zip` at the stock and patched MAME
zips, list screenshot source paths, and run the build. Entries with
`"artifact": null` render as download-less status pages (for in-development
work).

## Publishing

GitHub repository settings → Pages → Deploy from a branch → select the default
branch and the `/docs` folder. To show a "Source on GitHub" footer link, set
`site.repo_url` in `data/patches.json`.

## Preview locally

```bash
python3 -m http.server 8734 --directory docs
```
