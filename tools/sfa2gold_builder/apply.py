#!/usr/bin/env python3
"""Rebuild Street Fighter Alpha 2 Gold / Zero 2 Dash from your own copies.

This tool ships NO game data. It reconstructs the CPS-2 build entirely from
files you already own:
  * your PlayStation 2 Alpha Anthology / Fighter's Generation disc image, and
  * your arcade Street Fighter Zero 2 Alpha romset (sfz2al or sfz2alj).

Every graphic, sample, and program byte is copied or mechanically transformed
from those sources; only a small amount of our own new code is included as
data. Output is checksum-verified against the known-good build.

Usage (recommended — build everything for one region):
    python3 apply.py --iso <anthology.iso> --romset <sfz2al.zip> \
                     --region us --out-dir out

  Produces, under out/ — point every region at the SAME out/ and nothing
  collides (the 8MB sets and MRAs have distinct names; the 4MB sets go in a
  per-region subfolder because US/EU/Asia all share the arcade name sfz2al):
    mame/<region>/<set>.zip   4MB set for stock MAME and real CPS-2 hardware
    hbmame/<set>.zip          8MB set for HBMAME
    mister/                   MiSTer (Jotego jtcps2), laid out like the SD card
                              so you can copy its contents to the card root:
                                _Arcade/_Arcade Patches/_Restorations/
                                  <name>.mra               USA
                                  _<Region>/<name>.mra     Europe, Asia, Japan
                                games/hbmame/<set>.zip

Usage (single file):
    python3 apply.py --iso ... --romset ... --region us --size 8mb --out sfa2g.zip

Requires only Python 3.10+ — no emulator, no assembler.
"""
import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

import assemble
import extract as ex
from rom_sources import Resolver, RomError, QSOUND, search_paths, verify, publish_tree, write_zip

HERE = Path(__file__).resolve().parent
PROFILES = {"jp": ex.JP, "us": ex.US, "eu": ex.EU, "asia": ex.ASIA}

# region -> set names (4MB for MAME/hardware, 8MB for HBMAME/MiSTer) and the
# friendly MiSTer MRA filename (title and region; no date).
SETS = {
    "us": {"4mb": "sfz2al", "8mb": "sfa2g",
           "mra": "Street Fighter Alpha 2 Gold (USA)"},
    "eu": {"4mb": "sfz2al", "8mb": "sfa2d",
           "mra": "Street Fighter Alpha 2 Dash (Europe)"},
    "jp": {"4mb": "sfz2alj", "8mb": "sfz2d",
           "mra": "Street Fighter Zero 2 Dash (Japan)"},
    "asia": {"4mb": "sfz2al", "8mb": "sfz2da",
             "mra": "Street Fighter Zero 2 Dash (Asia)"},
}
# MiSTer folder: every project of ours lives under _Arcade/_Arcade Patches,
# sorted the way the website is.  USA sits at the top of its section and every
# other region in its own subfolder.
MRA_ROOT = ("_Arcade", "_Arcade Patches", "_Restorations")
MRA_DIR = {"us": "", "eu": "_Europe", "asia": "_Asia", "jp": "_Japan"}


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def zread(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist()}


