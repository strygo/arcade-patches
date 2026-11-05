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
- `site/logo.png`, `favicon.*`, `apple-touch-icon.png` — the AP emblem cut to
  size by `tools/make_logo.py` from `site/src/ap-emblem.png`; copied likewise.
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
version and date in `data/patches.json` **before** Capcom plans the candidate.
The description and changes list are copied into every download's readme. See
[AGENTS.md](AGENTS.md) for the page rules and the candidate checklist. Then
import the exact candidate that produced the `ready_for_import` record:

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
revision 1 without changing their bytes. Original archives remain hash-verifiable.

The importer requires the exact current baseline, the next revision number,
unchanged public names, clean reproduction and a matching game-content contract.
Promotion stages the replacements and uses an exclusive recovery journal. To
restore an interrupted promotion, run `python3 tools/import_release.py --recover`.
Updates move forward through newly qualified revisions; there is no command to
select an earlier publication. Archived downloads remain available for provenance.

Published download records are retained for provenance. After deployment,
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

## Tools and the ArcadePatches catalog

`data/tools.json` supplies the Tools landing page, home-page section and launcher
page. Tools have their own inventory, `data/tool-releases.json`; they are not
ROM candidates and do not alter the game-release inventory. Terminal screenshots
are captures of real launcher prompts, rendered from a PTY transcript. The
recipe is `release/collection/capture_terminal.py` in the Capcom repository;
`data/screenshots/arcadepatches/capture.json` records their identities.

Prepare the launcher ZIP with Capcom's `release/collection/package_launcher.py`.
Prepare a complete catalog with `release/collection/build_catalog.py`, pinning a
**qualified site inventory commit**, and qualify its metadata with
`release/collection/qualify_release.py`. Run Capcom producers through its storage
wrapper as documented in that repository. Then import here:

```sh
python3 tools/import_tool.py --archive <ArcadePatches-0.1.zip> \
  --receipt <package-receipt.json> --catalog <prepared-catalog-directory> \
  --qualification <catalog-qualification.json>
python3 tools/build.py
```

A newer launcher alone (no catalog change) uses the launcher-only import. It
checks the package receipt, requires a higher version that keeps every catalog
feature, publishes the ZIP under its own hash, sets the tool page's version and
date, and adds the catalog's `launcher` field with a new generation. Launchers
from 0.1.1 on read that field and name the newer download; game imports keep it.

```sh
python3 tools/import_tool.py --launcher-only --date <YYYY-MM-DD> \
  --archive <ArcadePatches-X.Y.Z.zip> --receipt <package-receipt.json>
python3 tools/build.py --render-only arcadepatches
```

The importer verifies the ZIP receipt and catalog qualification, preflights all
dependency hashes, and reuses existing game downloads by hash. New helpers,
adapters and wheels go under `docs/catalog/v1/artifacts/`. The launcher ZIP goes
under `docs/tools/downloads/archive/<sha256>/`, separate from the strict game
inventory; its selected path is recorded in `data/tool-releases.json`. The
catalog's live pointer is `docs/catalog/v1/index.json`; the default launcher URL
must be deployed in the same publication as the initial tool download. The site
build verifies the complete tool inventory and download closure.

The public index uses schema 2: discovery metadata and hash-pinned definition
references. `tools/catalog_index.py` extracts native `collection.json` files
byte-for-byte from their qualified kits into `catalog/v1/definitions/<sha>.json`.
These sidecars are generated delivery copies. The remaining adapter families
use generated `adapter-release-v1` sidecars. Neither has a separately authored
public recipe. The full schema 1 model is an internal qualification input;
`import_tool.py` converts it automatically after validating its receipt.
The launcher verifies and caches definitions before presenting prerequisites,
without fetching kit ZIPs, and reuses them offline.


All 14 IPS projects use native kit definitions. Their `collection_api: 1` page
metadata makes normal kit packaging generate `collection.json`. Qualified
`tools/import_release.py` imports refresh that project's catalog entry and
increment the generation automatically. `--recover` reconciles an interrupted
kit/catalog transaction. Native tooling revisions must increase; the importer
stores only the selected collection definition and creates no revision-selection
snapshots.

Other producer families retain their current adapters and need an explicit
catalog refresh until migrated. Keep the launcher ZIP unchanged when its code
and instructions have not changed. Retain published content-addressed artifacts
for cached catalogs; the removed IPS adapter ZIPs were never published.

To render imported pages while unrelated page drafts remain pending, use
`tools/build.py --render-only <comma-separated game slugs>,arcadepatches`.
This preserves other pages and site-wide indexes. Full site generation still
requires all page versions to match the release inventory.

After deployment, run `python3 tools/verify_tool.py --base-url https://arcadepatches.com/`
to verify the catalog, launcher and every referenced dependency over HTTP. For
staging, pass the local server URL instead. This supplements the game-only
`verify_hosted_releases.py`; neither runs game builders.

The initial Capcom qualifier deliberately pins 26 projects/293 targets. New
integrations need an updated acceptance contract; they do not inherently need
a new launcher.

Publishing remains a separate, explicitly authorized push.
