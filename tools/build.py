#!/usr/bin/env python3
"""Build the static site into docs/ from data/patches.json.

- Generates distributable IPS patch bundles by diffing stock vs patched romset
  zips found in the sibling work repo (../capcom). Every bundle is round-trip
  verified: stock + IPS must reproduce the patched bytes exactly.
- Copies curated screenshots into docs/img/.
- Renders all HTML pages.

The build degrades gracefully when the work repo is absent: previously
generated bundles, screenshots, and member manifests (data/generated/) are
reused, so the site can be rebuilt from this repository alone.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
import zlib
from html import escape as esc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import chdpatch
import ipsutil
import mra as mralib

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
GENERATED = ROOT / "data" / "generated"

# Set from the site config at the start of main(); used by readme/MRA credits.
SITE: dict = {}
# `--only slug,...`: rebuild just these from their sources.  Every other page
# reuses its committed downloads and images, so cutting one release never
# drags in another project's newer work-repo output.
ONLY: set = set()


def live(slug: str) -> bool:
    return not ONLY or slug in ONLY


def author_line() -> str:
    """One-line author credit, e.g. 'Steve Gordon (https://x.com/strygo)'."""
    name = SITE.get("author")
    if not name:
        return ""
    url = SITE.get("author_url")
    return f"{name} ({url})" if url else name

STATUS_LABELS = {
    "released": "Released",
    "release-candidate": "Release candidate",
    "beta": "Beta",
    "coming-soon": "Coming soon",
    "in-development": "In development",
    "research": "Research",
}


def resolve(path: str) -> Path:
    """Repo-relative, ~-relative, or $-prefixed.

    A leading $VAR is expanded from the environment, so build-time inputs
    that live OUTSIDE this repo -- the CPS+ kit zip is built in the private
    tree -- can be named without an absolute path to one machine.
    """
    # ${VAR:-fallback} first: os.path.expandvars leaves it untouched, so a
    # path written that way never resolved and the caller quietly used
    # whatever stale copy was already in place.
    def _default(m):
        return os.environ.get(m.group(1)) or m.group(2)
    path = re.sub(r"\$\{(\w+):-([^}]*)\}", _default, path)
    path = os.path.expandvars(path)
    p = Path(path).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def crc32(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


# ------------------------------------------------------------- downloads


def make_readme(patch: dict, members: list, fmt: str,
                variants: list | None = None) -> str:
    """Project readme.txt shipped inside every download (fmt: 'ips' or 'mra').

    `variants` (regional builds sharing one stock set) adds a --variant step
    and lists each build's changed files."""
    noun = patch_noun(patch)
    title = f"{patch['title']} · {patch['subtitle']}"
    lines = [
        title,
        "=" * len(title),
        "",
        f"Version:  {patch['version']} ({patch['date']})",
        f"Target:   MAME set '{patch['set']}' ({patch['game']})",
        f"Hardware: {patch['hardware']}",
    ]
    if author_line():
        lines.append(f"Patch by: {author_line()}")
    if SITE.get("site_url"):
        lines.append(f"Website:  {SITE['site_url']}")
    lines += [
        "",
        "ABOUT THIS PROJECT",
        "",
    ]
    for p in patch["description"]:
        lines += [textwrap.fill(p, 78), ""]
    if patch.get("changes"):
        lines += ["WHAT'S CHANGED", ""]
        for c in patch["changes"]:
            lines += textwrap.wrap(c, 78, initial_indent="  - ", subsequent_indent="    ")
        lines.append("")

    setname = patch["set"]
    if fmt == "chd":
        chd = patch["chd_patch"]["chd"]
        lines += [
            "WHAT THIS DOWNLOAD IS",
            "",
            "This patch contains no game data at all. It records which few bytes of",
            "the disc image change, and the apply script uses your own copy of",
            "chdman (which ships with MAME) to unpack your dump, patch it, and",
            "rebuild it. Every step is checksum-verified: if your dump is not the",
            "expected original, or the rebuilt image does not verify, nothing is",
            "kept.",
            "",
            "REQUIREMENTS",
            "",
            f"  - your own dump of the game: {setname}/{chd} (plus {setname}.zip)",
            "  - Python 3.8+",
            "  - chdman, from any reasonably recent MAME",
            "  - about 2.5 GB of free temporary disk space",
            "",
            "HOW TO APPLY",
            "",
            f"    python3 apply.py /path/to/{setname}/{chd}",
            "",
            f"This writes a verified {chd.replace('.chd', '-patched.chd')}. Rename it",
            f"to {chd} inside an {setname}/ folder placed ahead of the stock set in",
            "your MAME rompath. MAME reports a checksum warning for the patched",
            "CHD; that is expected and the game runs on Japanese, USA, and Export",
            "BIOS regions.",
        ]
    elif fmt == "ips":
        lines += ips_readme_lines(patch, members, variants)
    elif variants:
        lines += ["MISTER SETUP", ""]
        zip_dir = (patch["mra"].get("zip_dir") or "").strip("/")
        if zip_dir:
            top = zip_dir.split("/")[0]
            lines.append(f"1. Copy the {top}/ folder from this download to the root of your MiSTer SD")
            lines.append(f"   card (the MRAs land in {zip_dir}/), or just the MRAs you want:")
        else:
            lines.append("1. Copy the MRA for the build you want anywhere under _Arcade/ on your MiSTer:")
        for v in variants:
            rel = "/".join(x for x in (v["mra"].get("subdir", ""), v["mra"]["filename"]) if x)
            lines.append(f"       \"{rel}\"")
        lines += [
            f"2. Have the stock, unmodified romset at games/mame/{setname}.zip",
            "   (split MAME sets also need qsound.zip next to it).",
            "3. You need Jotego's jtcps2 core; the standard MiSTer downloader /",
            "   update_all installs it automatically.",
            "",
            f"Each MRA references your original romset and applies the {noun} in",
            "memory while the game loads. Nothing on your SD card is modified. Each",
            "build keeps its own settings and saves under its own setname:",
        ]
        for v in variants:
            lines.append(f"    {v['label']:16} {v['mra']['setname']}")
    else:
        mra_cfg = patch["mra"]
        zip_dir = (mra_cfg.get("zip_dir") or "").strip("/")
        lines += ["MISTER SETUP", ""]
        if zip_dir:
            lines += [
                f"1. Copy the {zip_dir.split('/')[0]}/ folder from this download to the root of your",
                f"   MiSTer SD card (the MRA lands in {zip_dir}/), or copy",
                f"   \"{mra_cfg['filename']}\" anywhere under _Arcade/.",
            ]
        else:
            lines.append(f"1. Copy \"{mra_cfg['filename']}\" anywhere under _Arcade/ on your MiSTer.")
        lines += [
            f"2. Have the stock, unmodified romset at games/mame/{setname}.zip",
            "   (split MAME sets also need qsound.zip next to it).",
            "3. You need Jotego's jtcps2 core; the standard MiSTer downloader /",
            "   update_all installs it automatically.",
            "",
            f"The MRA references your original romset and applies the {noun} in",
            "memory while the game loads. Nothing on your SD card is modified. The",
            f"{noun} keeps its own settings and saves under the name",
            f"'{mra_cfg['setname']}'.",
        ]

    lines += [
        "",
        "LEGAL",
        "",
        "This is a free, unofficial fan patch. It is not affiliated with or endorsed",
        "by Capcom. All game titles, characters, and artwork remain the property of",
        "their respective owners. You must own the game to use this patch. Do not",
        "sell this patch or distribute it applied to a ROM image.",
    ]
    return "\n".join(lines) + "\n"


def mra_meta(patch: dict, mra_cfg: dict) -> dict:
    """Organizer fields for this build: entry-wide values, region per build."""
    meta = {k: v for k, v in patch["mra"].items() if k in mralib.META_FIELDS}
    meta.update({k: v for k, v in mra_cfg.items() if k in mralib.META_FIELDS})
    return meta


def build_outputs(patch: dict, variants: list | None) -> list:
    """What `apply.py --out-dir out` writes, per build: (label or None,
    mame path or None, hbmame setname or None, mister MRA path or None).
    A patch with mame_build: false (SSF2 EX, whose added program ROM no stock
    MAME driver loads) ships no MAME set at all."""
    setname = patch["set"]
    builds = variants or [{"key": None, "label": None, "hbmame": patch.get("hbmame"),
                           "mra": patch.get("mra") if patch.get("mra", {}).get("filename") else None}]
    out = []
    for v in builds:
        mame = None
        if patch.get("mame_build", True):
            mame = f"mame/{v['key']}/{setname}.zip" if v["key"] else f"mame/{setname}.zip"
        mra = f"mister/{mra_zip_path(patch, v['mra'])}" if v.get("mra") else None
        out.append((v["label"], mame, (v.get("hbmame") or {}).get("setname"), mra))
    return out


