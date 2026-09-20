#!/usr/bin/env python3
"""Build the static site into docs/ from tracked publication data.

Released patch downloads come only from data/releases.json and are verified
in place before their pages render.  The site build never regenerates or
replaces an inventoried release from a sibling development checkout.
"""

import hashlib
import json
import os
import re
import shutil
import sys
from html import escape as esc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from release_inventory import load_inventory, published_bundle, validate_inventory

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# Set from the site config at the start of main().
SITE: dict = {}
# `--only slug,...` refreshes only those screenshots/title images from their
# declared tracked sources. Downloads always come from the release inventory.
ONLY: set = set()


def live(slug: str) -> bool:
    return not ONLY or slug in ONLY


STATUS_LABELS = {
    "released": "Released",
    "release-candidate": "Release candidate",
    "beta": "Beta",
    "coming-soon": "Coming soon",
    "in-development": "In development",
    "research": "Research",
}


def resolve(path: str) -> Path:
    """Resolve a tracked page asset path."""
    def _default(m):
        return os.environ.get(m.group(1)) or m.group(2)
    path = re.sub(r"\$\{(\w+):-([^}]*)\}", _default, path)
    path = os.path.expandvars(path)
    p = Path(path).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


# ------------------------------------------------------ published bundle shape


def patch_noun(patch: dict) -> str:
    return patch.get("patch_noun", "translation")


def mra_zip_path(patch: dict, mra_cfg: dict) -> str:
    parts = [(patch.get("mra") or {}).get("zip_dir", ""),
             mra_cfg.get("subdir", ""), mra_cfg["filename"]]
    return "/".join(x.strip("/") for x in parts if x and x.strip("/"))


def build_outputs(patch: dict, variants: list | None) -> list:
    """Paths written by a published kit, for display on its page."""
    setname = patch["set"]
    builds = variants or [{"key": None, "label": None,
                           "hbmame": patch.get("hbmame"),
                           "mra": patch.get("mra") if patch.get("mra", {}).get("filename") else None}]
    out = []
    for variant in builds:
        mame = None
        if patch.get("mame_build", True):
            mame = (f"mame/{variant['key']}/{setname}.zip" if variant["key"]
                    else f"mame/{setname}.zip")
        mra = (f"mister/{mra_zip_path(patch, variant['mra'])}"
               if variant.get("mra") else None)
        out.append((variant["label"], mame,
                    (variant.get("hbmame") or {}).get("setname"), mra))
    return out


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


def versioned(html: str, depth: int) -> str:
    """Append ?v=<content hash> to every local image, stylesheet and download
    URL. Files that don't exist yet are left alone."""
    here = DOCS if depth == 0 else DOCS / "_"

    def sub(m):
        attr, url = m.group(1), m.group(2)
        if "://" in url or url.startswith(("#", "mailto:")) or "?" in url:
            return m.group(0)
        target = (here / url).resolve() if depth else (DOCS / url).resolve()
        if not target.is_file() or DOCS.resolve() not in target.parents:
            return m.group(0)
        tag = hashlib.sha256(target.read_bytes()).hexdigest()[:10]
        return f'{attr}="{url}?v={tag}"'

    return re.sub(r'\b(src|href)="((?:\.\./)*(?:img/|downloads/|style\.css)[^"]*)"', sub, html)


