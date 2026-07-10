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
    "in-development": "In development",
    "research": "Research",
}


def resolve(path: str) -> Path:
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


def make_readme(patch: dict, members: list, fmt: str) -> str:
    """Project readme.txt shipped inside every download (fmt: 'ips' or 'mra')."""
    title = f"{patch['title']} — {patch['subtitle']}"
    lines = [
        title,
        "=" * len(title),
        "",
        f"Version:  {patch['version']} ({patch['date']})",
        f"Target:   MAME set '{patch['set']}' — {patch['game']}",
        f"Hardware: {patch['hardware']}",
    ]
    if author_line():
        lines.append(f"Patch by: {author_line()}")
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
        lines += [
            "HOW TO APPLY (recommended)",
            "",
            f"    python3 apply.py /path/to/{setname}.zip",
            "",
            "This verifies every file, applies the IPS patches, and writes",
            f"{setname}_patched.zip. Rename it to {setname}.zip and place it ahead of",
            "the stock set in your MAME rompath. MAME reports checksum warnings for",
            "the patched program ROMs; that is expected and the game runs normally.",
            "",
            "HOW TO APPLY (any IPS patcher)",
            "",
            f"Extract {setname}.zip, apply each file in ips/ to the ROM file of the",
            "same name (Flips, Lunar IPS, or any IPS tool), then re-zip everything",
            f"as {setname}.zip.",
            "",
            "CHANGED FILES",
            "",
        ]
        for m in members:
            if m["action"] == "patch":
                lines.append(
                    f"    {m['name']}  ({human_size(m['size'])})  "
                    f"CRC32 {m['stock_crc32']} -> {m['patched_crc32']}"
                )
        lines += ["", "All other files in the set are unmodified."]
    else:
        mra_cfg = patch["mra"]
        lines += [
            "MISTER SETUP",
            "",
            f"1. Copy \"{mra_cfg['filename']}\" anywhere under _Arcade/ on your MiSTer.",
            f"2. Have the stock, unmodified romset at games/mame/{setname}.zip",
            "   (split MAME sets also need qsound.zip next to it).",
            "3. You need Jotego's jtcps2 core; the standard MiSTer downloader /",
            "   update_all installs it automatically.",
            "",
            "The MRA references your original romset and applies the translation in",
            "memory while the game loads. Nothing on your SD card is modified. The",
            "translation keeps its own settings and saves under the name",
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


def zip_writer(out_path: Path, stamp: tuple):
    """Deterministic zip member writer."""

    def write(z: zipfile.ZipFile, name: str, data: bytes) -> None:
        info = zipfile.ZipInfo(name, date_time=stamp)
        info.external_attr = 0o644 << 16
        z.writestr(info, data, zipfile.ZIP_DEFLATED, 9)

    return write


def mra_header_note(patch: dict) -> str:
    mra_cfg = patch["mra"]
    credit = f"\n    Patch by {author_line()}.\n" if author_line() else ""
    return f"""    {patch['title']} — English translation patch ({patch['version']})
    An unofficial fan translation of {patch['game']}.
{credit}
    This is a patch-overlay MRA: it references the ORIGINAL, unmodified
    MAME romset ({patch['set']}.zip) and applies the translation in memory
    while the game loads. It contains no ROM data. Settings and saves use
    the name '{mra_cfg['setname']}'.

    Base MRA and jtcps2 core by Jose Tejada (jotego); see his header below.
    Not affiliated with or endorsed by Capcom. Free patch; do not sell."""


def build_mra_zip(patch: dict, stock_path: Path, patched_path: Path,
                  out_path: Path, stamp: tuple, members: list) -> None:
    """Generate and verify the MiSTer patch-overlay MRA, packaged with readme."""
    mra_cfg = patch["mra"]
    base_text = resolve(mra_cfg["base"]).read_text()

    stock_rom = mralib.assemble(base_text, mralib.ZipSource([stock_path]))
    declared = mralib.declared_asm_md5(base_text)
    if declared and hashlib.md5(stock_rom).hexdigest() != declared:
        raise SystemExit(f"{patch['slug']}: stock assembly does not match base MRA asm_md5")

    patched_rom = mralib.assemble(base_text, mralib.ZipSource([patched_path], check_crc=False))
    runs = mralib.diff_runs(stock_rom, patched_rom)
    text = mralib.make_patch_mra(
        base_text,
        name=mra_cfg["name"],
        setname=mra_cfg["setname"],
        patched_rom=patched_rom,
        runs=runs,
        note=mra_header_note(patch),
    )
    # Round-trip proof: the overlay MRA over the stock set must assemble to
    # exactly what the official MRA produces from the patched set.
    if mralib.assemble(text, mralib.ZipSource([stock_path])) != patched_rom:
        raise SystemExit(f"{patch['slug']}: MRA round-trip verification failed")

    write = zip_writer(out_path, stamp)
    with zipfile.ZipFile(out_path, "w") as z:
        write(z, mra_cfg["filename"], text.encode())
        write(z, "readme.txt", make_readme(patch, members, "mra").encode())
    print(f"{patch['slug']}: MRA rebuilt and verified ({len(runs)} patch runs) -> {out_path.name}")


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

    if stock_path.exists() and patched_path.exists():
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
                "title": f"{patch['title']} — {patch['subtitle']}",
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
        "title": f"{patch['title']} — {patch['subtitle']}",
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


def build_rom_downloads(patch: dict) -> dict | None:
    """Build (or reuse) the IPS and MRA downloads. Returns render info or None."""
    slug = patch["slug"]
    art = patch.get("artifact")
    if not art:
        return None

    ips_zipname = f"{slug}-{patch['version']}-ips.zip"
    mra_zipname = f"{slug}-{patch['version']}-mra.zip" if patch.get("mra") else None
    ips_path = DOCS / "downloads" / ips_zipname
    persist_path = GENERATED / f"{slug}.json"

    stock_path = resolve(art["stock_zip"])
    patched_path = resolve(art["patched_zip"])

    if stock_path.exists() and patched_path.exists():
        with zipfile.ZipFile(stock_path) as z:
            stock = {i.filename: z.read(i.filename) for i in z.infolist()}
        with zipfile.ZipFile(patched_path) as z:
            patched = {i.filename: z.read(i.filename) for i in z.infolist()}
        if set(stock) != set(patched):
            raise SystemExit(f"{slug}: stock and patched zips have different members")

        members, ips_files = [], {}
        for name in sorted(stock):
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

        manifest = {
            "title": f"{patch['title']} — {patch['subtitle']}",
            "version": patch["version"],
            "game": patch["game"],
            "set": patch["set"],
            "hardware": patch["hardware"],
            "members": members,
        }
        ips_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = tuple(int(x) for x in patch["date"].split("-")) + (0, 0, 0)

        write = zip_writer(ips_path, stamp)
        with zipfile.ZipFile(ips_path, "w") as z:
            write(z, "readme.txt", make_readme(patch, members, "ips").encode())
            write(z, "manifest.json", json.dumps(manifest, indent=2).encode())
            write(z, "apply.py", (ROOT / "tools" / "bundle_apply.py").read_bytes())
            for name, ips in sorted(ips_files.items()):
                write(z, f"ips/{name}.ips", ips)
        print(f"{slug}: IPS bundle rebuilt from sources ({len(ips_files)} patched ROMs) -> {ips_zipname}")

        if mra_zipname:
            build_mra_zip(patch, stock_path, patched_path,
                          DOCS / "downloads" / mra_zipname, stamp, members)

        GENERATED.mkdir(parents=True, exist_ok=True)
        persist_path.write_text(
            json.dumps({"ips_zipname": ips_zipname, "mra_zipname": mra_zipname,
                        "members": members}, indent=2) + "\n"
        )
    elif persist_path.exists() and ips_path.exists():
        saved = json.loads(persist_path.read_text())
        members = saved["members"]
        print(f"{slug}: sources unavailable, reusing existing downloads")
    else:
        print(f"{slug}: WARNING: no sources and no existing downloads; downloads omitted")
        return None

    result = {"kind": "rom", "members": members, "ips": download_info(ips_zipname)}
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
            if src_path.exists():
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
    <a class="home" href="{rel}index.html">{esc(site['title'])}</a>
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


def render_index(site: dict, patches: list, thumbs: dict) -> str:
    intro = "\n".join(f"<p>{esc(p)}</p>" for p in site["intro"])
    cards = []
    for patch in patches:
        thumb = thumbs.get(patch["slug"])
        thumb_html = (
            f'<img class="thumb" src="{esc(thumb)}" alt="{esc(patch["title"])} screenshot">'
            if thumb
            else ""
        )
        pill = status_pill(patch)
        heading = esc(patch["title"]) + (f" {pill}" if pill else "")
        cards.append(f"""<a class="card" href="{esc(patch['slug'])}/index.html">
  <div>
    <h2>{heading}</h2>
    <div class="sub">{esc(patch['subtitle'])} · {esc(patch['game'])}</div>
    <p class="summary">{esc(patch['summary'])}</p>
  </div>
  {thumb_html}
</a>""")
    body = f"{intro}\n{''.join(cards)}"
    return page(site, site["title"], body)


def render_shots(shots: list) -> str:
    if not shots:
        return ""
    out = ["<h2>Screenshots</h2>"]
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
{download_box("NAOMI GD-ROM patch — data-free, drives your own chdman", info)}
<p>The download contains <strong>no game data at all</strong> — just a description of
the change ({changed} bytes in track {m['patch']['track']}) and an apply script. You
need your own dump of <strong>{esc(patch['game'])}</strong>: the MAME set
<code>{setname}.zip</code> plus <code>{setname}/{chd}</code>.</p>
<h2>How to apply</h2>
<pre><code>unzip {esc(info['zipname'])} -d {setname}-patch
cd {setname}-patch
python3 apply.py /path/to/{setname}/{chd}</code></pre>
<p>Requirements: Python 3, <code>chdman</code> (it ships with every MAME
distribution), and about 2.5&nbsp;GB of temporary disk space. The script verifies
your dump, unpacks it with chdman, patches {changed} bytes, rebuilds the CHD, and
verifies the result against the checksum below — if anything does not match,
nothing is kept.</p>
<p>Rename the verified output to <code>{chd}</code> inside an
<code>{setname}/</code> folder placed ahead of the stock set in your MAME rompath.
MAME reports a checksum warning for the patched CHD — that is expected, and the
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
    rows = "\n".join(
        f'<tr><td class="mono">{esc(m["name"])}</td><td>{human_size(m["size"])}</td>'
        f'<td class="mono">{esc(m["stock_crc32"])}</td><td class="mono">{esc(m["patched_crc32"])}</td></tr>'
        for m in bundle["members"]
        if m["action"] == "patch"
    )
    setname = esc(patch["set"])
    ips = bundle["ips"]
    parts = ["<h2>Downloads</h2>"]
    parts.append(download_box("ROM patch (IPS) — MAME, emulators, original hardware", ips))
    mra_info = bundle.get("mra")
    if mra_info:
        parts.append(download_box("MiSTer (MRA patch overlay)", mra_info))
    parts.append(f"""<p>Neither download contains ROM data: you need your own dump of
<strong>{esc(patch['game'])}</strong> as the MAME set <code>{setname}.zip</code>.
Each zip includes a <code>readme.txt</code> with full instructions.</p>
<h2>How to apply (ROM patch)</h2>
<pre><code>unzip {esc(ips['zipname'])} -d {setname}-patch
cd {setname}-patch
python3 apply.py /path/to/{setname}.zip</code></pre>
<p>This verifies every file against the checksums below before and after patching,
then writes <code>{setname}_patched.zip</code>. Rename it to <code>{setname}.zip</code>
and put it ahead of the stock set in your MAME rompath. Alternatively, apply each
file in <code>ips/</code> with any IPS patcher (Flips, Lunar IPS, …) to the ROM file
of the same name and re-zip the set yourself.</p>
<p>MAME reports checksum warnings for the patched program ROMs when loading —
that is expected, and the game runs normally.</p>""")
    if mra_info:
        mra_cfg = patch["mra"]
        parts.append(f"""<h2>MiSTer</h2>
<p>Copy <code>{esc(mra_cfg['filename'])}</code> from the MRA download anywhere under
<code>_Arcade/</code> on your MiSTer, and have the stock, unmodified romset at
<code>games/mame/{setname}.zip</code> (split MAME sets also need <code>qsound.zip</code>).
You need Jotego's <code>jtcps2</code> core, which the standard MiSTer downloader
(update_all) installs automatically.</p>
<p>The MRA references your original romset and applies the translation in memory
while the game loads — nothing on your SD card is modified. The translation keeps
its own settings and saves under the setname <code>{esc(mra_cfg['setname'])}</code>.</p>""")
    parts.append(f"""<h3>Changed ROMs</h3>
<table>
<tr><th>File</th><th>Size</th><th>Original CRC32</th><th>Patched CRC32</th></tr>
{rows}
</table>""")
    return "\n".join(parts)


def render_patch_page(site: dict, patch: dict, bundle: dict | None, shots: list) -> str:
    parts = ['<a class="back" href="../index.html">&larr; All patches</a>']
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

    parts.append(render_download(patch, bundle))

    if patch.get("notes"):
        parts.append('<h2>Notes</h2><ul class="notes">')
        parts.extend(f"<li>{esc(n)}</li>" for n in patch["notes"])
        parts.append("</ul>")

    title = f"{patch['title']} — {site['title']}"
    return page(site, title, "\n".join(parts), depth=1)


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
difference patches (IPS), MiSTer MRA patch overlays, and open-source tooling
only: they describe the changes made to a game and are useless without your
own copy of that game.</p>
<p>To use a patch you must own the game in question — an original board,
cartridge, or GD-ROM, or a lawfully obtained copy — and produce your own dump
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

<a class="back" href="index.html">&larr; Back</a>"""
    return page(site, f"Legal — {site['title']}", body)


# ------------------------------------------------------------------ main


def main() -> None:
    config = json.loads((ROOT / "data" / "patches.json").read_text())
    site, patches = config["site"], config["patches"]
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
        bundle = build_downloads(patch)
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
        print(f"{slug}: page rendered ({len(shots)} screenshot blocks)")

    (DOCS / "index.html").write_text(render_index(site, patches, thumbs))
    (DOCS / "legal.html").write_text(render_legal(site))
    print(f"\nSite built into {DOCS}")


if __name__ == "__main__":
    main()
