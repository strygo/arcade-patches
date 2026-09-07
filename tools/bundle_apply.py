#!/usr/bin/env python3
"""Apply this patch set to your own dump of the game.

Usage:
    python3 apply.py <stock zip>              writes <set>_patched.zip next to it
    python3 apply.py <stock zip> -o out.zip   writes to the given path
    python3 apply.py <stock zip> --hbmame     writes the HBMAME clone set zip
    python3 apply.py <rom directory>          patches extracted files in place-adjacent copies

Bundles that carry several builds of one game (regional editions) take
--variant <key> to choose one; --list-variants shows them.

The stock zip is your own MAME-format romset (see manifest.json for the exact
set this patch targets). Every file is checksum-verified before and after
patching; nothing is written unless the source matches the expected original.

Requires only Python 3.8+. No third-party modules.
"""

import argparse
import json
import os
import sys
import zipfile
import zlib


def apply_ips(patch: bytes, src: bytes) -> bytes:
    if patch[:5] != b"PATCH":
        raise ValueError("not an IPS patch (bad magic)")
    buf = bytearray(src)
    pos = 5
    while True:
        if pos + 3 > len(patch):
            raise ValueError("truncated IPS patch")
        if patch[pos : pos + 3] == b"EOF":
            break
        offset = int.from_bytes(patch[pos : pos + 3], "big")
        size = int.from_bytes(patch[pos + 3 : pos + 5], "big")
        pos += 5
        if size == 0:  # RLE record
            rle_size = int.from_bytes(patch[pos : pos + 2], "big")
            data = patch[pos + 2 : pos + 3] * rle_size
            pos += 3
        else:
            data = patch[pos : pos + size]
            pos += size
        end = offset + len(data)
        if end > len(buf):
            buf.extend(b"\x00" * (end - len(buf)))
        buf[offset:end] = data
    return bytes(buf)