def page(site: dict, title: str, body: str, depth: int = 0) -> str:
    rel = "../" * depth
    return versioned(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="icon" href="{rel}favicon.svg" type="image/svg+xml">
<link rel="alternate icon" href="{rel}favicon.ico" sizes="16x16 32x32 48x48">
<link rel="apple-touch-icon" href="{rel}apple-touch-icon.png">
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
""", depth)


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


def what_it_is(entry: dict) -> str:
    """One line saying what an entry is, for its first appearance in the
    changelog: the opening sentence of its summary, else its subtitle."""
    summary = (entry.get("summary") or "").strip()
    if summary:
        first = summary.split(". ")[0].rstrip(".")
        if len(first) <= 180:
            return first + "."
    return (entry.get("subtitle") or "").strip()


def changelog_days(patches: list, projects: list = ()) -> list:
    """Every release of every entry, newest day first.

    One event per release_history record; an entry without a history still
    gets the one event its own date and version describe, so a first release
    is not missing from the changelog.  The oldest event for an entry is what
    added it to the site, the rest are updates.
    """
    events = []
    for entry in list(projects) + list(patches):
        hist = entry.get("release_history") or []
        if hist:
            for rel in hist:
                events.append({"slug": entry["slug"], "title": entry["title"],
                               "date": rel["date"], "version": rel["version"],
                               "items": list(rel["items"]), "what": what_it_is(entry),
                               "has_history": True})
        elif entry.get("date") and entry.get("version"):
            events.append({"slug": entry["slug"], "title": entry["title"],
                           "date": entry["date"], "version": entry["version"],
                           "items": [], "what": what_it_is(entry),
                           "has_history": False})
    first = {}
    for ev in sorted(events, key=lambda e: (e["date"], e["slug"])):
        first.setdefault(ev["slug"], ev["date"])
    for ev in events:
        ev["kind"] = "added" if first[ev["slug"]] == ev["date"] else "updated"
    days = {}
    for ev in events:
        days.setdefault(ev["date"], []).append(ev)
    return [{"date": d, "events": sorted(days[d], key=lambda e: e["title"])}
            for d in sorted(days, reverse=True)]


def changelog_link(ev: dict, depth: int = 0) -> str:
    """A release links to the entry's own release history where it has one."""
    rel = "../" * depth
    frag = "#history" if ev["has_history"] else ""
    return f'{rel}{esc(ev["slug"])}/{frag}'


def render_changelog_entry(ev: dict, depth: int = 0, with_items: bool = False) -> str:
    label = "New" if ev["kind"] == "added" else esc(ev["version"])
    items = ""
    if with_items:
        lines = ev["items"] if ev["kind"] == "updated" else (
            [ev["what"]] if ev.get("what") else [])
        if lines:
            items = "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in lines) + "</ul>"
    return (f'<li><a href="{changelog_link(ev, depth)}">{esc(ev["title"])}</a>'
            f' <span class="badge plain">{label}</span>{items}</li>')


def render_contact_section(site: dict) -> str:
    """Where to send a correction.  The site has no issue tracker, so this is
    the one place a reader is pointed at."""
    url = site.get("author_url")
    if not url:
        return ""
    handle = url.rstrip("/").rsplit("/", 1)[-1]
    return f"""<section class="contact" id="contact">
<h2 class="kind">Contact</h2>
<p>Spotted something wrong, or got a patch working on real hardware?
Say so to <a href="{esc(url)}">@{esc(handle)}</a> on X.</p>
</section>"""


def render_changelog_section(site: dict, days: list) -> str:
    """The home page's abridged changelog: the most recent days only."""
    limit = site.get("changelog_recent", 8)
    if not days:
        return ""
    out = ['<section class="changelog" id="changes">',
           '<h2 class="kind">Recent Changes</h2>']
    left = limit
    for day in days:
        if left <= 0:
            break
        evs = day["events"][:left]
        left -= len(evs)
        out.append(f'<h3>{esc(day["date"])}</h3><ul class="changes">')
        out.extend(render_changelog_entry(ev) for ev in evs)
        out.append("</ul>")
    out.append('<p class="more"><a href="changelog/">Full changelog</a></p>')
    out.append("</section>")
    return "\n".join(out)


def render_changelog_page(site: dict, days: list) -> str:
    out = ['<a class="back" href="../">&larr; All patches</a>',
           "<h1>Changelog</h1>",
           "<p>Every release, newest first. Each entry links to that patch's own "
           "release history.</p>"]
    for day in days:
        out.append(f'<h2>{esc(day["date"])}</h2><ul class="changes">')
        out.extend(render_changelog_entry(ev, depth=1, with_items=True)
                   for ev in day["events"])
        out.append("</ul>")
    return page(site, f"Changelog · {site['title']}", "\n".join(out), depth=1)


def render_index(site: dict, patches: list, thumbs: dict,
                 projects: list = (), project_thumbs: dict = {}) -> str:
    """The home page: the intro, then one section per kind of project (site
    `sections`, in order), each with its description and its cards.  Every
    patch and project names its section; an entry without one fails the
    build rather than silently disappearing from the page."""
    intro = "\n".join(f"<p>{esc(p)}</p>" for p in site["intro"])
    keys = [sec["key"] for sec in site["sections"]]
    for entry in list(projects) + list(patches):
        if entry.get("section") not in keys:
            raise SystemExit(f"{entry['slug']}: section {entry.get('section')!r} "
                             f"is not one of {keys}")

    def featured_card(proj: dict) -> str:
        thumb = project_thumbs.get(proj["slug"])
        thumb_html = (
            f'<img class="thumb" src="{esc(thumb)}" '
            f'alt="{esc(proj["title"])} screenshot">' if thumb else ""
        )
        stats = proj.get("featured_stats", "")
        stats_html = f'<div class="stats">{esc(stats)}</div>' if stats else ""
        return f"""<a class="card featured" href="{esc(proj['slug'])}/">
  <div>
    <h2>{esc(proj["title"])}</h2>
    <div class="sub">{esc(proj['subtitle'])}</div>
    <p class="summary">{esc(proj['summary'])}</p>
    {stats_html}
  </div>
  {thumb_html}
</a>"""

    def patch_card(patch: dict) -> str:
        thumb = thumbs.get(patch["slug"])
        thumb_html = (
            f'<img class="thumb" src="{esc(thumb)}" alt="{esc(patch["title"])} screenshot">'
            if thumb
            else ""
        )
        return f"""<a class="card" href="{esc(patch['slug'])}/">
  <div>
    <h2>{esc(patch["title"])}</h2>
    <div class="sub">{esc(patch['subtitle'])} · {esc(patch['game'])}</div>
    <p class="summary">{esc(patch['summary'])}</p>
  </div>
  {thumb_html}
</a>"""

    sections = []
    for sec in site["sections"]:
        cards = ([featured_card(p) for p in projects if p["section"] == sec["key"]]
                 + [patch_card(p) for p in patches if p["section"] == sec["key"]])
        if not cards:
            continue
        sections.append(f"""<section class="kind" id="{esc(sec['key'])}">
<h2 class="kind">{esc(sec['title'])}</h2>
<p class="kind-intro">{esc(sec['description'])}</p>
{''.join(cards)}
</section>""")
    changes = render_changelog_section(site, changelog_days(patches, projects))
    body = (f"{intro}\n" + "\n".join(sections)
            + f"\n{changes}\n{render_contact_section(site)}")
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


def tools_notice(bundle: dict) -> str:
    return (f'<p>Tools updated {esc(bundle["tools_updated"])}. Game version unchanged.</p>'
            if bundle.get("tools_updated") else "")


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
    parts = ["<h2>Downloads</h2>", tools_notice(bundle)]
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

    if bundle.get("kit_revision", 1) > 1:
        parts.append("<p>Merged, split and complete sets work, including nested or renamed ROMs. "
                     "Use <code>--rompath /path/to/roms</code> to search a collection (repeatable), "
                     "and <code>--check</code> to check inputs before building. ZIPs and extracted folders "
                     "need no extra software; 7z archives require 7-Zip. QSound can stay in your emulator's "
                     "ROM path; valid firmware already embedded in a source set is kept in complete outputs.</p>")
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
        if hb and patch.get("mister_hbmame"):
            listing.append(f"{pad}out/mister/games/hbmame/{hb}.zip")
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
        if patch.get("mister_hbmame"):
            parts.append("""<p><code>out/mister/</code> also holds the HBMAME set in <code>games/hbmame/</code>.
The MRA above doesn't need it, but the <a href="../cps-plus/">CPS+</a> Arrange and HD Remix MRAs load it,
so if you use CPS+, copy the <code>games</code> folder from <code>out/mister/</code> to your card as well.</p>""")

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
    inputs_note = ("<p>Merged, split and complete archives and extracted ROM folders are accepted. "
                   "Use <code>--rompath /path/to/roms</code> (repeatable) and <code>--check</code> "
                   "to verify arcade inputs before rebuilding from your disc. "
                   "7z needs installed 7-Zip; ZIP does not.</p>" if kit.get("kit_revision", 1) > 1 else "")
    return f"""<h2>Download</h2>
{tools_notice(kit)}
{inputs_note}
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
    parts = ['<h2 id="history">Release history</h2>']
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
    inventory = load_inventory(ROOT / "data" / "releases.json")
    validate_inventory(ROOT, inventory)
    site, patches = config["site"], config["patches"]
    projects = [p for p in config.get("projects", []) if not p.get("hidden")]
    global SITE
    SITE = site

    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("")
    shutil.copyfile(ROOT / "site" / "style.css", DOCS / "style.css")
    for icon in ("favicon.svg", "favicon.ico", "apple-touch-icon.png"):
        shutil.copyfile(ROOT / "site" / icon, DOCS / icon)

    patches = sorted(
        (p for p in patches if not p.get("hidden")),
        key=lambda p: p["title"].lower(),
    )

    thumbs = {}
    for patch in patches:
        slug = patch["slug"]
        bundle = published_bundle(ROOT, patch, inventory)
        if patch.get("builds"):
            builds = copy_build_titles(patch)
            shots = copy_screenshots(patch)
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
        kit = published_bundle(ROOT, project, inventory)
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
    days = changelog_days(patches, projects)
    (DOCS / "changelog").mkdir(parents=True, exist_ok=True)
    (DOCS / "changelog" / "index.html").write_text(render_changelog_page(site, days))
    print(f"changelog: {sum(len(d['events']) for d in days)} releases over {len(days)} days")
    print(f"\nSite built into {DOCS}")


if __name__ == "__main__":
    main()
