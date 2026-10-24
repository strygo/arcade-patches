# Repository agent guidance

Other sessions often leave uncommitted edits in this checkout. Run
`git status` before you start, and stage only the files your task owns.

## Commits on master

All work is on `master`, and other sessions commit to it while you work.

- Before `--amend`, a fixup or a rebase, check `git log --oneline -3`. Never
  amend or fix up a commit your task didn't make; HEAD may now be someone
  else's.
- Name page commits `<slug>: <version> page (...)`. They fold into the final
  `<slug>: <version>` at import.
- Page commits change `data/` (and the kit tooling a release needs), never
  `docs/`. Only the import commit carries the rebuilt `docs/`.
- Between a page commit and its import, `tools/build.py` stops at the release
  inventory ("page version rcN != inventory current rcM"). That is expected;
  never edit `data/releases.json` by hand to make it pass.
- When you commit a Capcom plan, put its packager commit on
  `keep/<slug>-<version>-candidate<N>-packager` straight away. Any later
  rewrite, yours or another track's fold, would otherwise orphan the commit
  the plan pins. Keep refs stay local.
- Keep releases in order directly on top of the published tip
  (`origin/master`), so each can be pushed on its own with
  `git push origin <sha>:master`. To fold a release whose commits are
  separated by another track's unpushed commits, rebase those commits on top
  of the folded release. That is allowed only when their trees are otherwise
  unchanged (check with `git diff <old> <new>`) and every commit their plans
  pin is already on a `keep/` ref. Report the new hashes to Steve so the
  other session can be told.
- Never rewrite pushed commits.

## Release candidates: the page is part of the kit

Capcom's [RELEASING.md](../capcom/release/RELEASING.md) walks the whole process,
page to push, with the exact commands. The sections below are the rules it
refers to.

Capcom's candidate factory (`../capcom/release/CANDIDATES.md`) packages each
download from a **pinned, committed** revision of this repository.
`tools/release_packager.py` copies these `data/patches.json` fields into the
`readme.txt` of every download, and into the MRAs and manifest:

`title`, `subtitle`, `version`, `date`, `game`, `set`, `hardware`,
`description`, `changes`, `patch_noun`, `patch_kind`, `patch_intro`, `mra`,
`artifact`, `hbmame`, `mame_build`, `mister_hbmame`

If any of these change after a plan pins the commit, you need a new candidate.
The page-only fields (`summary`, `release_history`, `screenshots`,
`comparison`, `related`, `sort_title`) never reach a download. Finish them before planning
anyway: the import commits whatever the page says.

### 1. Prepare the page, then commit it

Write for players: say what the player gets, never how it was made, reviewed
or verified. The description and the changes list describe the game, not the
release process. Words like "reviewed English", "checked against the
script", "approved", "source-matched" and internal data terms ("records",
"slots", "donors") are process language; say "win quotes", "follows the
Japanese script". Keep it short: one `changes` line per feature (what it is,
plus the input if there is one) and short screenshot captions. A how-to with
controls and rules belongs in the kit README, not the page. Public text never
mentions:

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
- Weigh an update against the whole game. The `release_history` entry is
  where an rc's fixes and additions go. Don't rebuild the summary, the
  description or the screenshots around them. A bug fix changes the body only
  if the body was wrong. A small addition gets one `changes` line and at most a
  clause in the description; it gets a screenshot or summary mention only if
  it matters to someone choosing the game. The page should read the same as if
  the feature had been there from the first release.
- Add one `release_history` entry for the new version, shaped
  `{"version": ..., "date": ..., "items": [...]}` like the others, first in
  the list and matching the page's `version` and `date`. List only the
  public-facing changes since the last **public** version. Leave out bugs that
  existed only in candidates, MRA folder moves and low-level detail.
- Check every claim against the project's own docs in `../capcom` (for
  example, `translations/ssf2x/enhanced/OPERATOR_SETTINGS.md`), not against
  commit subjects. SSF2 EX rc4 candidate 1 described a BONUS STAGE on/off
  operator setting as a "pace" option. The docs are the source for facts, not
  wording: they are written for developers, so restate each claim in a
  player's words.
- A page needs screenshots before import (a hidden page may wait until then):
  natural gameplay, no boot or identity screens, and short captions.
- Re-read the existing `changes`, `comparison` and screenshot captions against
  the new behavior. A caption describing something the release changed must
  change too. SSF2 EX rc4's bonus stages started following the Turbo speed,
  so "at the original Super Street Fighter II speed" had to go.
- Set `version` and `date`, keep `"hidden": true` on a brand-new page, then
  commit. The plan pins that commit.
- `python3 tools/build.py` validates every page's shape first, hidden pages
  included (`tools/page_schema.py`), before it renders anything. A hidden page
  is not rendered, so the validator is the only build check it gets before
  import unhides it.

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
explains what the importer verifies. Import one kit at a time and keep any
other new page hidden until its own import: every unhidden page needs a
release, and a failed import can leave a stray `docs/downloads/archive/`
folder. Fold the page-preparation commits and the import into one commit
named `<slug>: <version>` (see [Commits on master](#commits-on-master)). The
Capcom plan pins the packager commit, which stays on its `keep/` ref. Push
only with Steve's OK, and only the commits he named.
After Pages deploys, run `python3 tools/verify_hosted_releases.py`.

A release that changes game bytes usually has follow-ups. Check whether CPS+
MRAs pin the old CRCs, and whether the HBMAME set definitions need new
checksums.
