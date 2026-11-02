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

if sys.argv[1:] == ["--describe-json"]:
    from kit_identity import describe
    if (Path(__file__).resolve().parent / "collection.json").is_file():
        from gold_collection import describe
    print(json.dumps(describe(Path(__file__).resolve().parent), sort_keys=True))
    raise SystemExit(0)

import assemble
import fbneo_names
import extract as ex
from rom_sources import Resolver, RomError, QSOUND, search_paths, verify, publish_tree, write_zip

from rom_sources import require_path, default_output, check_output, print_completion

HERE = Path(__file__).resolve().parent
PROFILES = {"jp": ex.JP, "us": ex.US, "eu": ex.EU, "asia": ex.ASIA}

# Both editions explicitly declare their identity and platform names.
from kit_identity import load_config, PLATFORMS
_KIT = load_config(HERE)
TITLE, SETS, MRA_DIR = _KIT["title"], _KIT["sets"], _KIT["mra_dir"]
MRA_ROOT, DESCRIPTION = tuple(_KIT["mra_root"]), _KIT["description"]


def fail(msg):
    raise RomError(msg)


def zread(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist()}


def checked_members(members, want):
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
    return members


def write_set(members, want, out_path):
    write_zip(out_path, checked_members(members, want))


def main():
    ap = argparse.ArgumentParser(description=DESCRIPTION,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iso", help="your PS2 anthology disc image")
    ap.add_argument("--romset", help="your arcade ZIP, 7z, or ROM folder")
    ap.add_argument("--rompath", action="append", default=[])
    ap.add_argument("--include-devices", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--check-runtime", action="store_true")
    ap.add_argument("--platform", choices=("all", *PLATFORMS), default="all")
    ap.add_argument("--region", "--variant", dest="region", required=True, choices=tuple(SETS))
    ap.add_argument("--out-dir", help="build every platform into this folder")
    ap.add_argument("--size", choices=("4mb", "8mb"), help="single-file mode: capacity")
    ap.add_argument("--out", help="single-file mode: output zip path")
    ap.add_argument("--debug", action="store_true", help="show a traceback for troubleshooting")
    from tool_controls import add_arguments, configure
    add_arguments(ap)
    ap.add_argument("--inputs-json", help="exact discovered ROM bindings; disables folder discovery")
    ap.add_argument("--check-disc", action="store_true", help="verify arcade ROMs and both consumed disc resources without building")
    ap.add_argument("--collection-layout", action="store_true")
    ap.add_argument("--report-json", type=Path)
    args = ap.parse_args()
    if args.collection_layout:
        from gold_collection import run
        run(HERE, args, build_outputs)
        return
    configure(sys.argv[1:])
    if args.check_disc and not args.iso:
        ap.error("--check-disc requires --iso")
    if args.iso:
        args.iso = require_path(args.iso, "--iso", file=True)
    if not (args.check or args.check_runtime or args.check_disc):
        if not args.iso:
            fail("--iso is required to reconstruct " + TITLE)
        if bool(args.size) != bool(args.out):
            fail("single-file mode needs both --size and --out")
        if args.out_dir and args.out:
            fail("choose --out-dir or --out, not both")
        if not args.out_dir and not args.out:
            args.out_dir = str(default_output("output-sfa2"))
    if not (args.check or args.check_runtime or args.check_disc):
        check_output(args.out_dir or args.out, [p for p in [args.iso, args.romset, *args.rompath] if p], directory=bool(args.out_dir))

    if args.platform in ('darksoft', 'fbneo') and (args.out or args.size == '4mb'):
        fail(f'{args.platform} uses the best 8 MiB native-sample version and requires --out-dir')
    if not (args.check or args.check_runtime or args.check_disc) and not args.out_dir and not (args.size and args.out):
        fail("give --out-dir, or both --size and --out")

    data = json.loads((HERE / "recipes" / f"{args.region}.json").read_text())
    profile = PROFILES[args.region]
    print("Reading your arcade romset...")
    catalog = json.loads((HERE / "rom_inputs.json").read_text())["sets"]
    source_set = SETS[args.region]['4mb']
    resolver = Resolver(search_paths(args.romset, args.rompath, HERE),
                        hints=[source_set, "sfz2al", "qsound", "qsound_hle"],
                        exclude=[Path(args.out_dir or args.out or "out"), HERE / "work"], progress=print, bindings=args.inputs_json)
    arc = resolver.resolve([s for s in catalog[source_set].values() if s["role"] == "game"])
    arc.update(resolver.devices(include=args.include_devices))
    if args.check_runtime:
        resolver.devices(include=True)
    if (args.check or args.check_runtime) and not args.check_disc:
        print(f"{TITLE} stock ROM inputs verified")
        return
    if not args.iso:
        fail(f"--iso is required to reconstruct {TITLE}")
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
    if args.check_disc:
        print(f"{TITLE} arcade ROMs and disc resources verified")
        return
    inp = assemble.prepare_inputs(arc, {
        "entry531": z6["entry531"], "comp2": z6["comp2"], "audio": audio,
    })

    if args.out:
        data = json.loads((HERE / "recipes" / f"{args.region}.json").read_text())
        entry = data['sizes'][args.size]
        members = assemble.reconstruct(inp, {k: base64.b64decode(v) for k, v in entry['recipes'].items()})
        write_set(members, entry['md5'], Path(args.out))
        print(f"Wrote {args.out} — all members checksum-verified.")
        return
    files = export_outputs(args.region, inp, args.platform)
    publish_tree(Path(args.out_dir), files)
    print_completion(args.out_dir, files)


def build_outputs(region, arc, iso, platform='all'):
    """The same reconstruction and exports serve standalone and collection runs."""
    z6 = ex.extract_zero6(str(iso), PROFILES[region])
    audio = ex.extract_audio(str(iso), PROFILES[region])
    inp = assemble.prepare_inputs(arc, {'entry531': z6['entry531'], 'comp2': z6['comp2'], 'audio': audio})
    return export_outputs(region, inp, platform)


def export_outputs(region, inp, platform='all'):
    data = json.loads((HERE / 'recipes' / (region + '.json')).read_text())
    names, reconstructed, outputs = SETS[region], {}, {}
    def reconstruct(size):
        if size not in reconstructed:
            print('Reconstructing ' + size + '...')
            entry = data['sizes'][size]
            members = assemble.reconstruct(inp, {k: base64.b64decode(v) for k, v in entry['recipes'].items()})
            reconstructed[size] = checked_members(members, entry['md5'])
        return reconstructed[size]
    if platform in ('all', 'mame'):
        outputs[f"mame/{region}/{names['4mb']}.zip"] = reconstruct('4mb')
    if platform in ('all', 'hbmame', 'mister', 'fbneo'):
        members = reconstruct('8mb')
        if platform in ('all', 'hbmame'):
            outputs[f"hbmame/{names['8mb']}.zip"] = members
        if platform in ('all', 'mister'):
            outputs[f"mister/games/hbmame/{names['8mb']}.zip"] = members
            mra = HERE / 'mras' / (names['mra'] + '.mra')
            if not mra.is_file():
                fail('Missing regional MRA: ' + mra.name)
            text = re.sub(r'zip="(/hbmame/[^"|]+\.zip)"',
                          r'zip="\1|qsound.zip|qsound_hle.zip"', mra.read_text())
            folder = '/'.join(('mister', *MRA_ROOT, *([MRA_DIR[region]] if MRA_DIR[region] else [])))
            outputs[folder + '/' + mra.name] = text.encode()
        if platform in ('all', 'fbneo'):
            outputs.update(fbneo_names.tree(names['8mb'], members))
    if platform in ('all', 'darksoft'):
        from darksoft import tree, native_menu
        outputs.update(tree(reconstruct('8mb'), names['4mb'] + '_qsound8',
                            folder=_KIT['edition'] + '_' + region, menu=native_menu(_KIT['edition'], region)))
    return outputs


if __name__ == "__main__":
    try:
        if '--emit-definition' in sys.argv:
            from gold_collection import package_main
            package_main(HERE, sys.argv[1:], build_outputs)
        else:
            main()
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        if "--debug" in sys.argv:
            raise
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(1)
