#!/usr/bin/env python3
"""Build Final Fight EX (CPS-2) from your own copies.

This tool ships no game data.  It rebuilds the CPS-2 sets from files you
already own:
  * your Final Fight CD disc images: the USA disc for the USA build, the
    Japanese disc for the Japan build and the English restoration, and
  * your arcade romsets: Final Fight (ffight, ffightu and ffightj) and
    Street Fighter Alpha 3 (sfa3u).

Only the builds whose disc you give are made.  Every member is
checksum-verified against the known build.  See README.txt.
"""
import argparse
import importlib
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from rom_sources import require_path, default_output, check_output, print_completion

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))         # the kit's own folder only: ffex_kit, packs and their modules
from rom_sources import Resolver, RomError, QSOUND, search_paths, publish_tree  # noqa: E402

KIT = json.loads((HERE / "kit.json").read_text())
PLATFORMS = ("mister", "hbmame", "darksoft")


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def kit_module(name):
    """A module the release stages beside this tool (ffex_kit, packs), imported when first needed."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        fail(f"this kit is incomplete: {name}.py is missing beside apply.py")


def check_mra(text, members):
    """Every arcade part the MRA names is a member of the set, with its CRC."""
    rom = next(r for r in ET.fromstring(text).findall("rom") if r.get("index") == "0")
    for part in rom.iter("part"):
        name = part.get("name")
        if not name or part.get("zip") or name == QSOUND["name"]:
            continue
        if name not in members or f"{zlib.crc32(members[name]):08x}" != part.get("crc"):
            raise RomError(f"the MiSTer file does not match the built {name}")


def mister_mra(meta, members):
    text = (HERE / "mras" / f"{meta['mra']}.mra").read_text()
    check_mra(text, members)
    # The QSound firmware may be missing from the set: fall back on the usual device zips.
    return re.sub(r'zip="(/hbmame/[^"|]+\.zip)"', r'zip="\1|qsound.zip|qsound_hle.zip"', text)


def main():
    ap = argparse.ArgumentParser(description=KIT["description"],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--disc-jp", type=Path, help="your Final Fight CD (Japan) disc: its .cue, a .chd, or a .zip/.7z holding one")
    ap.add_argument("--disc-us", type=Path, help="your Final Fight CD (USA) disc: its .cue, a .chd, or a .zip/.7z holding one")
    ap.add_argument("--romset", action="append", default=[], help="an arcade ZIP, 7z or ROM folder (repeatable)")
    ap.add_argument("--rompath", action="append", default=[], help="a folder to search for romsets (repeatable)")
    ap.add_argument("--platform", choices=(*PLATFORMS, "all"), default="all")
    ap.add_argument("--include-devices", action="store_true", help="put the QSound firmware into the sets")
    ap.add_argument("--check", action="store_true", help="verify the arcade inputs only")
    ap.add_argument("--out-dir", help="build into this folder")
    ap.add_argument("--debug", action="store_true", help="show a traceback for troubleshooting")
    ap.add_argument('--tools-cache', type=Path, help='verified helper cache')
    ap.add_argument('--tools-bundle', type=Path, help='offline tools ZIP or directory')
    ap.add_argument('--offline', action='store_true', help='never download helpers')
    args = ap.parse_args()
    if args.tools_cache or args.tools_bundle or args.offline:
        sys.path.insert(0, str(HERE/'cpsplus/tools'))
        kit_module('ffmpeg_runtime').configure(sys.argv[1:])
    if args.disc_us:
        args.disc_us = require_path(args.disc_us, "--disc-us", file=True)
    if args.disc_jp:
        args.disc_jp = require_path(args.disc_jp, "--disc-jp", file=True)
    if not args.check and not (args.disc_us or args.disc_jp):
        fail("give --disc-us and/or --disc-jp")
    if not args.check and not args.out_dir:
        args.out_dir = str(default_output("output-" + HERE.name))
    if not args.check:
        check_output(args.out_dir, [p for p in [args.disc_us, args.disc_jp] if p] + args.romset + args.rompath)

    if not args.check and not args.out_dir:
        fail("give --out-dir")
    catalog = json.loads((HERE / "rom_inputs.json").read_text())["sets"]
    print("Reading your arcade romsets...")
    paths = []
    for r in args.romset:
        paths += search_paths(r, args.rompath, HERE)
    resolver = Resolver(dict.fromkeys(paths or search_paths(None, args.rompath, HERE)),
                        hints=[*KIT["romsets"], "qsound", "qsound_hle"],
                        exclude=[Path(args.out_dir or "out"), HERE / "work"], progress=print)
    romsets = {name: resolver.resolve(list(catalog[name].values())) for name in KIT["romsets"]}
    firmware = resolver.devices(include=args.include_devices)
    if QSOUND["name"] in firmware:
        romsets["qsound"] = firmware
    if args.check:
        print(f"{KIT['title']} arcade inputs verified")
        return
    discs = {"jp": args.disc_jp, "us": args.disc_us}
    for region, disc in discs.items():
        if disc is not None and not disc.is_file():
            fail(f"--disc-{region}: {disc.absolute()} is not a file")
    editions = [e for e, meta in KIT["editions"].items() if discs[meta["disc"]]]
    if not editions:
        fail("give --disc-us (USA build) and/or --disc-jp (Japan build and English restoration)")
    platforms = PLATFORMS if args.platform == "all" else (args.platform,)
    ffex_kit = kit_module("ffex_kit")
    packs = kit_module("packs") if "mister" in platforms else None
    if "darksoft" in platforms:
        import darksoft
        dead_key = darksoft.dead_key_profile(KIT["darksoft"])
    if "mister" in platforms and not (HERE / "core" / KIT["core"]).is_file():
        fail(f"this kit is incomplete: core/{KIT['core']} is missing")
    # Each build's MRA and HBMAME definitions name its members' CRCs; the release stages them with it.
    for edition in editions:
        meta = KIT["editions"][edition]
        for staged in ((f"mras/{meta['mra']}.mra",) if "mister" in platforms else ()) + \
                      ((f"definitions/{meta['set']}.cpp.inc",) if "hbmame" in platforms else ()):
            if not (HERE / staged).is_file():
                fail(f"this kit is incomplete: {staged} is missing")
    out = Path(args.out_dir)
    out.resolve().parent.mkdir(parents=True, exist_ok=True)
    files = {}
    with tempfile.TemporaryDirectory(prefix=".ffex2-", dir=out.resolve().parent) as tmp:
        work = Path(tmp)
        for edition in editions:
            meta = KIT["editions"][edition]
            print(f"Building {meta['mra']} (this takes several minutes)...")
            try:
                members = ffex_kit.build(edition, romsets, discs, work / edition)
            except ValueError as exc:
                fail(f"{meta['mra']}: {exc}")
            zip_name = f"{meta['set']}.zip"
            if "hbmame" in platforms:
                files[f"hbmame/{zip_name}"] = members
                files[f"hbmame/definitions/{meta['set']}.cpp.inc"] = \
                    (HERE / "definitions" / f"{meta['set']}.cpp.inc").read_bytes()
            if "mister" in platforms:
                files[f"mister/games/hbmame/{zip_name}"] = members
                folder = "/".join(["mister", *KIT["mra_root"], KIT["mra_dir"][edition]]).rstrip("/")
                files[f"{folder}/{meta['mra']}.mra"] = mister_mra(meta, members).encode()
                print(f"Building the {meta['mra']} music pack...")
                cpk = work / "packs" / f"{meta['pack']}.cpk"
                try:
                    packs.build(discs["jp"], discs["us"], edition, cpk, work=work / "pack-work",
                                keyed=HERE / "keyed")
                except ValueError as exc:
                    fail(f"{meta['pack']}: {exc}")
                files[f"mister/games/CPS+/{meta['pack']}.zip"] = {cpk.name: cpk.read_bytes()}
                cpk.unlink()
            if "darksoft" in platforms:
                # The set is a Phoenix set (plaintext program, dead key) in the sfa3ud layout.
                layout = {meta.get("layout_names", {}).get(n, n): data for n, data in members.items()}
                files.update(darksoft.tree(layout, dead_key["profile"], folder=f"{KIT['darksoft']}_{edition}",
                                           menu=dead_key["menus"][edition], dead_key_kit=KIT["darksoft"]))
        if "mister" in platforms:
            files[f"mister/_Arcade/cores/{KIT['core']}"] = (HERE / "core" / KIT["core"]).read_bytes()
        publish_tree(out, files)
    print_completion(out, files)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        if "--debug" in sys.argv:
            raise
        fail(str(exc))
