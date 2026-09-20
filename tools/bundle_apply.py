#!/usr/bin/env python3
"""Apply a kit to merged, split, complete or extracted MAME ROM sources.

Use --rompath DIR --out-dir OUT for collection discovery. Existing positional
extracted-directory mode remains an explicit legacy in-place operation.
"""
import argparse
import json
import os
import sys
import shutil
import tempfile
from pathlib import Path
from rom_sources import Resolver, RomError, search_paths, write_zip, verify

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



def build_added(m, patch):
    """A ROM the stock set doesn't have: its patch creates it from nothing."""
    data = apply_ips(patch, b"")
    if len(data) != m["size"] or crc32(data) != m["patched_crc32"]:
        fail(f"{m['name']}: added file checksum mismatch (bad patch file?)")
    print(f"  {m['name']}: added, CRC32 {m['patched_crc32']}")
    return data



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


def requirements(manifest, compact=False):
    result = []
    for member in manifest["members"]:
        if member["action"] == "add" or member.get("role") == "device" or member["name"] == "dl-1425.bin":
            continue
        if compact and member["action"] == "copy":
            continue
        result.append({"name": member["name"], "size": member["size"],
                       "crc32": member["stock_crc32"], "sha256": member.get("stock_sha256")})
    return result


def build_members(manifest, patches, source, compact=False):
    result = {}
    for m in manifest["members"]:
        name = m["name"]
        if name == "dl-1425.bin" or m.get("role") == "device" or (compact and m["action"] == "copy"):
            continue
        if m["action"] == "add":
            data = build_added(m, patches[name])
        elif m["action"] == "patch":
            data = apply_ips(patches[name], source[name])
        else:
            data = source[name]
        expected = {"size": m.get("output_size", m["size"]),
                    "crc32": m.get("patched_crc32", m.get("stock_crc32")),
                    "sha256": m.get("output_sha256")}
        if not expected["sha256"] or not verify(data, expected):
            raise RomError(f"{name}: output identity mismatch or missing output SHA-256")
        result[manifest["hbmame"]["renames"][name] if compact else name] = data
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?")
    parser.add_argument("--rompath", action="append", default=[])
    parser.add_argument("--out-dir")
    parser.add_argument("-o", "--output")
    parser.add_argument("--hbmame", action="store_true")
    parser.add_argument("--platform", choices=("all", "mame", "hbmame", "mister"))
    parser.add_argument("--variant")
    parser.add_argument("--list-variants", action="store_true")
    parser.add_argument("--include-devices", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-runtime", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        assert apply_ips(b"PATCH\x00\x00\x00\x00\x01ZEOF", b"ABC") == b"ZBC"
        print("IPS applier self-test passed")
        return
    base = read_manifest()
    if args.list_variants:
        for variant in base.get("variants", []):
            print(f"{variant['key']}: {variant['label']}")
        return
    if args.out_dir and args.output:
        parser.error("choose --out-dir or --output")
    if args.hbmame and args.platform not in (None, "hbmame"):
        parser.error("--hbmame conflicts with --platform")
    platform = args.platform or ("hbmame" if args.hbmame else "all" if args.out_dir or args.check or args.check_runtime else "mame")
    if platform in ("all", "mister") and not args.out_dir and not (args.check or args.check_runtime):
        parser.error("this platform needs --out-dir")
    if args.target and os.path.isdir(args.target) and not args.out_dir and not args.rompath and not (args.check or args.check_runtime):
        if platform != "mame" or args.output:
            parser.error("use --rompath for directory discovery; positional directory mode patches in place")
        manifest, patches = load_bundle(args.variant)
        patch_dir(manifest, patches, args.target)
        return
    keys = [v["key"] for v in base.get("variants", [])]
    if args.variant or not keys:
        keys = [args.variant]
    elif not args.out_dir and not (args.check or args.check_runtime):
        parser.error("choose --variant or use --out-dir for all builds")
    bundles = [load_bundle(key) for key in keys]
    default_name = (bundles[0][0].get("hbmame", {}).get("setname", base["set"])
                    if platform == "hbmame" else base["set"] + "_patched") + ".zip"
    output = Path(args.out_dir or args.output or default_name).resolve()
    if args.target and Path(args.target).resolve() == output:
        parser.error("output must differ from input")
    resolver = Resolver(search_paths(args.target, args.rompath, HERE),
                        hints=[base["set"], *base.get("parents", []), "qsound", "qsound_hle"],
                        exclude=[output], progress=print)
    needed = {}
    for manifest, _ in bundles:
        need_full = platform in ("mame", "all") and base.get("mame_build", True)
        need_hb = (platform in ("hbmame", "all") or (platform == "mister" and base.get("mister_hbmame"))) and bool(manifest.get("hbmame"))
        if platform == "mame" and not base.get("mame_build", True):
            raise RomError("This kit has no MAME build; select HBMAME or MiSTer")
        if platform == "hbmame" and not need_hb:
            raise RomError("This kit has no HBMAME mapping")
        if need_full or need_hb or args.check_runtime:
            for spec in requirements(manifest, compact=not (need_full or args.check_runtime)):
                if spec["name"] in needed and needed[spec["name"]] != spec:
                    raise RomError("Variants require conflicting stock identities")
                needed[spec["name"]] = spec
    source = resolver.resolve(needed.values())
    devices = resolver.devices(include=args.include_devices) if needed else {}
    if args.check_runtime:
        resolver.devices(include=True)
        print("Game and firmware content verified; MiSTer also needs its MRA-named archive paths on the card.")
    if args.check or args.check_runtime:
        print("Build inputs verified")
        return
    # Build and verify every selected result before publishing any file.
    outputs = {}
    for manifest, patches in bundles:
        key = manifest.get("variant", {}).get("key")
        if platform in ("all", "mame") and base.get("mame_build", True):
            name = str(Path("mame") / (key or "") / (base["set"] + ".zip"))
            outputs[name] = build_members(manifest, patches, source) | devices
        if manifest.get("hbmame") and (platform in ("all", "hbmame") or (platform == "mister" and base.get("mister_hbmame"))):
            name = "hbmame/" + manifest["hbmame"]["setname"] + ".zip"
            members = build_members(manifest, patches, source, compact=True)
            if platform in ("all", "hbmame"):
                outputs[name] = members
            if platform in ("all", "mister") and base.get("mister_hbmame"):
                outputs["mister/games/" + name] = members
        if platform in ("all", "mister") and manifest.get("mra"):
            outputs[manifest["mra"]] = (Path(HERE) / manifest["mra"]).read_bytes()
    if not outputs:
        raise RomError("No output is available for the selected platform")
    if args.out_dir:
        # A staging/backup transaction protects existing destinations on failure.
        from rom_sources import publish_tree
        publish_tree(output, outputs)
    else:
        write_zip(output, next(iter(outputs.values())))
    print(f"Built game files: {output}")
    if platform in ("all", "hbmame"):
        print("Compact HBMAME clones use the stock game archives and QSound in your emulator ROM path.")
    if platform in ("all", "mister"):
        print("MiSTer MRAs use stock archives on the card; renamed input archives are not installed automatically.")


if __name__ == "__main__":
    try:
        main()
    except (RomError, OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}")