def crc32(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def load_bundle(variant=None):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    ips_dir = os.path.join(here, "ips")
    variants = manifest.get("variants")
    if variants:
        keys = ", ".join(v["key"] for v in variants)
        if not variant:
            fail(f"this bundle holds {len(variants)} builds; choose one with "
                 f"--variant <key> (one of: {keys}), or --list-variants")
        chosen = next((v for v in variants if v["key"] == variant), None)
        if chosen is None:
            fail(f"no build named '{variant}' (one of: {keys})")
        manifest = dict(manifest)
        manifest["members"] = chosen["members"]
        manifest["hbmame"] = chosen.get("hbmame")
        manifest["variant"] = {"key": chosen["key"], "label": chosen["label"]}
        ips_dir = os.path.join(ips_dir, chosen["key"])
    elif variant:
        fail("this bundle has a single build; drop --variant")
    patches = {}
    for member in manifest["members"]:
        if member["action"] != "patch":
            continue
        ips_path = os.path.join(ips_dir, member["name"] + ".ips")
        with open(ips_path, "rb") as f:
            patches[member["name"]] = f.read()
    return manifest, patches


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def patch_zip(manifest, patches, in_path, out_path):
    expected = {m["name"]: m for m in manifest["members"]}
    with zipfile.ZipFile(in_path) as zin:
        names = set(zin.namelist())
        missing_patched = sorted(
            n for n, m in expected.items() if m["action"] == "patch" and n not in names
        )
        if missing_patched:
            fail(
                f"{in_path} does not look like a {manifest['set']} set; "
                f"missing: {', '.join(missing_patched)}"
            )
        missing_other = sorted(set(expected) - names)
        if missing_other:
            print(f"warning: set is missing unpatched files: {', '.join(missing_other)}")
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in sorted(names):
                data = zin.read(name)
                m = expected.get(name)
                if m is None:
                    print(f"  {name}: not in manifest, copied unchanged")
                    zout.writestr(name, data)
                    continue
                if m["action"] == "patch":
                    if crc32(data) != m["stock_crc32"]:
                        fail(
                            f"{name}: CRC32 {crc32(data)} does not match the expected "
                            f"original {m['stock_crc32']}. Wrong or already-patched set; "
                            f"this patch targets MAME set '{manifest['set']}' "
                            f"({manifest['game']})."
                        )
                    data = apply_ips(patches[name], data)
                    if crc32(data) != m["patched_crc32"]:
                        fail(f"{name}: patched output checksum mismatch (bad patch file?)")
                    print(f"  {name}: patched, CRC32 {m['stock_crc32']} -> {m['patched_crc32']}")
                elif crc32(data) != m["stock_crc32"]:
                    print(f"  {name}: warning: CRC32 differs from the reference set, copied unchanged")
                zout.writestr(name, data)
    print(f"\nWrote {out_path}")
    print("Note: MAME will report checksum warnings for the patched program ROMs.")
    print("That is expected; the game runs normally.")


def patch_zip_hbmame(manifest, patches, in_path, out_path):
    hb = manifest.get("hbmame")
    if not hb:
        fail("this bundle has no HBMAME set mapping (--hbmame not supported here)")
    expected = {m["name"]: m for m in manifest["members"] if m["action"] == "patch"}
    with zipfile.ZipFile(in_path) as zin:
        names = set(zin.namelist())
        missing = sorted(set(expected) - names)
        if missing:
            fail(
                f"{in_path} does not look like a {manifest['set']} set; "
                f"missing: {', '.join(missing)}"
            )
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in sorted(expected):
                m = expected[name]
                data = zin.read(name)
                if crc32(data) != m["stock_crc32"]:
                    fail(
                        f"{name}: CRC32 {crc32(data)} does not match the expected "
                        f"original {m['stock_crc32']}. Wrong or already-patched set; "
                        f"this patch targets MAME set '{manifest['set']}' "
                        f"({manifest['game']})."
                    )
                data = apply_ips(patches[name], data)
                if crc32(data) != m["patched_crc32"]:
                    fail(f"{name}: patched output checksum mismatch (bad patch file?)")
                new_name = hb["renames"][name]
                print(f"  {name} -> {new_name}: patched, CRC32 {m['patched_crc32']}")
                zout.writestr(new_name, data)
    print(f"\nWrote {out_path}")
    print(f"Put it in HBMAME's roms/ folder next to your stock {manifest['set']}.zip;")
    print(f"the game appears as '{hb['setname']}'. The set loads with no checksum")
    print("warnings: HBMAME's set definition carries the patched checksums.")


def patch_dir(manifest, patches, in_dir):
    ok = True
    for m in manifest["members"]:
        if m["action"] != "patch":
            continue
        path = os.path.join(in_dir, m["name"])
        if not os.path.exists(path):
            print(f"  {m['name']}: not found, skipped")
            ok = False
            continue
        with open(path, "rb") as f:
            data = f.read()
        if crc32(data) == m["patched_crc32"]:
            print(f"  {m['name']}: already patched, skipped")
            continue
        if crc32(data) != m["stock_crc32"]:
            fail(f"{m['name']}: CRC32 {crc32(data)} does not match expected {m['stock_crc32']}")
        data = apply_ips(patches[m["name"]], data)
        if crc32(data) != m["patched_crc32"]:
            fail(f"{m['name']}: patched output checksum mismatch (bad patch file?)")
        with open(path, "wb") as f:
            f.write(data)
        print(f"  {m['name']}: patched in place")
    if not ok:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="stock romset zip, or a directory of extracted ROM files")
    ap.add_argument("-o", "--output", help="output zip path (zip input only)")
    ap.add_argument(
        "--hbmame",
        action="store_true",
        help="write the HBMAME clone set zip (patched ROMs only, HBMAME names)",
    )
    ap.add_argument("--variant", help="which build to apply, for bundles that hold several")
    ap.add_argument("--list-variants", action="store_true",
                    help="show the builds in this bundle and exit")
    args = ap.parse_args()

    if args.list_variants:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "manifest.json"), "r", encoding="utf-8") as f:
            m = json.load(f)
        for v in m.get("variants", []):
            print(f"  {v['key']:12} {v['label']}")
        if not m.get("variants"):
            print("  (single build; no --variant needed)")
        return

    if not args.target:
        ap.error("the following arguments are required: target")
    manifest, patches = load_bundle(args.variant)
    print(f"{manifest['title']} ({manifest['version']})")
    print(f"Target: MAME set '{manifest['set']}' - {manifest['game']}")
    if manifest.get("variant"):
        print(f"Build:  {manifest['variant']['label']} (--variant {manifest['variant']['key']})")
    print()

    if os.path.isdir(args.target):
        if args.hbmame:
            fail("--hbmame needs the stock zip as input, not a directory")
        patch_dir(manifest, patches, args.target)
    else:
        out = args.output
        if not out:
            if args.hbmame:
                setname = manifest.get("hbmame", {}).get("setname")
                if not setname:
                    fail("this bundle has no HBMAME set mapping (--hbmame not supported here)")
                out = os.path.join(os.path.dirname(os.path.abspath(args.target)), setname + ".zip")
            else:
                base, ext = os.path.splitext(args.target)
                tag = "_" + manifest["variant"]["key"] if manifest.get("variant") else ""
                out = base + tag + "_patched" + (ext or ".zip")
        if os.path.abspath(out) == os.path.abspath(args.target):
            fail("output path must differ from input path")
        if args.hbmame:
            patch_zip_hbmame(manifest, patches, args.target, out)
        else:
            patch_zip(manifest, patches, args.target, out)


if __name__ == "__main__":
    main()
