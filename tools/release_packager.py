#!/usr/bin/env python3
"""Deterministic IPS/MRA packaging used by pinned Capcom candidates.

This module is not part of the site renderer. Capcom's isolated candidate
factory loads it from an explicit arcade-patches commit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import textwrap
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ipsutil
import mra as mralib

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SITE: dict = {}


def author_line() -> str:
    """One-line author credit, e.g. 'Steve Gordon (https://x.com/strygo)'."""
    name = SITE.get("author")
    if not name:
        return ""
    url = SITE.get("author_url")
    return f"{name} ({url})" if url else name

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
#
# Superseded kits STAY in docs/downloads.  Nothing here prunes them, and a
# release must not delete them by hand: romhacking.net entries, forum posts
# and bookmarks link the exact filename of the version they were written
# against, and removing it turns every one of those links into a 404.  The
# pages only ever link the current version, so an old zip costs nothing but
# the disk it sits on.


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
    if fmt == "ips":
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
        if hb and patch.get("mister_hbmame"):
            lines.append(f"{pad}                          out/mister/games/hbmame/{hb}.zip")
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
        if patch.get("mister_hbmame"):
            lines += [
                "",
                "out/mister/ also holds the HBMAME set in games/hbmame/. This MRA doesn't use",
                "it, but the CPS+ Arrange and HD Remix MRAs load it: if you use CPS+, copy",
                "the games folder from out/mister/ to the card as well.",
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


def build_rom_downloads(patch: dict) -> dict:
    """Build the IPS and MRA downloads from explicit source archives.

    One IPS bundle carries the patches (ips/, or ips/<variant>/ when the
    patch has several builds on one stock set), the verified MiSTer overlays
    under mister/, and apply.py, which writes mame/, hbmame/ and mister/ in
    one run.  The MRAs are also packaged on their own for MiSTer-only users."""
    slug = patch["slug"]
    art = patch.get("artifact")
    if not art:
        raise SystemExit(f"{slug}: no artifact configuration")
    specs = art.get("variants")
    ips_zipname = f"{slug}-{patch['version']}-ips.zip"
    mra_zipname = f"{slug}-{patch['version']}-mra.zip" if patch.get("mra") else None
    ips_path = DOCS / "downloads" / ips_zipname
    stock_path = resolve(art["stock_zip"])
    builds = specs or [{"key": None, "patched_zip": art["patched_zip"],
                        "mra": patch.get("mra") if patch.get("mra") else None,
                        "hbmame": patch.get("hbmame")}]
    if not stock_path.is_file():
        raise SystemExit(f"{slug}: stock archive is missing: {stock_path}")
    missing = [str(resolve(b["patched_zip"])) for b in builds
               if not resolve(b["patched_zip"]).is_file()]
    if missing:
        raise SystemExit(f"{slug}: patched archives are missing: {missing}")

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
    if patch.get("mister_hbmame"):
        manifest["mister_hbmame"] = True
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
    if texts:
        write_mra_zip(patch, texts, DOCS / "downloads" / mra_zipname, stamp, members, variants)

    result = {"kind": "rom", "members": members, "ips": download_info(ips_zipname)}
    if specs:
        result["variants"] = variants
    if mra_zipname and (DOCS / "downloads" / mra_zipname).exists():
        result["mra"] = download_info(mra_zipname)
    return result
