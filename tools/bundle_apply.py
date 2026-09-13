#!/usr/bin/env python3
"""Apply this patch set to your own dump of the game.

Usage:
    python3 apply.py <stock zip> --out-dir out   writes every platform's files (recommended):
                                                   out/mame/     MAME / original hardware set
                                                   out/hbmame/   HBMAME set
                                                   out/mister/   MiSTer MRA (uses your stock zip)
    python3 apply.py <stock zip>                 writes <set>_patched.zip next to it
    python3 apply.py <stock zip> -o out.zip      writes to the given path
    python3 apply.py <stock zip> --hbmame        writes the HBMAME clone set zip
    python3 apply.py <rom directory>             patches extracted files in place

Bundles that carry several builds of one game (regional editions) take
--variant <key> to choose one (--out-dir makes all of them unless you do);
--list-variants shows them.

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


HERE = os.path.dirname(os.path.abspath(__file__))


def read_manifest():
    with open(os.path.join(HERE, "manifest.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def load_bundle(variant=None):
    manifest = read_manifest()
    ips_dir = os.path.join(HERE, "ips")
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
        manifest["mra"] = chosen.get("mra")
        manifest["variant"] = {"key": chosen["key"], "label": chosen["label"]}
        ips_dir = os.path.join(ips_dir, chosen["key"])
    elif variant:
        fail("this bundle has a single build; drop --variant")
    patches = {}
    for member in manifest["members"]:
        if member["action"] == "copy":
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
        added = sorted(n for n, m in expected.items() if m["action"] == "add")
        if missing_patched:
            fail(
                f"{in_path} does not look like a {manifest['set']} set; "
                f"missing: {', '.join(missing_patched)}"
            )
        missing_other = sorted(set(expected) - names - set(added))
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
            for name in added:
                zout.writestr(name, build_added(expected[name], patches[name]))
    print(f"Wrote {out_path}")


def build_added(m, patch):
    """A ROM the stock set doesn't have: its patch creates it from nothing."""
    data = apply_ips(patch, b"")
    if len(data) != m["size"] or crc32(data) != m["patched_crc32"]:
        fail(f"{m['name']}: added file checksum mismatch (bad patch file?)")
    print(f"  {m['name']}: added, CRC32 {m['patched_crc32']}")
    return data


def patch_zip_hbmame(manifest, patches, in_path, out_path):
    hb = manifest.get("hbmame")
    if not hb:
        fail("this bundle has no HBMAME set mapping (--hbmame not supported here)")
    expected = {m["name"]: m for m in manifest["members"] if m["action"] != "copy"}
    with zipfile.ZipFile(in_path) as zin:
        names = set(zin.namelist())
        missing = sorted(n for n, m in expected.items() if m["action"] == "patch" and n not in names)
        if missing:
            fail(
                f"{in_path} does not look like a {manifest['set']} set; "
                f"missing: {', '.join(missing)}"
            )
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in sorted(expected):
                m = expected[name]
                if m["action"] == "add":
                    zout.writestr(hb["renames"][name], build_added(m, patches[name]))
                    continue
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
    print(f"Wrote {out_path}")


def patch_dir(manifest, patches, in_dir):
    ok = True
    for m in manifest["members"]:
        if m["action"] == "copy":
            continue
        path = os.path.join(in_dir, m["name"])
        if m["action"] == "add":
            with open(path, "wb") as f:
                f.write(build_added(m, patches[m["name"]]))
            continue
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


def write_out_dir(target, out_dir, only_variant):
    """Every platform's files for each build, in one run."""
    base = read_manifest()
    keys = [v["key"] for v in base.get("variants", [])]
    if only_variant or not keys:
        keys = [only_variant]
    for key in keys:
        manifest, patches = load_bundle(key)
        if manifest.get("variant"):
            print(f"== {manifest['variant']['label']} (--variant {key})")
        if base.get("mame_build", True):
            sub = os.path.join("mame", key) if key else "mame"
            mame = os.path.join(out_dir, sub, manifest["set"] + ".zip")
            os.makedirs(os.path.dirname(mame), exist_ok=True)
            print(f"MAME ({os.path.relpath(mame, out_dir)}):")
            patch_zip(manifest, patches, target, mame)
        if manifest.get("hbmame"):
            hb = os.path.join(out_dir, "hbmame", manifest["hbmame"]["setname"] + ".zip")
            os.makedirs(os.path.dirname(hb), exist_ok=True)
            print(f"HBMAME ({os.path.relpath(hb, out_dir)}):")
            patch_zip_hbmame(manifest, patches, target, hb)
        if manifest.get("mra"):
            src = os.path.join(HERE, *manifest["mra"].split("/"))
            dst = os.path.join(out_dir, *manifest["mra"].split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, "rb") as fin, open(dst, "wb") as fout:
                fout.write(fin.read())
            print(f"MiSTer: wrote {os.path.relpath(dst, out_dir)}")
        print()
    print(f"Done. Everything is in {out_dir}/ (see readme.txt for where each file goes).")
    if base.get("mame_build") is False:
        print("There is no MAME build: stock MAME can't load the added program ROM.")
        print("HBMAME and MiSTer load it with no checksum warnings. MiSTer uses your")
        print("stock set in games/mame/.")
    else:
        print("MAME reports checksum warnings for the patched ROMs; that's expected.")
        print("HBMAME and MiSTer load without them. MiSTer uses your stock set in games/mame/.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="stock romset zip, or a directory of extracted ROM files")
    ap.add_argument("--out-dir", help="write the MAME, HBMAME and MiSTer files for every build here")
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
    if args.out_dir:
        if os.path.isdir(args.target) or args.output or args.hbmame:
            ap.error("--out-dir takes the stock zip and no -o/--hbmame")
        m = read_manifest()
        print(f"{m['title']} ({m['version']})")
        print(f"Target: MAME set '{m['set']}' - {m['game']}")
        print()
        write_out_dir(args.target, args.out_dir, args.variant)
        return
    manifest, patches = load_bundle(args.variant)
    if manifest.get("mame_build") is False and not args.hbmame:
        fail("this edition has no MAME build (stock MAME can't load its added ROM); "
             "use --out-dir, or --hbmame for the HBMAME set")
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
            print(f"Put it in HBMAME's roms/ folder next to your stock {manifest['set']}.zip;")
            print(f"the game appears as '{manifest['hbmame']['setname']}', with no checksum warnings.")
        else:
            patch_zip(manifest, patches, args.target, out)
            print("MAME reports checksum warnings for the patched ROMs; that's expected.")


if __name__ == "__main__":
    main()
