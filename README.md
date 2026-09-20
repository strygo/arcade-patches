# arcade-patches

Static website for fan-made arcade game patches, published via GitHub Pages
from the `docs/` directory.

The site lists translation and restoration patches (currently the Darkstalkers
CPS-2 translations, plus in-development NAOMI and CPS-2 projects), with
before/after screenshots, download bundles, checksums, and apply instructions.

## Layout

- `data/patches.json` — site content plus one entry per patch. Entries with
  `"hidden": true` are kept but not built or listed.
- `data/releases.json` — append-only public release inventory. It pins the
  current version plus every retained historical download's filename, size,
  SHA-256 hash, and qualification/provenance state.
- `data/mra/` — vendored base MRAs from [jotego/jtbin](https://github.com/jotego/jtbin)
  (GPLv2, attribution headers preserved) that the MiSTer patch overlays are
  derived from.
- `tools/build.py` — verifies inventoried downloads and renders HTML into
  `docs/`.
- `tools/release_packager.py` — deterministic IPS/MRA packaging loaded only
  by Capcom's isolated candidate factory from a pinned commit; the site build
  never invokes it.
- `tools/import_release.py` — the only supported path from an immutable,
  qualified Capcom candidate into `docs/downloads` and `data/releases.json`.
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

No dependencies beyond Python 3.9+. The build verifies every current patch
download against `data/releases.json`, reads its embedded manifest for the
page, and fails if a file is missing or changed. It does not inspect Capcom
development outputs and cannot regenerate or replace a published patch kit.

The build requires every ZIP under `docs/downloads` to appear exactly once in
the inventory. Historical CPS+ versions retain their pre-boundary provenance;
the current version records its Capcom public-parity qualification.

Every download contains a `readme.txt` describing the project, the changes,
apply instructions, and legal notes. IPS bundles additionally contain a
checksum manifest and an `apply.py` that verifies every file before and after
patching. No ROM data is ever included.

## Importing a release

Capcom owns production and end-user QA. Prepare the page prose and set its new
version and date in `data/patches.json`, then import the exact candidate that
produced the `ready_for_import` record:

```bash
python3 tools/import_release.py \
  --ready ../capcom/release/validation/example.ready.json \
  --candidate ../capcom/path/to/out/releases/example/rc2
python3 tools/build.py
```

The importer checks that the readiness record, `release.json`, clean
reproduction, QA receipt, download set, and every download hash agree. It
requires the page to name the candidate's version, copies the files, verifies
them again, appends that version to `data/releases.json`, and makes it current.
Game releases are append-only. A qualified tooling revision may refresh an existing
filename while preserving the game version and every reconstructed game byte.

After GitHub Pages deploys the commit, verify that every hosted current
download is exactly the qualified file recorded in the inventory:

```bash
python3 tools/verify_hosted_releases.py
```

Because the hosted SHA-256 must equal the selected qualified revision's
SHA-256, this check binds the deployed download to
the reconstruction and runtime evidence in its readiness record.

Superseded kits stay in `docs/downloads` and in the version history under
`data/releases.json`. The page links only the inventory's current version.
Historical filenames therefore remain stable for external links.

## Updating kit tools without changing the game version

Use Capcom's `release/plan_tooling_revision.py` and tooling-parity qualification,
then import its readiness record with the same command above. Keep the page's
game version and the public download filename. The inventory records a separate
kit revision and tools date, with immutable copies under
`docs/downloads/archive/<sha256>/<filename>`. Existing entries migrate to
revision 1 without changing their bytes. Every original remains recoverable.

The importer requires the exact current baseline, the next revision number,
unchanged public names, clean reproduction and a matching game-content contract.
Promotion stages the replacements and uses an exclusive recovery journal. To
restore an interrupted promotion, run `python3 tools/import_release.py --recover`.
To select a previous kit revision, run:

```bash
python3 tools/import_release.py --rollback <kit> <game-version> <revision>
python3 tools/build.py
```

The whole revision history is retained. After deployment,
`python3 tools/verify_hosted_releases.py --archives` also checks the immutable
copies. The renderer's existing content-hash URL query updates automatically
when the stable filename's contents change.

The IPS, Gold and Final Fight EX kits share Capcom's `release/rom_sources.py`.
Refresh vendored copies with `release/sync_kit_tools.py --site <this-checkout>`
from Capcom; `--check` detects drift. ZIP, 7z and loose files may be merged,
split, nested or renamed, provided their contents match the required hashes.
Use repeatable `--rompath` and `--check` in the shipped appliers. New collection
workflows stage verified outputs without changing the source collection.

## Adding a patch

Create the project and candidate in Capcom first. Add its page content to
`data/patches.json`, then use the qualified-candidate import above. Entries
with `"artifact": null` and `"hidden": true` can still describe work that is
not yet published.

## Publishing

GitHub repository settings → Pages → Deploy from a branch → select the default
branch and the `/docs` folder. To show a "Source on GitHub" footer link, set
`site.repo_url` in `data/patches.json`.

## Preview locally

```bash
python3 -m http.server 8734 --directory docs
```
