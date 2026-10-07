#!/usr/bin/env python3
"""Build an Arrange edition from your own copies.

This tool ships no game data.  It rebuilds the CPS-2 sets from files you
already own:
  * your arcade romsets: Vampire Hunter 2 (vhunt2), Vampire Savior 2 (vsav2)
    and Vampire Savior (vsavj), all Japanese, and
  * your PS2 Vampire: Darkstalkers Collection (Japan) disc image.

Every member is checksum-verified against the known build.  See README.txt.
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import arrange
import collection
from rom_sources import Resolver, RomError, QSOUND, search_paths, publish_tree, write_zip

HERE = Path(__file__).resolve().parent
KIT = json.loads((HERE / "kit.json").read_text())


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=KIT["description"],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iso", help="your Vampire: Darkstalkers Collection (Japan) disc image")
    ap.add_argument("--romset", action="append", default=[], help="an arcade ZIP, 7z or ROM folder (repeatable)")
    ap.add_argument("--rompath", action="append", default=[], help="a folder to search for romsets (repeatable)")
    ap.add_argument("--region", choices=(*KIT["regions"], "all"), default="all")
    ap.add_argument("--include-devices", action="store_true")
    ap.add_argument("--check", action="store_true", help="verify the arcade inputs only")
    ap.add_argument("--out-dir", help="build into this folder")
    args = ap.parse_args()
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
    roms = {}
    for name, members in KIT["romsets"].items():
        roms[name] = resolver.resolve([catalog[name][m] for m in members])
    firmware = resolver.devices(include=args.include_devices)
    if args.check:
        print(f"{KIT['title']} arcade inputs verified")
        return
    if not args.iso:
        fail(f"--iso is required to build {KIT['title']}")
    try:
        disc = collection.Disc(args.iso)
    except (OSError, ValueError) as exc:
        fail(f"{exc}\n       looked for: {Path(args.iso).absolute()}")
    base = KIT["base"]
    if QSOUND["name"] in firmware:
        roms[base][QSOUND["name"]] = firmware[QSOUND["name"]]
    regions = list(KIT["regions"]) if args.region == "all" else [args.region]
    stage_context = tempfile.TemporaryDirectory(prefix=".arrange-", dir=Path(args.out_dir).resolve().parent)
    stage = Path(stage_context.name)
    for region in regions:
        meta = KIT["regions"][region]
        recipes = json.loads((HERE / "recipes" / f"{region}.json").read_text())
        if QSOUND["name"] not in roms[base]:
            recipes["units"] = [u for u in recipes["units"] if u["members"] != [QSOUND["name"]]]
            recipes["md5"].pop(QSOUND["name"], None)
        print(f"Building {meta['mra']} (this takes a few minutes)...")
        try:
            members = arrange.reconstruct(arrange.Inputs(roms, disc, recipes["spec"]), recipes)
        except ValueError as exc:
            fail(str(exc))
        zip_name = f"{meta['set']}.zip"
        write_zip(stage / "hbmame" / zip_name, members)
        write_zip(stage / "mister" / "games" / "hbmame" / zip_name, members)
        mra = (HERE / "mras" / f"{meta['mra']}.mra").read_text()
        mra = re.sub(r'zip="(/hbmame/[^"|]+\.zip)"', r'zip="\1|qsound.zip|qsound_hle.zip"', mra)
        folder = stage.joinpath("mister", *KIT["mra_root"], KIT["mra_dir"][region])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{meta['mra']}.mra").write_text(mra)
    cores = stage / "mister" / "_Arcade" / "cores"
    cores.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "core" / KIT["core"], cores / KIT["core"])
    files = {p.relative_to(stage).as_posix(): p.read_bytes() for p in stage.rglob("*") if p.is_file()}
    publish_tree(Path(args.out_dir), files)
    stage_context.cleanup()
    print(f"\nBuilt {', '.join(regions)} into {args.out_dir}/ — every member checksum-verified:")
    for region in regions:
        meta = KIT["regions"][region]
        print(f"  hbmame/{meta['set']}.zip")
    print(f"  mister/ -> copy to the SD card root (MRAs, games/hbmame/, _Arcade/cores/{KIT['core']})")


if __name__ == "__main__":
    try:
        main()
    except RomError as exc:
        fail(str(exc))
