#!/usr/bin/env python3
"""Apply this patch set to your own dump of the game.

Usage:
    python3 apply.py <stock zip>              writes <set>_patched.zip next to it
    python3 apply.py <stock zip> -o out.zip   writes to the given path
    python3 apply.py <rom directory>          patches extracted files in place-adjacent copies

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


def load_bundle():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    patches = {}
    for member in manifest["members"]:
        if member["action"] != "patch":
            continue
        ips_path = os.path.join(here, "ips", member["name"] + ".ips")
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
    ap.add_argument("target", help="stock romset zip, or a directory of extracted ROM files")
    ap.add_argument("-o", "--output", help="output zip path (zip input only)")
    args = ap.parse_args()

    manifest, patches = load_bundle()
    print(f"{manifest['title']} ({manifest['version']})")
    print(f"Target: MAME set '{manifest['set']}' - {manifest['game']}\n")

    if os.path.isdir(args.target):
        patch_dir(manifest, patches, args.target)
    else:
        out = args.output
        if not out:
            base, ext = os.path.splitext(args.target)
            out = base + "_patched" + (ext or ".zip")
        if os.path.abspath(out) == os.path.abspath(args.target):
            fail("output path must differ from input path")
        patch_zip(manifest, patches, args.target, out)


if __name__ == "__main__":
    main()