def member_line(m: dict) -> str:
    if m["action"] == "add":
        return f"    {m['name']}  ({human_size(m['size'])})  new file, CRC32 {m['patched_crc32']}"
    return (f"    {m['name']}  ({human_size(m['size'])})  "
            f"CRC32 {m['stock_crc32']} -> {m['patched_crc32']}")


def ips_readme_lines(patch: dict, members: list, variants: list | None) -> list:
    """The IPS download's readme body: one apply.py run writes the MAME,
    HBMAME and MiSTer files for every build."""
    noun = patch_noun(patch)
    setname = patch["set"]
    outputs = build_outputs(patch, variants)
    mame_build = patch.get("mame_build", True)
    lines = []
    if variants:
        lines += ["BUILDS IN THIS DOWNLOAD", ""]
        lines += [f"    {v['key']:10} {v['label']}" for v in variants]
        lines.append("")
    lines += [
        "HOW TO APPLY (recommended)",
        "",
        f"    python3 apply.py /path/to/{setname}.zip --out-dir out",
        "",
        "This checks every file against the original, applies the patches, checks",
    ]
    if variants:
        lines += ["the result, and writes everything you need for every build (add",
                  "--variant <build> to make just one):"]
    else:
        lines += ["the result, and writes everything you need for each platform:"]
    lines.append("")
    for label, mame, hb, mra in outputs:
        pad = "    " if label else "  "
        if label:
            lines.append(f"  {label}:")
        if mame:
            lines.append(f"{pad}MAME / original hardware: out/{mame}")
        if hb:
            lines.append(f"{pad}HBMAME:                   out/hbmame/{hb}.zip")
        if mra:
            lines.append(f"{pad}MiSTer:                   out/{mra}")
    if mame_build:
        lines += [
            "",
            f"MAME: put {setname}.zip from out/mame/ ahead of the stock set in your MAME",
            "rompath" + (" (one build at a time: they share the set name)." if variants else "."),
            "MAME reports checksum warnings for the patched ROMs; that's expected and",
            "the game runs normally. The same files can be burned for an original board.",
        ]
    else:
        lines += [
            "",
            "There is no MAME build: stock MAME has no driver that loads the added",
            "program ROM. On a computer, play it in HBMAME.",
        ]
    hb_sets = [o[2] for o in outputs if o[2]]
    if hb_sets:
        hb = patch.get("hbmame") or next(v["hbmame"] for v in variants if v.get("hbmame"))
        if hb.get("pr_url"):
            lines += ["", f"HBMAME: this {noun} is an official HBMAME set ({', '.join(hb_sets)}),",
                      "so full HBMAME collections may already carry it."]
        else:
            lines += ["", f"HBMAME: the set definition ({', '.join(hb_sets)}) ships with the project;",
                      "an upstream HBMAME submission is pending."]
        lines += [f"Put the zip in HBMAME's roms/ folder next to your stock {setname}.zip.",
                  "It loads with no checksum warnings."]
    if any(o[3] for o in outputs):
        lines += [
            "",
            "MiSTer: copy the _Arcade folder from out/mister/ to the root of your SD card",
            f"and keep the stock, unmodified romset at games/mame/{setname}.zip (split MAME",
            f"sets also need qsound.zip). The MRA applies the {noun} in memory as the",
            "game loads, so nothing on the card is modified. You need Jotego's jtcps2",
            "core, which update_all installs. The MRAs are also a separate download.",
        ]
    ips_dir = "ips/<build>/" if variants else "ips/"
    if mame_build:
        # Re-zipping the patched files IS a MAME build, so a patch without
        # one doesn't offer the by-hand route either.
        lines += [
            "",
            "HOW TO APPLY (any IPS patcher)",
            "",
            f"Extract {setname}.zip, apply each file in {ips_dir} to the ROM file of the",
            "same name (Flips, Lunar IPS, or any IPS tool), then re-zip everything",
            f"as {setname}.zip.",
        ]
    lines += ["", "CHANGED FILES", ""]
    if variants:
        for v in variants:
            lines.append(f"  {v['label']} (--variant {v['key']}):")
            lines += [member_line(m) for m in v["members"] if m["action"] != "copy"]
            lines.append("")
    else:
        lines += [member_line(m) for m in members if m["action"] != "copy"]
        lines.append("")
    lines.append("All other files in the set are unmodified.")
    return lines


def zip_writer(out_path: Path, stamp: tuple):
    """Deterministic zip member writer."""

    def write(z: zipfile.ZipFile, name: str, data: bytes) -> None:
        info = zipfile.ZipInfo(name, date_time=stamp)
        info.external_attr = 0o644 << 16
        z.writestr(info, data, zipfile.ZIP_DEFLATED, 9)

    return write


def patch_noun(patch: dict) -> str:
    """How the patch refers to itself in prose: 'translation' unless the
    entry says otherwise (a restoration, an edition)."""
    return patch.get("patch_noun", "translation")


def mra_header_note(patch: dict, mra_cfg: dict | None = None) -> str:
    mra_cfg = mra_cfg or patch["mra"]
    kind = patch.get("patch_kind", "English translation patch")
    intro = patch.get("patch_intro") or f"An unofficial fan translation of {patch['game']}."
    credit = f"\n    Patch by {author_line()}.\n" if author_line() else ""
    return f"""    {patch['title']}, {kind} ({patch['version']})
    {intro}
{credit}
    This is a patch-overlay MRA: it references the ORIGINAL, unmodified
    MAME romset ({patch['set']}.zip) and applies the {patch_noun(patch)} in memory
    while the game loads. It contains no ROM data. Settings and saves use
    the name '{mra_cfg['setname']}'.

    Base MRA and jtcps2 core by Jose Tejada (jotego); see his header below.
    Not affiliated with or endorsed by Capcom. Free patch; do not sell."""


def mra_zip_path(patch: dict, mra_cfg: dict) -> str:
    """Where an MRA sits inside its download: the SD-card folder the patch
    names (e.g. _Arcade/_Translations), a per-build regional subfolder
    (e.g. _Japan), then the file."""
    parts = [(patch.get("mra") or {}).get("zip_dir", ""), mra_cfg.get("subdir", ""), mra_cfg["filename"]]
    return "/".join(x.strip("/") for x in parts if x and x.strip("/"))


def generate_mras(patch: dict, stock_path: Path, entries: list) -> list:
    """Generate and verify the MiSTer patch-overlay MRAs.

    `entries` is a list of (mra_cfg, patched_path, members): one overlay per
    patched set, all built on the base MRA named in patch["mra"]["base"].
    Returns (path inside the download, text, patch runs) per overlay."""
    base_text = resolve(patch["mra"]["base"]).read_text()
    stock_src = mralib.ZipSource([stock_path])
    stock_rom = mralib.assemble(base_text, stock_src)
    declared = mralib.declared_asm_md5(base_text)
    if declared and hashlib.md5(stock_rom).hexdigest() != declared:
        raise SystemExit(f"{patch['slug']}: stock assembly does not match base MRA asm_md5")

    texts = []
    for mra_cfg, patched_path, members in entries:
        overlay_base = target_base = base_text
        added = [(m["name"], m["patched_crc32"], m["size"]) for m in members if m["action"] == "add"]
        if added:
            # Program ROMs the stock set lacks: the target layout names them;
            # the overlay reserves the same space with a filler and patches
            # the bytes in, so it still needs only the stock set.
            after = patch["mra"].get("insert_after")
            if not after:
                raise SystemExit(f"{patch['slug']}: added ROMs need mra.insert_after")
            target_base = mralib.insert_program_parts(base_text, after, added, placeholder=False)
            overlay_base = mralib.insert_program_parts(base_text, after, added, placeholder=True)
        patched_rom = mralib.assemble(target_base, mralib.ZipSource([patched_path], check_crc=False))
        runs = mralib.diff_runs(mralib.assemble(overlay_base, stock_src), patched_rom)
        text = mralib.make_patch_mra(
            overlay_base,
            name=mra_cfg["name"],
            setname=mra_cfg["setname"],
            patched_rom=patched_rom,
            runs=runs,
            note=mra_header_note(patch, mra_cfg),
            meta=mra_meta(patch, mra_cfg),
        )
        # Round-trip proof: the overlay MRA over the stock set must assemble to
        # exactly what the official MRA produces from the patched set.
        if mralib.assemble(text, stock_src) != patched_rom:
            raise SystemExit(f"{patch['slug']}: MRA round-trip verification failed "
                             f"({mra_cfg['filename']})")
        texts.append((mra_zip_path(patch, mra_cfg), text, len(runs)))
    return texts


