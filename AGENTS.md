# Repository agent guidance

Other sessions often leave uncommitted edits in this checkout. Run
`git status` before you start, and stage only the files your task owns.

## Release candidates: the page is part of the kit

Capcom's candidate factory (`../capcom/release/CANDIDATES.md`) packages each
download from a **pinned, committed** revision of this repository.
`tools/release_packager.py` copies these `data/patches.json` fields into the
`readme.txt` of every download, and into the MRAs and manifest:

`title`, `subtitle`, `version`, `date`, `game`, `set`, `hardware`,
`description`, `changes`, `patch_noun`, `patch_kind`, `patch_intro`, `mra`,
`artifact`, `hbmame`, `mame_build`, `mister_hbmame`

If any of these change after a plan pins the commit, you need a new candidate.
The page-only fields (`summary`, `release_history`, `screenshots`,
`comparison`, `related`) never reach a download. Finish them before planning
anyway: the import commits whatever the page says.

### 1. Prepare the page, then commit it

Write for players. The description and the changes list describe the game,
not the release process. Public text never mentions:

- where, how or whether a build was tested, or what testing is still to come
  (MiSTer, hardware checks, MAME passes, "machine verified"). Steve handles QA
  internally. Saying where the game *runs* is fine ("It runs in MAME and
  HBMAME and on MiSTer").
- internal status: candidate numbers, pending approvals, "needs a check".
  For an rc, "This is a release candidate." is the whole status statement.
- arcade boards, or claims about a board's own ROM layout.
- boot, info or identity screens, boot dates (such as `261001`), the R after a
  boot date, or restoration/EX staff-roll credits. Screenshots never show
  them either.

Also:

- `subtitle` is a plain kind label ("Enhanced version", "English
  restoration", "Arcade backport"), never a tagline.
- Add one `release_history` entry for the new version. List only the
  public-facing changes since the last **public** version. Leave out bugs that
  existed only in candidates, MRA folder moves and low-level detail.
- Check every claim against the project's own docs in `../capcom` (for
  example, `translations/ssf2x/enhanced/OPERATOR_SETTINGS.md`), not against
  commit subjects. SSF2 EX rc4 candidate 1 described a BONUS STAGE on/off
  operator setting as a "pace" option.
- Re-read the existing `changes`, `comparison` and screenshot captions against
  the new behavior. A caption describing something the release changed must
  change too. SSF2 EX rc4's bonus stages started following the Turbo speed,
  so "at the original Super Street Fighter II speed" had to go.
- Set `version` and `date`, keep `"hidden": true` on a brand-new page, then
  commit. The plan pins that commit.

### 2. Check the built candidate before the long gates

Right after `pipeline.py build`, and before QA:

```bash
unzip -p <candidate>/downloads/<kit>-<version>-ips.zip readme.txt
cd tools && python3 -c "import sys, content_audit; [content_audit.check(p) for p in sys.argv[1:]]; print('audit clean')" <candidate>/downloads/*.zip
```

Read the whole readme the way a player would. The importer runs the same
content audit, and a failure there means rebuilding the candidate.

To fix anything, correct the page, commit it, and plan the next candidate
(`<version>-candidate.<N+1>`). Never edit a built candidate. A readme-only
respin copies the previous plan and changes only `candidate`,
`sources.arcade_patches` and `handoff`. The Capcom source and the tested
archives stay the same. Then compare the two candidates' downloads: only
`readme.txt` should differ.

### 3. Hardware review is Steve's

QA, the runtime gate and a clean reproduction are automated ("machine
verified"). None of them satisfies the `hardware-review` gate. Never run
`pipeline.py approve` because automated checks passed. Record the approval
only after Steve says in chat that he tested the build, or let him run
`approve` himself. The note says what he reported and which archive hashes
it covers. A readme-only respin of archives he already tested can carry his
report, as SSF2 EX rc3 candidate 4 and rc4 candidate 3 did.

### 4. Import, commit and publish

```bash
python3 ../capcom/release/pipeline.py check-ready ... --record ../capcom/release/manifests/<kit>/<version>.json
python3 tools/import_release.py --ready ../capcom/release/manifests/<kit>/<version>.json --candidate <candidate>
python3 tools/build.py
```

Run the `check-ready` step from `../capcom`; [README.md](README.md#importing-a-release)
explains what the importer verifies. Fold the page-preparation commits and the
import into one commit named `<slug>: <version>`. The Capcom plan pins the
packager commit, so keep it reachable on
`keep/<slug>-<version>-candidate<N>-packager`. Push only with Steve's OK.
After Pages deploys, run `python3 tools/verify_hosted_releases.py`.

A release that changes game bytes usually has follow-ups. Check whether CPS+
MRAs pin the old CRCs, and whether the HBMAME set definitions need new
checksums.