def write_set(members, want, out_path):
    """Verify every member against the known-good build, then write the zip.

    The reconstruction starts from your romset, so a zip that carries more
    than this build uses -- a merged set, a scanned .dat, a leftover file --
    would otherwise stop the build on a file the set does not even contain.
    Those are dropped; only a member this build needs and cannot find is an
    error, and it says which.
    """
    want = {n: h for n, h in want.items() if n != "dl-1425.bin" or n in members}
    if "dl-1425.bin" in members and not verify(members["dl-1425.bin"], QSOUND):
        fail("QSound firmware identity mismatch")
    missing = sorted(set(want) - set(members))
    if missing:
        fail("your romset is missing " + ", ".join(missing)
             + " — is this the right set for this region?")
    extra = sorted(set(members) - set(want))
    if extra:
        shown = ", ".join(extra[:4]) + (" …" if len(extra) > 4 else "")
        print(f"  (ignoring {len(extra)} file(s) in your romset this build "
              f"does not use: {shown})")
    members = {m: members[m] for m in want}
    for m in want:
        if hashlib.md5(members[m]).hexdigest() != want[m]:
            fail(f"{m}: checksum mismatch — wrong disc or romset for this region")
    write_zip(out_path, members)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iso", help="your PS2 anthology disc image")
    ap.add_argument("--romset", help="your arcade ZIP, 7z, or ROM folder")
    ap.add_argument("--rompath", action="append", default=[])
    ap.add_argument("--include-devices", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--check-runtime", action="store_true")
    ap.add_argument("--platform", choices=("all", "mame", "hbmame", "mister"), default="all")
    ap.add_argument("--region", required=True, choices=("jp", "us", "eu", "asia"))
    ap.add_argument("--out-dir", help="build every platform into this folder")
    ap.add_argument("--size", choices=("4mb", "8mb"), help="single-file mode: capacity")
    ap.add_argument("--out", help="single-file mode: output zip path")
    args = ap.parse_args()
    if not (args.check or args.check_runtime) and not args.out_dir and not (args.size and args.out):
        fail("give --out-dir, or both --size and --out")

    data = json.loads((HERE / "recipes" / f"{args.region}.json").read_text())
    profile = PROFILES[args.region]
    print("Reading your arcade romset...")
    catalog = json.loads((HERE / "rom_inputs.json").read_text())["sets"]
    source_set = "sfz2alj" if args.region == "jp" else "sfz2al"
    resolver = Resolver(search_paths(args.romset, args.rompath, HERE),
                        hints=[source_set, "sfz2al", "qsound", "qsound_hle"],
                        exclude=[Path(args.out_dir or args.out or "out"), HERE / "work"], progress=print)
    arc = resolver.resolve([s for s in catalog[source_set].values() if s["role"] == "game"])
    arc.update(resolver.devices(include=args.include_devices))
    if args.check_runtime:
        resolver.devices(include=True)
    if args.check or args.check_runtime:
        print("Gold stock ROM inputs verified")
        return
    if not args.iso:
        fail("--iso is required to reconstruct Gold")
    if args.out and args.romset and Path(args.out).resolve() == Path(args.romset).resolve():
        fail("output must differ from source archive")
    gaps = assemble.missing_inputs(arc)
    if gaps:
        fail("your romset is missing " + ", ".join(gaps)
             + ". Add its parent archive with --rompath.")
    print("Extracting Cammy data from your disc (this takes a minute)...")
    try:
        z6 = ex.extract_zero6(args.iso, profile)
        audio = ex.extract_audio(args.iso, profile)
    except Exception as exc:  # noqa: BLE001
        fail(f"could not extract from the disc — is this the {args.region.upper()} "
             f"anthology ISO? ({exc})\n"
             f"       looked for: {Path(args.iso).absolute()}")
    inp = assemble.prepare_inputs(arc, {
        "entry531": z6["entry531"], "comp2": z6["comp2"], "audio": audio,
    })

    def reconstruct(size):
        entry = data["sizes"][size]
        recipes = {k: base64.b64decode(v) for k, v in entry["recipes"].items()}
        return assemble.reconstruct(inp, recipes), entry["md5"]

    if args.out:  # single-file mode
        print("Reconstructing and verifying...")
        members, want = reconstruct(args.size)
        write_set(members, want, Path(args.out))
        print(f"\nWrote {args.out} — all {len(members)} members checksum-verified.")
        return

    # Publish all selected outputs only after reconstruction/verification finishes.
    import tempfile
    Path(args.out_dir).parent.mkdir(parents=True, exist_ok=True)
    stage_context = tempfile.TemporaryDirectory(prefix=".gold-export-", dir=Path(args.out_dir).parent)
    out = Path(stage_context.name)
    names = SETS[args.region]
    print("Reconstructing 4MB (MAME / hardware)...")
    m4, w4 = reconstruct("4mb")
    # 4MB set in a per-region subfolder: US/EU/Asia share the arcade name
    # sfz2al, so this keeps all regions side by side in one out/.
    write_set(m4, w4, out / "mame" / args.region / f"{names['4mb']}.zip")
    print("Reconstructing 8MB (HBMAME / MiSTer)...")
    m8, w8 = reconstruct("8mb")
    write_set(m8, w8, out / "hbmame" / f"{names['8mb']}.zip")
    # MiSTer: mirror the SD-card layout so out/mister/ can be copied to the root.
    # games/hbmame, not games/mame: these are hbmame sets, the MRA asks for
    # /hbmame/<set>.zip, and the CPS+ MRAs for the same sets look there too --
    # so a user running both kits keeps one copy of the 8 MB zip, not two.
    write_set(m8, w8, out / "mister" / "games" / "hbmame" / f"{names['8mb']}.zip")
    mra = HERE / "mras" / f"{names['mra']}.mra"
    if mra.exists():
        arcade = out.joinpath("mister", *MRA_ROOT, MRA_DIR[args.region])
        arcade.mkdir(parents=True, exist_ok=True)
        text = mra.read_text()
        text = re.sub(r'zip="(/hbmame/[^"|]+\.zip)"',
                      r'zip="\1|qsound.zip|qsound_hle.zip"', text)
        (arcade / mra.name).write_text(text)
    files = {}
    for path in out.rglob("*"):
        if path.is_file():
            name = path.relative_to(out).as_posix()
            if args.platform == "all" or name.startswith(args.platform + "/"):
                files[name] = path.read_bytes()
    publish_tree(Path(args.out_dir), files)
    stage_context.cleanup()
    out = Path(args.out_dir)
    print(f"\nBuilt {args.region} into {out}/ — every game member checksum-verified:")
    print(f"  mame/{args.region}/{names['4mb']}.zip   (stock MAME, real hardware)")
    print(f"  hbmame/{names['8mb']}.zip     (HBMAME)")
    mra_path = "/".join(filter(None, ("/".join(MRA_ROOT), MRA_DIR[args.region], names["mra"])))
    print(f"  mister/ -> copy to SD root: {mra_path}.mra "
          f"+ games/hbmame/{names['8mb']}.zip   (MiSTer jtcps2)")
    print("Point other regions at the same out/ to collect all builds together.")
    print("\nMAME/HBMAME will warn about checksums (unofficial build); it runs normally.")


if __name__ == "__main__":
    try:
        main()
    except RomError as exc:
        fail(str(exc))