def write_mra_zip(patch: dict, texts: list, out_path: Path, stamp: tuple,
                  members: list, variants: list | None = None) -> None:
    write = zip_writer(out_path, stamp)
    with zipfile.ZipFile(out_path, "w") as z:
        for path, text, _ in texts:
            write(z, path, text.encode())
        write(z, "readme.txt", make_readme(patch, members, "mra", variants).encode())
    runs_desc = ", ".join(str(n) for _, _, n in texts)
    print(f"{patch['slug']}: MRA rebuilt and verified ({runs_desc} patch runs) -> {out_path.name}")


def download_info(zipname: str) -> dict:
    out_path = DOCS / "downloads" / zipname
    return {
        "zipname": zipname,
        "size": out_path.stat().st_size,
        "sha256": sha256_file(out_path),
    }


def verify_chd_patch_end_to_end(patch: dict, manifest: dict, stock_path: Path) -> None:
    """Run the shipped apply script for real against the stock CHD."""
    slug = patch["slug"]
    tmp = Path(tempfile.mkdtemp(prefix="chdverify-"))
    try:
        shutil.copyfile(ROOT / "tools" / "bundle_apply_chd.py", tmp / "apply.py")
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
        out_chd = tmp / "out.chd"
        print(f"{slug}: end-to-end verification (extract + patch + rebuild)...")
        proc = subprocess.run(
            [sys.executable, str(tmp / "apply.py"), str(stock_path), "-o", str(out_chd)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise SystemExit(f"{slug}: end-to-end verify failed:\n{proc.stdout}\n{proc.stderr}")
        got = chdpatch.chd_header_sha1(out_chd)
        want = manifest["target"]["chd_sha1"]
        if got != want:
            raise SystemExit(f"{slug}: verified output SHA1 {got} != expected {want}")
        print(f"{slug}: end-to-end verify OK (output SHA1 {want})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_chd_downloads(patch: dict) -> dict | None:
    """Build (or reuse) a data-free CHD patch download."""
    slug = patch["slug"]
    cfg = patch["chd_patch"]
    zipname = f"{slug}-{patch['version']}-chd.zip"
    out_path = DOCS / "downloads" / zipname
    persist_path = GENERATED / f"{slug}.json"

    stock_path = resolve(cfg["stock_chd"])
    patched_path = resolve(cfg["patched_chd"])
    saved = json.loads(persist_path.read_text()) if persist_path.exists() else {}

    if live(slug) and stock_path.exists() and patched_path.exists():
        cache_keys = {
            "stock_sha1": chdpatch.chd_header_sha1(stock_path),
            "patched_sha1": chdpatch.chd_header_sha1(patched_path),
        }
        if saved.get("cache_keys") == cache_keys:
            technical = saved["technical"]
            print(f"{slug}: CHD patch sources unchanged, reusing verified manifest")
        else:
            if not shutil.which("chdman"):
                raise SystemExit(
                    f"{slug}: CHD sources changed but chdman is not available to "
                    f"regenerate the patch"
                )
            print(f"{slug}: generating CHD patch manifest (extracting both dumps)...")
            technical = chdpatch.generate(stock_path, patched_path)
            manifest = {
                "title": f"{patch['title']} · {patch['subtitle']}",
                "version": patch["version"],
                "game": patch["game"],
                "set": patch["set"],
                "hardware": patch["hardware"],
                "chd": cfg["chd"],
                **technical,
            }
            verify_chd_patch_end_to_end(patch, manifest, stock_path)
            GENERATED.mkdir(parents=True, exist_ok=True)
            persist_path.write_text(
                json.dumps({"kind": "chd", "cache_keys": cache_keys,
                            "technical": technical}, indent=2) + "\n"
            )
    elif saved.get("technical") and out_path.exists():
        technical = saved["technical"]
        print(f"{slug}: sources unavailable, reusing existing CHD patch")
    else:
        print(f"{slug}: WARNING: no sources and no existing CHD patch; download omitted")
        return None

    manifest = {
        "title": f"{patch['title']} · {patch['subtitle']}",
        "version": patch["version"],
        "game": patch["game"],
        "set": patch["set"],
        "hardware": patch["hardware"],
        "chd": cfg["chd"],
        **technical,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = tuple(int(x) for x in patch["date"].split("-")) + (0, 0, 0)
    write = zip_writer(out_path, stamp)
    with zipfile.ZipFile(out_path, "w") as z:
        write(z, "readme.txt", make_readme(patch, [], "chd").encode())
        write(z, "manifest.json", json.dumps(manifest, indent=2).encode())
        write(z, "apply.py", (ROOT / "tools" / "bundle_apply_chd.py").read_bytes())
    print(f"{slug}: CHD patch packaged -> {zipname}")

    return {"kind": "chd", "chd": download_info(zipname), "manifest": manifest}


def build_downloads(patch: dict) -> dict | None:
    """Build (or reuse) the downloads for a patch. Returns render info or None."""
    if patch.get("chd_patch"):
        return build_chd_downloads(patch)
    return build_rom_downloads(patch)


def diff_members(slug: str, stock: dict, patched: dict) -> tuple[list, dict]:
    """Per-member actions and IPS patches for one stock -> patched pair,
    round-trip verified.  A member only the patched set has is an added ROM:
    its IPS creates the file from nothing."""
    if set(stock) - set(patched):
        raise SystemExit(f"{slug}: patched zip lacks stock members "
                         f"{sorted(set(stock) - set(patched))}")
    members, ips_files = [], {}
    for name in sorted(set(stock) | set(patched)):
        if name not in stock:
            ips = ipsutil.make_ips_create(patched[name])
            if ipsutil.apply_ips(ips, b"") != patched[name]:
                raise SystemExit(f"{slug}: round-trip verification failed for added {name}")
            members.append({"name": name, "size": len(patched[name]), "action": "add",
                            "patched_crc32": crc32(patched[name])})
            ips_files[name] = ips
            continue
        entry = {
            "name": name,
            "size": len(stock[name]),
            "stock_crc32": crc32(stock[name]),
            "action": "copy",
        }
        if stock[name] != patched[name]:
            ips = ipsutil.make_ips(stock[name], patched[name])
            # Round-trip proof: stock + patch must equal the verified build.
            if ipsutil.apply_ips(ips, stock[name]) != patched[name]:
                raise SystemExit(f"{slug}: round-trip verification failed for {name}")
            entry.update(action="patch", patched_crc32=crc32(patched[name]))
            ips_files[name] = ips
        members.append(entry)
    if not ips_files:
        raise SystemExit(f"{slug}: stock and patched zips are identical")
    return members, ips_files


def check_hbmame(slug: str, hb: dict, members: list, patched: dict) -> dict:
    """Validate an hbmame block against the patched members; returns the
    manifest entry.  Renames may map a file to its own name when HBMAME keeps
    the stock filenames and only the checksums change."""
    patched_names = {m["name"] for m in members if m["action"] != "copy"}
    if set(hb["renames"]) != patched_names:
        raise SystemExit(
            f"{slug}: hbmame.renames keys {sorted(hb['renames'])} do not match "
            f"patched members {sorted(patched_names)}"
        )
    if len(set(hb["renames"].values())) != len(hb["renames"]):
        raise SystemExit(f"{slug}: hbmame.renames has duplicate target names")
    hbmame_out = {hb["renames"][n]: crc32(patched[n]) for n in patched_names}
    print(f"{slug}: HBMAME set '{hb['setname']}' verified: " +
          ", ".join(f"{n}={c}" for n, c in sorted(hbmame_out.items())))
    return {"setname": hb["setname"], "renames": hb["renames"]}


def read_zip(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist()}


def build_rom_downloads(patch: dict) -> dict | None:
    """Build (or reuse) the IPS and MRA downloads. Returns render info or None.

    One IPS bundle carries the patches (ips/, or ips/<variant>/ when the
    patch has several builds on one stock set), the verified MiSTer overlays
    under mister/, and apply.py, which writes mame/, hbmame/ and mister/ in
    one run.  The MRAs are also packaged on their own for MiSTer-only users."""
    slug = patch["slug"]
    art = patch.get("artifact")
    if not art:
        return None
    specs = art.get("variants")
    ips_zipname = f"{slug}-{patch['version']}-ips.zip"
    mra_zipname = f"{slug}-{patch['version']}-mra.zip" if patch.get("mra") else None
    ips_path = DOCS / "downloads" / ips_zipname
    persist_path = GENERATED / f"{slug}.json"
    stock_path = resolve(art["stock_zip"])
    builds = specs or [{"key": None, "patched_zip": art["patched_zip"],
                        "mra": patch.get("mra") if patch.get("mra") else None,
                        "hbmame": patch.get("hbmame")}]
    sources = live(slug) and stock_path.exists() and all(resolve(b["patched_zip"]).exists() for b in builds)

    if sources:
        stock = read_zip(stock_path)
        results, mra_entries = [], []
        for b in builds:
            tag = f"{slug}/{b['key']}" if b["key"] else slug
            patched = read_zip(resolve(b["patched_zip"]))
            members, ips_files = diff_members(tag, stock, patched)
            entry = {"key": b["key"], "label": b.get("label"), "members": members}
            if b.get("mra"):
                entry["mra"] = b["mra"]
                mra_entries.append((b["mra"], resolve(b["patched_zip"]), members))
            if b.get("hbmame"):
                entry["hbmame"] = check_hbmame(tag, b["hbmame"], members, patched)
            results.append((entry, ips_files))
        texts = generate_mras(patch, stock_path, mra_entries) if mra_zipname else []
        mra_by_cfg = {id(cfg): path for (cfg, _, _), (path, _, _) in zip(mra_entries, texts)}

        manifest = {
            "title": f"{patch['title']} · {patch['subtitle']}",
            "version": patch["version"],
            "game": patch["game"],
            "set": patch["set"],
            "hardware": patch["hardware"],
        }
        if patch.get("mame_build") is False:
            manifest["mame_build"] = False
        if specs:
            manifest["variants"] = [
                {k: e[k] for k in ("key", "label", "members") if k in e}
                | ({"hbmame": e["hbmame"]} if e.get("hbmame") else {})
                | ({"mra_setname": e["mra"]["setname"], "mra": f"mister/{mra_by_cfg[id(e['mra'])]}"}
                   if e.get("mra") else {})
                for e, _ in results]
        else:
            e = results[0][0]
            manifest["members"] = e["members"]
            if e.get("hbmame"):
                manifest["hbmame"] = e["hbmame"]
            if e.get("mra"):
                manifest["mra"] = f"mister/{mra_by_cfg[id(e['mra'])]}"

        ips_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = tuple(int(x) for x in patch["date"].split("-")) + (0, 0, 0)
        variants = [e for e, _ in results] if specs else None
        members = results[0][0]["members"]
        write = zip_writer(ips_path, stamp)
        with zipfile.ZipFile(ips_path, "w") as z:
            write(z, "readme.txt", make_readme(patch, members, "ips", variants).encode())
            write(z, "manifest.json", json.dumps(manifest, indent=2).encode())
            write(z, "apply.py", (ROOT / "tools" / "bundle_apply.py").read_bytes())
            for e, ips_files in results:
                prefix = f"ips/{e['key']}/" if e["key"] else "ips/"
                for name, ips in sorted(ips_files.items()):
                    write(z, f"{prefix}{name}.ips", ips)
            for path, text, _ in texts:
                write(z, f"mister/{path}", text.encode())
        n_patched = sum(len(f) for _, f in results)
        print(f"{slug}: IPS bundle rebuilt from sources ({len(results)} builds, "
              f"{n_patched} patched ROMs) -> {ips_zipname}")
        if texts:
            write_mra_zip(patch, texts, DOCS / "downloads" / mra_zipname, stamp, members, variants)
        GENERATED.mkdir(parents=True, exist_ok=True)
        saved = {"ips_zipname": ips_zipname, "mra_zipname": mra_zipname}
        saved |= {"variants": variants} if specs else {"members": members}
        persist_path.write_text(json.dumps(saved, indent=2) + "\n")
    elif persist_path.exists() and ips_path.exists():
        saved = json.loads(persist_path.read_text())
        variants = saved.get("variants")
        members = variants[0]["members"] if variants else saved["members"]
        print(f"{slug}: sources unavailable, reusing existing downloads")
    else:
        print(f"{slug}: WARNING: no sources and no existing downloads; downloads omitted")
        return None

    result = {"kind": "rom", "members": members, "ips": download_info(ips_zipname)}
    if specs:
        result["variants"] = variants
    if mra_zipname and (DOCS / "downloads" / mra_zipname).exists():
        result["mra"] = download_info(mra_zipname)
    return result


# ------------------------------------------------------------ screenshots


def copy_screenshots(patch: dict) -> list:
    """Copy configured screenshots into docs/img/<slug>/ and return render info."""
    slug = patch["slug"]
    img_dir = DOCS / "img" / slug
    shots = []
    for i, shot in enumerate(patch.get("screenshots", [])):
        entry = {"caption": shot.get("caption", "")}
        if shot.get("before_label"):
            entry["before_label"] = shot["before_label"]
        if shot.get("after_label"):
            entry["after_label"] = shot["after_label"]
        for role in ("before", "after", "single"):
            src = shot.get(role)
            if not src:
                continue
            target = img_dir / f"{i:02d}_{role}.png"
            src_path = resolve(src)
            if src_path.exists() and (live(slug) or not target.exists()):
                img_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_path, target)
            elif not target.exists():
                print(f"{slug}: WARNING: screenshot missing: {src}")
                continue
            entry[role] = f"../img/{slug}/{target.name}"
        if any(r in entry for r in ("before", "after", "single")):
            shots.append(entry)
    return shots


# ------------------------------------------------------------------ html


def page(site: dict, title: str, body: str, depth: int = 0) -> str:
    rel = "../" * depth
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{rel}style.css">
</head>
<body>
<header class="site">
  <div class="inner">
    <a class="home" href="{rel or './'}">{esc(site['title'])}</a>
    <div class="tagline">{esc(site['tagline'])}</div>
  </div>
</header>
<main>
{body}
</main>
<footer class="site">
  <div class="inner">
    <p>These are free, unofficial fan patches. This site is not affiliated with or endorsed
    by Capcom. All game titles, characters, and artwork remain the property of their
    respective owners. No ROMs or other copyrighted game data are hosted here.</p>
    <p><a href="{rel}legal.html">Legal &amp; disclaimers</a>{repo_link(site)}</p>
  </div>
</footer>
</body>
</html>
"""


def repo_link(site: dict) -> str:
    url = site.get("repo_url")
    return f' · <a href="{esc(url)}">Source on GitHub</a>' if url else ""


# Statuses considered the default/finished state, shown without a status pill.
NO_PILL_STATUSES = {"released"}


def status_pill(patch: dict) -> str:
    status = patch["status"]
    if status in NO_PILL_STATUSES:
        return ""
    label = STATUS_LABELS.get(status, status)
    return f'<span class="badge {esc(status)}">{esc(label)}</span>'


def badges(patch: dict) -> str:
    out = []
    pill = status_pill(patch)
    if pill:
        out.append(pill)
    if patch.get("version"):
        ver = patch["version"]
        out.append(f'<span class="badge plain">{esc("v" + ver if ver[0].isdigit() else ver)}</span>')
    out.append(f'<span class="badge plain">{esc(patch["hardware"])}</span>')
    return f'<div class="badges">{"".join(out)}</div>'


def render_index(site: dict, patches: list, thumbs: dict,
                 projects: list = (), project_thumbs: dict = {}) -> str:
    intro = "\n".join(f"<p>{esc(p)}</p>" for p in site["intro"])
    featured = []
    for proj in projects:
        thumb = project_thumbs.get(proj["slug"])
        thumb_html = (
            f'<img class="thumb" src="{esc(thumb)}" '
            f'alt="{esc(proj["title"])} screenshot">' if thumb else ""
        )
        stats = proj.get("featured_stats", "")
        stats_html = f'<div class="stats">{esc(stats)}</div>' if stats else ""
        featured.append(f"""<a class="card featured" href="{esc(proj['slug'])}/">
  <div>
    <h2>{esc(proj["title"])}</h2>
    <div class="sub">{esc(proj['subtitle'])}</div>
    <p class="summary">{esc(proj['summary'])}</p>
    {stats_html}
  </div>
  {thumb_html}
</a>""")
    cards = []
    for patch in patches:
        thumb = thumbs.get(patch["slug"])
        thumb_html = (
            f'<img class="thumb" src="{esc(thumb)}" alt="{esc(patch["title"])} screenshot">'
            if thumb
            else ""
        )
        cards.append(f"""<a class="card" href="{esc(patch['slug'])}/">
  <div>
    <h2>{esc(patch["title"])}</h2>
    <div class="sub">{esc(patch['subtitle'])} · {esc(patch['game'])}</div>
    <p class="summary">{esc(patch['summary'])}</p>
  </div>
  {thumb_html}
</a>""")
    body = f"{intro}\n{''.join(featured)}{''.join(cards)}"
    return page(site, site["title"], body)


def render_shots(shots: list, heading: str = "Screenshots") -> str:
    if not shots:
        return ""
    out = [f"<h2>{esc(heading)}</h2>"]
    for shot in shots:
        cap = f"<figcaption>{esc(shot['caption'])}</figcaption>" if shot["caption"] else ""
        if "single" in shot:
            out.append(
                f'<figure class="shot single"><img src="{esc(shot["single"])}" '
                f'alt="{esc(shot["caption"])}">{cap}</figure>'
            )
        else:
            cols = []
            defaults = {"before": "Original", "after": "Patched"}
            for role in ("before", "after"):
                if role in shot:
                    label = shot.get(f"{role}_label", defaults[role])
                    cols.append(
                        f'<div><p class="label">{esc(label)}</p>'
                        f'<img src="{esc(shot[role])}" alt="{esc(label)}: {esc(shot["caption"])}"></div>'
                    )
            out.append(f'<figure class="shot"><div class="pair">{"".join(cols)}</div>{cap}</figure>')
    return "\n".join(out)


def download_box(label: str, info: dict) -> str:
    return f"""<div class="download">
  <div class="kind">{esc(label)}</div>
  <div class="file"><a href="../downloads/{esc(info['zipname'])}">{esc(info['zipname'])}</a>
  ({human_size(info['size'])})</div>
  <div class="hash">SHA-256: {info['sha256']}</div>
</div>"""


def render_chd_download(patch: dict, bundle: dict) -> str:
    m = bundle["manifest"]
    setname = esc(patch["set"])
    chd = esc(m["chd"])
    info = bundle["chd"]
    changed = len(m["patch"]["new"]) // 2
    return f"""<h2>Download</h2>
{download_box("NAOMI GD-ROM patch, applied to your own disc image", info)}
<p>The download is a description of the change ({changed} bytes in track
{m['patch']['track']}) and a script that applies it. You need your own dump of
<strong>{esc(patch['game'])}</strong>: the MAME set
<code>{setname}.zip</code> plus <code>{setname}/{chd}</code>.</p>
<h2>How to apply</h2>
<pre><code>unzip {esc(info['zipname'])} -d {setname}-patch
cd {setname}-patch
python3 apply.py /path/to/{setname}/{chd}</code></pre>
<p>Requirements: Python 3, <code>chdman</code> (it ships with every MAME
distribution), and about 2.5&nbsp;GB of temporary disk space. The script verifies
your dump, unpacks it with chdman, patches {changed} bytes, rebuilds the CHD, and
verifies the result against the checksum below. If anything does not match,
nothing is kept.</p>
<p>Rename the verified output to <code>{chd}</code> inside an
<code>{setname}/</code> folder placed ahead of the stock set in your MAME rompath.
MAME reports a checksum warning for the patched CHD. That is expected, and the
game boots on Japanese, USA, and Export BIOS regions.</p>
<h3>CHD checksums</h3>
<table>
<tr><th></th><th>SHA-1</th><th>Data SHA-1</th></tr>
<tr><td>Original</td><td class="mono">{esc(m['source']['chd_sha1'])}</td>
<td class="mono">{esc(m['source']['data_sha1'])}</td></tr>
<tr><td>Patched</td><td class="mono">{esc(m['target']['chd_sha1'])}</td>
<td class="mono">{esc(m['target']['data_sha1'])}</td></tr>
</table>"""


def render_download(patch: dict, bundle: dict | None) -> str:
    if not bundle:
        return ""
    if bundle["kind"] == "chd":
        return render_chd_download(patch, bundle)
    if bundle["kind"] == "kit":
        return render_kit_download(patch, bundle)
    return render_rom_download(patch, bundle)


def crc_rows(entries) -> str:
    return "\n".join(
        f'<tr><td class="mono">{esc(n)}</td><td>{human_size(m["size"])}</td>'
        f'<td class="mono">{esc(m["stock_crc32"]) if m["action"] == "patch" else "new file"}</td>'
        f'<td class="mono">{esc(m["patched_crc32"])}</td></tr>'
        for n, m in entries)


def render_rom_download(patch: dict, bundle: dict) -> str:
    """Downloads for an IPS/MRA patch, one build or several on one stock set."""
    noun = patch_noun(patch)
    variants = bundle.get("variants")
    setname = esc(patch["set"])
    ips = bundle["ips"]
    mra_info = bundle.get("mra")
    n = len(variants) if variants else 1
    mame_build = patch.get("mame_build", True)
    kit = ("Patch kit for MAME, HBMAME, MiSTer and original hardware" if mame_build
           else "Patch kit for HBMAME and MiSTer")
    parts = ["<h2>Downloads</h2>"]
    if variants:
        parts.append(download_box(f"{kit}, all {n} builds", ips))
        if mra_info:
            parts.append(download_box(f"MiSTer MRAs only, all {n} builds", mra_info))
    else:
        parts.append(download_box(kit, ips))
        if mra_info:
            parts.append(download_box("MiSTer MRA only", mra_info))
    parts.append(f"""<p>You need your own dump of <strong>{esc(patch['game'])}</strong> as the MAME
set <code>{setname}.zip</code>.{" Every build is made from that one set." if variants else ""}
Each zip includes a <code>readme.txt</code> with full instructions.</p>""")

    if variants:
        has_hb = any(v.get("hbmame") for v in variants)
        head = "<tr><th>Build</th><th><code>--variant</code></th>"
        head += "<th>MiSTer setname</th>" if mra_info else ""
        head += "<th>HBMAME set</th>" if has_hb else ""
        rows = []
        for v in variants:
            row = f'<tr><td>{esc(v["label"])}</td><td class="mono">{esc(v["key"])}</td>'
            if mra_info:
                row += f'<td class="mono">{esc(v["mra"]["setname"]) if v.get("mra") else "—"}</td>'
            if has_hb:
                row += f'<td class="mono">{esc(v["hbmame"]["setname"]) if v.get("hbmame") else "—"}</td>'
            rows.append(row + "</tr>")
        parts.append(f"<h3>The builds</h3>\n<table>\n{head}</tr>\n{''.join(rows)}\n</table>")

    outputs = build_outputs(patch, variants)
    listing = []
    for label, mame, hb, mra in outputs:
        if label:
            listing.append(f"{label}:")
        pad = "  " if label else ""
        if mame:
            listing.append(f"{pad}out/{mame}")
        if hb:
            listing.append(f"{pad}out/hbmame/{hb}.zip")
        if mra:
            listing.append(f"{pad}out/{mra}")
    one = f" (add <code>--variant {esc(variants[0]['key'])}</code> to make just one build)" if variants else ""
    parts.append(f"""<h2>How to apply</h2>
<pre><code>unzip {esc(ips['zipname'])} -d {setname}-patch
cd {setname}-patch
python3 apply.py /path/to/{setname}.zip --out-dir out</code></pre>
<p>The script checks every file against the checksums below, applies the patches,
checks the result, and writes the files for every platform in one go:{one}</p>
<pre><code>{esc(chr(10).join(listing))}</code></pre>""")
    if mame_build:
        parts.append(f"""<p><strong>MAME and original hardware.</strong> Put the zip from <code>out/mame/</code> ahead of the
stock set in your MAME rompath (it keeps the name <code>{setname}.zip</code>{", so use one build at a time" if variants else ""}), or burn its ROM
files. MAME reports checksum warnings for the patched ROMs. That's expected, and the game runs
normally.</p>""")
    else:
        parts.append("""<p>There is no MAME build: stock MAME has no driver that loads the added
program ROM, so on a computer, play it in HBMAME.</p>""")

    hb_sets = [o[2] for o in outputs if o[2]]
    if hb_sets:
        hb = patch.get("hbmame") or next(v["hbmame"] for v in variants if v.get("hbmame"))
        sets = ", ".join(f"<code>{esc(s)}</code>" for s in hb_sets)
        if hb.get("pr_url"):
            status = (f"This {noun} is an official <a href=\"https://github.com/Robbbert/hbmame\">HBMAME</a> "
                      f"set, {sets} (<a href=\"{esc(hb['pr_url'])}\">merged upstream</a>), so full HBMAME "
                      f"collections may already carry it.")
        else:
            status = (f"The <a href=\"https://github.com/Robbbert/hbmame\">HBMAME</a> set definition "
                      f"({sets}) is generated with the {noun}; an upstream submission is pending, so for "
                      f"now you add it to your own HBMAME build.")
        parts.append(f"""<p><strong>HBMAME.</strong> Put the zip from <code>out/hbmame/</code> in HBMAME's
<code>roms/</code> folder next to your stock <code>{setname}.zip</code>. It loads with no checksum
warnings. {status}</p>""")

    if mra_info:
        parts.append(f"""<p><strong>MiSTer.</strong> Copy the <code>_Arcade</code> folder from
<code>out/mister/</code> to the root of your SD card, and keep the stock, unmodified romset at
<code>games/mame/{setname}.zip</code> (split MAME sets also need <code>qsound.zip</code>). You need
Jotego's <code>jtcps2</code> core, which update_all installs. The MRA applies the {noun} in memory
as the game loads, so nothing on your card is modified, and {"each build keeps its own settings and saves under its own setname" if variants else f"it keeps its own settings and saves under <code>{esc(patch['mra']['setname'])}</code>"}.
The MiSTer-only download holds the same MRAs, ready to copy.</p>""")

    ips_dir = "ips/&lt;variant&gt;/" if variants else "ips/"
    if mame_build:
        parts.append(f"""<p><strong>Any IPS patcher.</strong> Apply each file in <code>{ips_dir}</code>
(Flips, Lunar IPS, …) to the ROM file of the same name and re-zip the set yourself.</p>""")

    # Changed ROMs: members that patch identically in every build once, the
    # build-specific ones per build.
    builds = variants or [{"label": None, "members": bundle["members"]}]
    names = []
    for v in builds:
        for m in v["members"]:
            if m["action"] != "copy" and m["name"] not in names:
                names.append(m["name"])
    by_name = {nm: [next((m for m in v["members"] if m["name"] == nm), None) for v in builds]
               for nm in names}
    shared, per_build = [], []
    for nm in names:
        entries = by_name[nm]
        crcs = {(m or {}).get("patched_crc32") if (m or {}).get("action") != "copy" else None
                for m in entries}
        if len(crcs) == 1 and None not in crcs:
            shared.append((nm, entries[0]))
        else:
            per_build.append((nm, entries))
    head = "<tr><th>File</th><th>Size</th><th>Original CRC32</th><th>Patched CRC32</th></tr>"
    parts.append("<h3>Changed ROMs</h3>")
    if shared:
        intro = "<p>Shared by every build:</p>\n" if variants else ""
        parts.append(f"{intro}<table>\n{head}\n{crc_rows(shared)}\n</table>")
    for nm, entries in per_build:
        first = next(m for m in entries if m)
        orig = (f"original CRC32 <code>{esc(first['stock_crc32'])}</code>"
                if first["action"] != "add" else "a new file")
        rows = "\n".join(
            f'<tr><td>{esc(v["label"])}</td><td class="mono">'
            f'{esc(m["patched_crc32"]) if m and m["action"] != "copy" else "unchanged"}</td></tr>'
            for v, m in zip(builds, entries))
        parts.append(f"""<p><code>{esc(nm)}</code> ({human_size(first["size"])}, {orig}) differs per build:</p>
<table>
<tr><th>Build</th><th>Patched CRC32</th></tr>
{rows}
</table>""")
    return "\n".join(parts)


def render_feedback(patch: dict) -> str:
    """Restorations ask players to report differences the patch missed."""
    if not patch.get("feedback"):
        return ""
    url = SITE.get("author_url")
    contact = (f'<a href="{esc(url)}">let us know</a>' if url else "let us know")
    return f"""<h2>Spot a difference we missed?</h2>
<p>A restoration is only as complete as its list of differences. If the Japanese version does
something this one still doesn't, whether a line of text, a scene, a screen or a small detail,
please {contact}. Anything you find goes into the next release.</p>"""


def render_comparison(patch: dict) -> str:
    """Optional side-by-side table against a reference release."""
    cmp = patch.get("comparison")
    if not cmp:
        return ""
    parts = [f"<h2>{esc(cmp.get('heading', 'Compared with'))}</h2>"]
    for t in cmp.get("intro", []):
        parts.append(f"<p>{esc(t)}</p>")
    head = "".join(f"<th>{esc(c)}</th>" for c in cmp["columns"])
    rows = "".join(
        "<tr>" + "".join(
            (f"<th scope=\"row\">{esc(cell)}</th>" if i == 0 else f"<td>{esc(cell)}</td>")
            for i, cell in enumerate(row)) + "</tr>"
        for row in cmp["rows"])
    parts.append(f'<table class="compare"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>')
    for t in cmp.get("outro", []):
        parts.append(f"<p>{esc(t)}</p>")
    return "\n".join(parts)


# ------------------------------------------------ multi-build pages (SFA2 Gold)


def copy_build_titles(patch: dict) -> list:
    """Copy each build's title screen into docs/img/<slug>/ and return render info."""
    slug = patch["slug"]
    img_dir = DOCS / "img" / slug
    out = []
    for b in patch["builds"]:
        target = img_dir / f"title_{b['key']}.png"
        src = resolve(b["title_screen"])
        if src.exists() and (live(slug) or not target.exists()):
            img_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        elif not target.exists():
            print(f"{slug}: WARNING: build title missing: {b['title_screen']}")
            continue
        out.append({**b, "img": f"../img/{slug}/{target.name}"})
    return out


def render_builds_gallery(builds: list) -> str:
    cells = []
    for b in builds:
        cells.append(
            f'<figure class="build">'
            f'<img src="{esc(b["img"])}" alt="{esc(b["title"])} ({esc(b["region"])}) title screen">'
            f'<figcaption><strong>{esc(b["title"])}</strong><br>'
            f'{esc(b["region"])} · <code>{esc(b["hbmame_set"])}</code></figcaption>'
            f'</figure>'
        )
    return f'<h2>The four builds</h2>\n<div class="build-gallery">{"".join(cells)}</div>'


# The default kit shape (SFA2 Gold's): a handful of modules beside a
# recipes/ directory.  A patch whose reconstruction is a PIPELINE rather
# than a recipe -- Final Fight CD runs a 68000 interpreter over the disc --
# names its own contents with reconstruction.kit_include instead.
KIT_MODULES = ("apply.py", "extract.py", "recipe.py", "gfx.py", "assemble.py",
               "bizlz.py", "README.txt")


def build_reconstruction_kit(patch: dict) -> dict | None:
    """Package the reconstruction tool + per-region recipes into a
    download zip."""
    rec = patch.get("reconstruction") or {}
    kit_dir = rec.get("kit_dir")
    if not kit_dir:
        return None
    src = ROOT / kit_dir
    recipe_files = sorted((src / "recipes").glob("*.json")) if (src / "recipes").exists() else []
    zipname = f"{patch['slug']}-{patch['version']}-kit.zip"
    out_path = DOCS / "downloads" / zipname
    include = rec.get("kit_include")
    if include:
        # Glob-listed kit.  Every path is stated, so nothing a build leaves
        # behind -- work trees, generated ROMs -- can be swept in by accident.
        files = []
        for pat in include:
            files += sorted(q for q in src.glob(pat) if q.is_file())
        if not files:
            print(f"{patch['slug']}: WARNING: kit_include matched nothing; "
                  f"download omitted")
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = tuple(int(x) for x in patch["date"].split("-")) + (0, 0, 0)
        write = zip_writer(out_path, stamp)
        with zipfile.ZipFile(out_path, "w") as z:
            for q in files:
                write(z, str(q.relative_to(src)), q.read_bytes())
        print(f"{patch['slug']}: reconstruction kit packaged "
              f"({len(files)} files) -> {zipname}")
        return {"kind": "kit", "zipname": zipname,
                "size": out_path.stat().st_size,
                "sha256": sha256_file(out_path), "regions": []}
    if not recipe_files:
        if out_path.exists():
            print(f"{patch['slug']}: recipes absent, reusing existing kit")
            return {"kind": "kit", "zipname": zipname, "size": out_path.stat().st_size,
                    "sha256": sha256_file(out_path), "regions": []}
        print(f"{patch['slug']}: WARNING: no recipes and no existing kit; download omitted")
        return None
    mra_files = sorted((src / "mras").glob("*.mra")) if (src / "mras").exists() else []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = tuple(int(x) for x in patch["date"].split("-")) + (0, 0, 0)
    write = zip_writer(out_path, stamp)
    with zipfile.ZipFile(out_path, "w") as z:
        for m in KIT_MODULES:
            write(z, m, (src / m).read_bytes())
        for rf in recipe_files:
            write(z, f"recipes/{rf.name}", rf.read_bytes())
        for mf in mra_files:
            write(z, f"mras/{mf.name}", mf.read_bytes())
    print(f"{patch['slug']}: reconstruction kit packaged "
          f"({len(recipe_files)} regions) -> {zipname}")
    return {"kind": "kit", "zipname": zipname, "size": out_path.stat().st_size,
            "sha256": sha256_file(out_path), "regions": [f.stem for f in recipe_files]}


def render_kit_download(patch: dict, kit: dict) -> str:
    """The kit download box.

    Everything specific to a patch comes from its reconstruction block --
    what the user must supply, and the command that builds it.  This used
    to be SFA2 Gold's text hardcoded, which would have told Final Fight CD
    readers to supply a PlayStation 2 disc.
    """
    rec = patch.get("reconstruction") or {}
    slug = patch["slug"]
    needs = "".join(f"<li>{esc(x)}</li>" for x in rec.get("requires", []))
    cmd = rec.get("kit_command") or (
        f"python3 apply.py --help")
    return f"""<h2>Download</h2>
{download_box("Reconstruction kit, rebuilds from your own disc + romset", kit)}
<p>The kit is our reconstruction code and a short list of byte patches.
Everything else is rebuilt on your machine from files you already own.</p>
<h3>What you supply</h3>
<ul>{needs}</ul>
<h2>How to build</h2>
<pre><code>unzip {esc(kit['zipname'])} -d {esc(slug)}
cd {esc(slug)}
{esc(cmd)}</code></pre>
"""

def render_reconstruction(patch: dict, builds: list, bundle: dict | None) -> str:
    rec = patch.get("reconstruction", {})
    reqs = "".join(f"<li>{esc(r)}</li>" for r in rec.get("requires", []))
    rows = "".join(
        f'<tr><td>{esc(b["region"])}</td>'
        f'<td>{esc(b["title"])}</td>'
        f'<td class="mono">{esc(b["hbmame_set"])}</td>'
        f'<td class="mono">{esc(b["mame_set"])}</td></tr>'
        for b in builds
    )
    parts = ["""<h2>How it's distributed</h2>
<p>Each build is put together on your own machine from files you already own.
You need:</p>"""]
    parts.append(f"<ul>{reqs}</ul>")
    parts.append("""<p>A small tool reads the revised game from your PlayStation 2 disc image,
combines it with your arcade Zero 2 Alpha romset, and writes out the finished
CPS-2 build.</p>
<p>Two sizes of each build are produced. A <strong>4&nbsp;MB</strong> set stays within
original CPS-2 limits and runs on real hardware and stock MAME. An <strong>8&nbsp;MB</strong>
set carries Cammy's complete voice and sound-effect audio, more than an original board
could hold, for HBMAME and MiSTer (Jotego's <code>jtcps2</code> core).</p>""")
    parts.append(f"""<h3>The builds</h3>
<table>
<tr><th>Region</th><th>Title</th><th>HBMAME / MiSTer (8&nbsp;MB)</th><th>Hardware / MAME (4&nbsp;MB)</th></tr>
{rows}
</table>""")
    if bundle and bundle.get("kind") == "kit":
        parts.append(render_kit_download(patch, bundle))
    elif bundle:
        parts.append(render_download(patch, bundle))
    else:
        parts.append('<p class="notes">The reconstruction tool is published with this'
                     " page; download and step-by-step instructions appear here once built.</p>")
    return "\n".join(parts)


def render_builds_page(site: dict, patch: dict, builds: list, bundle: dict | None,
                       shots: list | None = None) -> str:
    parts = ['<a class="back" href="../">&larr; All patches</a>']
    parts.append(f"<h1>{esc(patch['title'])}</h1>")
    parts.append(f'<p class="subtitle">{esc(patch["subtitle"])}</p>')
    parts.append(badges(patch))

    meta = [("Game", patch["game"]), ("Hardware", patch["hardware"])]
    if patch.get("version"):
        meta.append(("Version", patch["version"]))
    if patch.get("date"):
        meta.append(("Updated", patch["date"]))
    parts.append(
        '<dl class="meta">'
        + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in meta)
        + "</dl>"
    )

    parts.append(render_builds_gallery(builds))
    parts.append("<h2>About</h2>")
    parts.extend(f"<p>{esc(p)}</p>" for p in patch["description"])
    if patch.get("changes"):
        parts.append("<h2>What's included</h2><ul>")
        parts.extend(f"<li>{esc(c)}</li>" for c in patch["changes"])
        parts.append("</ul>")
    if shots:
        parts.append(render_shots(shots, heading=patch.get("screenshots_heading", "Screenshots")))
    parts.append(render_reconstruction(patch, builds, bundle))
    parts.append(render_release_history(patch))
    parts.append(render_related(patch))
    if patch.get("notes"):
        parts.append('<h2>Notes</h2><ul class="notes">')
        parts.extend(f"<li>{esc(n)}</li>" for n in patch["notes"])
        parts.append("</ul>")

    return page(site, f"{patch['title']} · {site['title']}", "\n".join(parts), depth=1)



def render_release_history(patch: dict) -> str:
    """Optional per-version changelog section."""
    hist = patch.get("release_history")
    if not hist:
        return ""
    parts = ["<h2>Release history</h2>"]
    for rel in hist:
        parts.append(
            f'<h3>{esc(rel["version"])} ({esc(rel["date"])})</h3><ul>'
        )
        parts.extend(f"<li>{esc(i)}</li>" for i in rel["items"])
        parts.append("</ul>")
    return "\n".join(parts)


def render_related(patch: dict) -> str:
    """Optional cross-links to companion projects/pages."""
    rel = patch.get("related")
    if not rel:
        return ""
    items = "".join(
        f'<li><a href="{esc(r["url"])}">{esc(r["label"])}</a>, {esc(r["note"])}</li>'
        for r in rel)
    return f"<h2>Related projects</h2><ul>{items}</ul>"


def render_patch_page(site: dict, patch: dict, bundle: dict | None, shots: list) -> str:
    parts = ['<a class="back" href="../">&larr; All patches</a>']
    parts.append(f"<h1>{esc(patch['title'])}</h1>")
    parts.append(f'<p class="subtitle">{esc(patch["subtitle"])}</p>')
    parts.append(badges(patch))

    meta = [("Game", patch["game"]), ("Hardware", patch["hardware"])]
    if patch.get("set") and patch["set"] != "—":
        meta.append(("MAME set", patch["set"]))
    if patch.get("version"):
        meta.append(("Version", patch["version"]))
    if patch.get("date"):
        meta.append(("Updated", patch["date"]))
    parts.append(
        '<dl class="meta">'
        + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in meta)
        + "</dl>"
    )

    parts.append(render_shots(shots))
    parts.append("<h2>About this patch</h2>")
    parts.extend(f"<p>{esc(p)}</p>" for p in patch["description"])

    if patch.get("changes"):
        parts.append("<h2>What's changed</h2><ul>")
        parts.extend(f"<li>{esc(c)}</li>" for c in patch["changes"])
        parts.append("</ul>")
    if patch.get("comparison"):
        parts.append(render_comparison(patch))
    if patch.get("feedback"):
        parts.append(render_feedback(patch))

    dl = render_download(patch, bundle)
    if not dl and patch.get("download_note"):
        dl = ('<h2>Download</h2>\n<div class="download soon">'
              f'<div class="kind">Coming soon</div>'
              f'<p>{esc(patch["download_note"])}</p></div>')
    parts.append(dl)

    if patch.get("release_history"):
        parts.append(render_release_history(patch))
    parts.append(render_related(patch))
    if patch.get("notes"):
        parts.append('<h2>Notes</h2><ul class="notes">')
        parts.extend(f"<li>{esc(n)}</li>" for n in patch["notes"])
        parts.append("</ul>")

    title = f"{patch['title']} · {site['title']}"
    return page(site, title, "\n".join(parts), depth=1)



def copy_project_kit(project: dict) -> dict | None:
    """Copy a prebuilt project kit zip into docs/downloads and describe it."""
    kit = project.get("kit")
    if not kit:
        return None
    src = resolve(kit["source"])
    zipname = kit["zipname"]
    dest = DOCS / "downloads" / zipname
    if not live(project["slug"]) and dest.exists():
        pass
    elif src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    elif dest.exists():
        print(f"{project['slug']}: WARNING: kit source not found ({src}); "
              f"REUSING the existing {zipname}, which may be stale")
    else:
        print(f"{project['slug']}: WARNING: kit missing: {src}")
        return None
    return download_info(zipname)


def render_project_page(site: dict, project: dict, kit: dict | None,
                        shots: list) -> str:
    parts = ['<a class="back" href="../">&larr; All patches</a>']
    parts.append(f"<h1>{esc(project['title'])}</h1>")
    parts.append(f'<p class="subtitle">{esc(project["subtitle"])}</p>')
    parts.append(badges(project))

    meta = [(k, v) for k, v in (
        ("Hardware", project.get("hardware")),
        ("Version", project.get("version")),
        ("Updated", project.get("date")),
    ) if v]
    if meta:
        parts.append('<dl class="meta">' + "".join(
            f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in meta) + "</dl>")

    parts.extend(f"<p>{esc(t)}</p>" for t in project["description"])

    if project.get("highlights"):
        parts.append('<ul class="highlights">')
        parts.extend(f"<li>{esc(t)}</li>" for t in project["highlights"])
        parts.append("</ul>")

    parts.append(render_shots(shots))

    if kit:
        parts.append("<h2>Get started</h2>")
        parts.append(download_box(project["kit"].get(
            "label", "MiSTer kit: cores, MRAs and the pack builder"), kit))
    if project.get("quickstart"):
        parts.append("<ol>")
        parts.extend(f"<li>{esc(t)}</li>" for t in project["quickstart"])
        parts.append("</ol>")
    for t in project.get("download_notes", []):
        parts.append(f"<p>{esc(t)}</p>")

    if project.get("coverage"):
        # The platform is written once, on the disc list, and looked up here --
        # so a disc cannot end up labelled on one row and bare on the next.
        platform_of = {s["disc"]: s["platform"] for s in project.get("sources", [])}
        parts.append("<h2>Game coverage</h2>")
        if project.get("coverage_intro"):
            parts.append(f"<p>{esc(project['coverage_intro'])}</p>")
        parts.append('<table class="coverage"><thead><tr>'
                     "<th>Arcade game</th><th>Soundtrack</th>"
                     "<th>Built from</th></tr></thead><tbody>")
        for game in project["coverage"]:
            packs = game["packs"]
            for i, pack in enumerate(packs):
                cls = ' class="group"' if i == 0 else ""
                parts.append(f"<tr{cls}>")
                if i == 0:
                    alt = (f'<span class="alt">{esc(game["also"])}</span>'
                           if game.get("also") else "")
                    sets = (f'<span class="sets mono">{esc(game["sets"])}</span>'
                            if game.get("sets") else "")
                    parts.append(f'<th scope="rowgroup" rowspan="{len(packs)}">'
                                 f'{esc(game["game"])}{alt}{sets}</th>')
                disc = pack["disc"]
                plat = platform_of.get(disc)
                disc_html = (f'{esc(disc)} <span class="on">({esc(plat)})</span>'
                             if plat else esc(disc))
                parts.append(f'<td>{esc(pack["soundtrack"])}</td>'
                             f'<td>{disc_html}</td></tr>')
        parts.append("</tbody></table>")

    if project.get("sources"):
        parts.append("<h3>Which disc you need</h3>")
        parts.append('<div class="sources">')
        for row in sorted(project["sources"], key=lambda r: r["disc"].lower()):
            parts.append(
                '<div class="source"><div class="src-disc">'
                f'<strong>{esc(row["disc"])}</strong>'
                f'<span class="platform">{esc(row["platform"])}</span>'
                f'<span class="variants">{esc(row["variants"])}</span></div>'
                "</div>")
        parts.append("</div>")

    if project.get("how_it_works"):
        parts.append("<h2>Under the hood</h2>")
        parts.extend(f"<p>{esc(t)}</p>" for t in project["how_it_works"])

    if project.get("tech_spec"):
        parts.append("<h2>Making your own packs</h2>")
        parts.extend(f"<p>{esc(t)}</p>" for t in project["tech_spec"])

    if project.get("faq"):
        parts.append("<h2>FAQ</h2>")
        for item in project["faq"]:
            body = "".join(f"<p>{esc(t)}</p>" for t in item["a"])
            parts.append(f'<details class="faq"><summary>{esc(item["q"])}'
                         f"</summary>{body}</details>")

    parts.append(render_release_history(project))

    if project.get("links"):
        parts.append("<h2>Source &amp; related projects</h2><ul>")
        for link in project["links"]:
            parts.append(f'<li><a href="{esc(link["url"])}">'
                         f'{esc(link["label"])}</a>, {esc(link["note"])}</li>')
        parts.append("</ul>")

    if project.get("notes"):
        parts.append('<h2>Notes</h2><ul class="notes">')
        parts.extend(f"<li>{esc(n)}</li>" for n in project["notes"])
        parts.append("</ul>")

    title = f"{project['title']} · {site['title']}"
    return page(site, title, "\n".join(parts), depth=1)


def write_redirects(site: dict, patch: dict) -> None:
    """A renamed page leaves a stub at each old slug that forwards to the
    new one, so links already out in the world keep working."""
    for old in patch.get("redirect_from", []):
        target = f"../{patch['slug']}/"
        out_dir = DOCS / old
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{esc(patch['title'])} · {esc(site['title'])}</title>
<meta http-equiv="refresh" content="0; url={target}">
<link rel="canonical" href="{target}">
</head>
<body>
<p>This page has moved to <a href="{target}">{esc(patch['title'])}</a>.</p>
</body>
</html>
""")
        print(f"{old}: redirect to {patch['slug']} written")


def render_legal(site: dict) -> str:
    contact = esc(site.get("contact_note", ""))
    body = f"""<h1>Legal &amp; disclaimers</h1>

<h2>Ownership and trademarks</h2>
<p>This site and the patches on it are unofficial fan projects. They are not
affiliated with, sponsored by, or endorsed by Capcom Co., Ltd., Sega, or any
other rights holder. All game titles, logos, characters, artwork, and other
game content referenced or shown in screenshots remain the property of their
respective owners.</p>

<h2>What is (and is not) distributed here</h2>
<p>No ROM images, disc images, or other copies of any game are hosted on this
site, and none will be provided on request. Downloads consist of binary
difference patches (IPS), MiSTer MRA patch overlays, FPGA core builds with
their corresponding source, and open-source tooling (including audio-pack
builders that run against discs you own) only: they describe the changes made to a game and are useless without your
own copy of that game.</p>
<p>To use a patch you must own the game in question (an original board,
cartridge, or GD-ROM, or a lawfully obtained copy) and produce your own dump
of it. Where a patch reuses official English text, that text is only ever
reconstructed against a copy of the game you already own.</p>

<h2>Use</h2>
<p>Patches are free. Do not sell them, bundle them with ROM images, or
distribute pre-patched ROMs. If you redistribute a patch, redistribute it
unmodified and with its documentation.</p>

<h2>No warranty</h2>
<p>Everything here is provided &ldquo;as is&rdquo;, without warranty of any
kind. Applying patches to ROM images, and using those images on emulators or
original hardware, is entirely at your own risk.</p>

<h2>Takedown</h2>
<p>If you are a rights holder and believe anything here goes beyond fair
fan-work practice, please get in touch and it will be addressed promptly.
{contact}</p>

<a class="back" href="./">&larr; Back</a>"""
    return page(site, f"Legal · {site['title']}", body)


# ------------------------------------------------------------------ main


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Build the site into docs/.")
    ap.add_argument("--only", help="comma-separated slugs to rebuild from sources; "
                    "everything else reuses what is committed")
    args = ap.parse_args()
    if args.only:
        ONLY.update(x.strip() for x in args.only.split(",") if x.strip())
    config = json.loads((ROOT / "data" / "patches.json").read_text())
    site, patches = config["site"], config["patches"]
    projects = [p for p in config.get("projects", []) if not p.get("hidden")]
    global SITE
    SITE = site

    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("")
    shutil.copyfile(ROOT / "site" / "style.css", DOCS / "style.css")

    patches = sorted(
        (p for p in patches if not p.get("hidden")),
        key=lambda p: p["title"].lower(),
    )

    thumbs = {}
    for patch in patches:
        slug = patch["slug"]
        if patch.get("builds"):
            builds = copy_build_titles(patch)
            shots = copy_screenshots(patch)
            bundle = build_reconstruction_kit(patch) or build_downloads(patch)
            if builds and slug not in thumbs:
                key = patch.get("thumbnail_build")
                chosen = next((b for b in builds if b["key"] == key), builds[0])
                thumbs[slug] = chosen["img"].replace("../", "")
            out_dir = DOCS / slug
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "index.html").write_text(
                render_builds_page(site, patch, builds, bundle, shots))
            print(f"{slug}: builds page rendered ({len(builds)} builds, "
                  f"{len(shots)} screenshots)")
            continue
        # A single-build patch can still ship a reconstruction kit: Final
        # Fight CD has one set per region, not a build matrix, but its
        # download IS the kit.
        bundle = build_reconstruction_kit(patch) or build_downloads(patch)
        shots = copy_screenshots(patch)
        for shot in shots:
            if patch.get("thumbnail") is False:
                break
            candidate = shot.get("after") or shot.get("single")
            if candidate and slug not in thumbs:
                thumbs[slug] = candidate.replace("../", "")  # index is one level up
        out_dir = DOCS / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(render_patch_page(site, patch, bundle, shots))
        write_redirects(site, patch)
        print(f"{slug}: page rendered ({len(shots)} screenshot blocks)")

    project_thumbs = {}
    for project in projects:
        slug = project["slug"]
        kit = copy_project_kit(project)
        shots = copy_screenshots(project)
        for shot in shots:
            candidate = shot.get("after") or shot.get("single")
            if candidate and slug not in project_thumbs:
                project_thumbs[slug] = candidate.replace("../", "")
        out_dir = DOCS / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(
            render_project_page(site, project, kit, shots))
        print(f"{slug}: project page rendered ({len(shots)} screenshot blocks)")

    (DOCS / "index.html").write_text(
        render_index(site, patches, thumbs, projects, project_thumbs))
    (DOCS / "legal.html").write_text(render_legal(site))
    print(f"\nSite built into {DOCS}")


if __name__ == "__main__":
    main()
