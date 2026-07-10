#!/usr/bin/env python3
"""Apply this GD-ROM patch to your own CHD dump of the game.

Usage:
    python3 apply.py /path/to/sfz3ugd/gdl-0002.chd
    python3 apply.py /path/to/gdl-0002.chd -o gdl-0002-patched.chd
    python3 apply.py gdl-0002.chd --chdman /path/to/chdman --tmp /big/disk

Requires Python 3.8+ and chdman (part of every MAME distribution). The patch
contains no game data: it records which bytes of your own disc image change.
Your CHD is verified before anything runs, the rebuilt CHD is verified after,
and about 2.5 GB of temporary disk space is used in between.

The whole pipeline is checksummed end to end: if anything does not match the
expected original, nothing is written.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def run_chdman(chdman, args):
    proc = subprocess.run([chdman] + args, capture_output=True, text=True)
    if proc.returncode != 0:
        fail(f"chdman {' '.join(args[:1])} failed:\n{proc.stderr.strip()}")
    return proc.stdout + proc.stderr


def chd_hashes(chdman, path):
    """Return (sha1, data_sha1) reported by chdman info."""
    out = run_chdman(chdman, ["info", "-i", path])
    sha1 = data_sha1 = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SHA1:"):
            sha1 = line.split(":", 1)[1].strip()
        elif line.startswith("Data SHA1:"):
            data_sha1 = line.split(":", 1)[1].strip()
    if not sha1:
        fail(f"could not parse chdman info output for {path}")
    return sha1, data_sha1


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("chd", help="your dump of the game (gdl-0002.chd)")
    ap.add_argument("-o", "--output", help="output CHD path (default: <name>-patched.chd)")
    ap.add_argument("--chdman", default="chdman", help="path to the chdman executable")
    ap.add_argument("--tmp", help="directory for ~2.5 GB of temporary files")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "manifest.json"), encoding="utf-8") as f:
        m = json.load(f)
    patch = m["patch"]

    print(f"{m['title']} ({m['version']})")
    print(f"Target: MAME set '{m['set']}' - {m['game']}\n")

    if not shutil.which(args.chdman):
        fail(
            "chdman not found. It ships with MAME - install MAME or pass "
            "--chdman /path/to/chdman"
        )

    out_path = args.output
    if not out_path:
        base, ext = os.path.splitext(args.chd)
        out_path = base + "-patched" + (ext or ".chd")
    if os.path.abspath(out_path) == os.path.abspath(args.chd):
        fail("output path must differ from input path")

    # 1. Verify the input dump. The CHD SHA1 covers the decompressed data and
    #    metadata, so this check is independent of which chdman version made
    #    the container.
    print("Checking your dump...")
    sha1, data_sha1 = chd_hashes(args.chdman, args.chd)
    if sha1 == m["target"]["chd_sha1"]:
        fail("this CHD is already patched")
    if sha1 != m["source"]["chd_sha1"] and data_sha1 != m["source"]["data_sha1"]:
        fail(
            f"this CHD does not match the expected original.\n"
            f"  expected SHA1 {m['source']['chd_sha1']}\n"
            f"  found    SHA1 {sha1}\n"
            f"You need the unmodified {m['set']} GD-ROM dump ({m['chd']})."
        )

    tmp_parent = args.tmp or os.path.dirname(os.path.abspath(out_path)) or "."
    tmpdir = tempfile.mkdtemp(prefix="chdpatch-", dir=tmp_parent)
    try:
        # 2. Unpack the disc image.
        print("Extracting disc image (this takes a minute)...")
        gdi = os.path.join(tmpdir, "disc.gdi")
        run_chdman(args.chdman, ["extractcd", "-i", args.chd, "-o", gdi])

        # 3. Locate the track file and apply the byte patch.
        with open(gdi, encoding="ascii") as f:
            lines = f.read().split()
        # gdi: count, then 6 fields per track; field 5 (index 4) is the filename
        track_files = {}
        fields = lines[1:]
        for t in range(int(lines[0])):
            row = fields[t * 6 : (t + 1) * 6]
            track_files[int(row[0])] = row[4]
        track_path = os.path.join(tmpdir, track_files[patch["track"]])

        old = bytes.fromhex(patch["old"])
        new = bytes.fromhex(patch["new"])
        with open(track_path, "r+b") as f:
            f.seek(patch["offset"])
            cur = f.read(len(old))
            if cur != old:
                fail(
                    f"track {patch['track']} bytes at {patch['offset']:#x} do not "
                    f"match the original (found {cur.hex()}, expected {old.hex()})"
                )
            f.seek(patch["offset"])
            f.write(new)
        print(f"Patched {len(new)} bytes in track {patch['track']}.")

        print("Verifying patched track...")
        if sha1_file(track_path) != patch["track_sha1_after"]:
            fail("patched track checksum mismatch")

        # 4. Rebuild the CHD.
        print("Rebuilding CHD (this takes a few minutes)...")
        if os.path.exists(out_path):
            os.remove(out_path)
        run_chdman(args.chdman, ["createcd", "-i", gdi, "-o", out_path])

        # 5. Verify the result.
        out_sha1, _ = chd_hashes(args.chdman, out_path)
        if out_sha1 != m["target"]["chd_sha1"]:
            os.remove(out_path)
            fail(
                f"rebuilt CHD SHA1 {out_sha1} does not match the expected "
                f"{m['target']['chd_sha1']}"
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\nWrote {out_path}")
    print(f"  SHA1: {m['target']['chd_sha1']} (verified)")
    print(f"\nPlace it as {m['set']}/{m['chd']} in a MAME rompath ahead of the")
    print(f"stock set, keeping {m['set']}.zip available. MAME will report a")
    print("checksum warning for the CHD; that is expected and the game runs")
    print("on Japanese, USA, and Export BIOS regions.")


if __name__ == "__main__":
    main()
