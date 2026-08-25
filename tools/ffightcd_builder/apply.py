#!/usr/bin/env python3
"""Rebuild Final Fight CD's cutscene backport from your own disc and romset.

The sets are built on your machine, from your own files:

  * your Final Fight CD disc supplies the cutscenes.  A 68000 interpreter
    executes the disc's own scene script and renders the Mega Drive video
    state it produces, so the frames are the game's output rather than a
    recording of it;
  * your arcade Final Fight romset supplies everything else -- program,
    sound, samples and the stock graphics.

Usage:
    python3 apply.py --disc-us "Final Fight CD (USA) (Track 01).bin" \
                     --romset /path/to/roms --out-dir out
    python3 apply.py --disc-jp "Final Fight CD (JP).img" \
                     --romset /path/to/roms --out-dir out

Each disc is the raw track-1 image of that region's Final Fight CD: the
.img or .bin beside the rip's .cue sheet (2352-byte sectors).  Pass the
image itself, not the .cue.  Supply both discs to build both regions.

Produces, under out/ (point both regions at the same out/ -- nothing
collides):

    hbmame/<set>.zip              for HBMAME
    mister/_Arcade/_Backports/<name>.mra   MiSTer, laid out like the SD card
    mister/games/hbmame/<set>.zip can copy mister/'s contents to the root

Requires Python 3.9+, numpy, Pillow, and 7zz (or 7z) for the romset.
The first build runs the scene player over both bundles and takes several
minutes; it prints each stage as it goes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETS = {"us": "ffightus01", "jp": "ffightjs01"}

# per-member SHA-256 of the published sets; a finished build must match
EXPECTED = {
    "ffightus01": {
        "c07.c01": "ad1062a24a47c8bc4f57db1d1ff7a1854811d306bf3aeb03823a1ef9b2235d6e",
        "c07.c03": "d7641b5679c32272b067a329a3a791e5116a103c46746c1241215a5993a1083c",
        "c07.c05": "cd9793340805e7417de443ba77787a13a8712452d41361d1b8fcc168ddd1c643",
        "c07.c07": "828ca6fb9d6c75e803f5551af23e2ceba31ba1a44120bc3a3d70895d5ff4c816",
        "c07.m1": "9ef9aa8c7d236046404003b8900826cc834450ccd7284145fd93660ed3b438a8",
        "c07.p1": "d3e3952fb762996bc5b8192cb0ca48cbfb820791c6a39114525248cf8d928cea",
        "c07.p2": "ccccce3e5b9a3fbe726e34a36f3d2d59cecef386c3f39e3797282c002f02ef30",
        "c07.p3": "fef402f58aa14f2dc138b05a44006f249b4ecc7ffaafa8309e35356c48b87f88",
        "c07.p5": "b2b4c0060b4151635d185ae1cd7e93262f5eef6f15a77371a5ff6120883a676b",
        "c07.v1": "ce61fd555f583a007ca544886683b1f3589aba55009db72b331884180b765d17",
        "c07.v2": "dd897bea75f2927786315fc186b619a570be791f1d3de84a368e95c17c7537b8",
        "c07u.p4": "4a43ad06011f101e557fb7f75a070a9c1255c8af735d1fb772abf559c7138f59",
        "c07us01.c09": "00cbb3be3de2bc25a07649e677203534d3a8131722d84e4e718174373511397c",
        "c07us01.c11": "7454ed428575512c3614181a33c0fee8356db881a111cb89b0d61b9267ad2686",
        "c07us01.c13": "1c4b90de66d223b41ff26861bfcce7feb896df3c5db78938e97a6fe71009d637",
        "c07us01.c15": "52c51beb4b2f0123f2a38db718a84e001233d1ab010133e6f14ada644032f772",
        "c07us01.c17": "c1c8ccdd325169cd7a119f2e01d86317051efc11c4d3f5fd5e677946816ad861",
        "c07us01.c19": "645c79d16614c789690e40f6403f5febcefe30a00ee7c06bb5e033b0c2e1227c",
        "c07us01.c21": "6073f3c6255a1fbb26d9b7a8d5764812be446054c89a0bca5a82d4849453fa4f",
        "c07us01.c23": "d230b4ff4300d40f9db492fcf6e88ebbada379660980d7f2138a94f362293eb5",
        "c07us01.p7": "81be80c9763a55de8f44d04f9e6a96821a6ded33dc82b99efe00e8d00b37c453",
        "ffightus01.key": "b64056b0aa20b4a01fd2c73003d8d583d51df2c833478e16bef595e862385524",
    },
    "ffightjs01": {
        "c07.c01": "756585ed5f8f5ca1fa0b2526b5ad130bdb6104ef1c571654a0721842af5e29ed",
        "c07.c03": "7c38ea89761f80da8726c339231ea7b31391b8731ef982f49b86be3fffebcb43",
        "c07.c05": "2def49e3c5cddf4fcf6936c1ccc29f27016855a91cd635673c43e3ed097fd5dd",
        "c07.c07": "802d3a1d439063d2ae290c6b5c2068a22e234f3c9a5cff586e5c1a21605afeed",
        "c07.m1": "f87cf6f645a41561f1ad190f0abd871272d90b9830694b18793d71844ee8cded",
        "c07.p1": "27d16e9df0d4a718659a6cc7706687015ed0161575a15352b79029998894abde",
        "c07.p2": "c1f60638a50684359955d9fe21fb2f9e4f2de7de9c480bfc96cd9a88796f73ff",
        "c07.p3": "fef402f58aa14f2dc138b05a44006f249b4ecc7ffaafa8309e35356c48b87f88",
        "c07.p5": "0e6bd5ba0c5c967a7c7ac77bb9641f03a3c10ab0690729ead62fe667b3895799",
        "c07.v1": "ce61fd555f583a007ca544886683b1f3589aba55009db72b331884180b765d17",
        "c07.v2": "dd897bea75f2927786315fc186b619a570be791f1d3de84a368e95c17c7537b8",
        "c07j.p4": "e428ece731cd49e9605fce2cbd3d6254d9b4b63e66feb1c83d67b1ddbff31c39",
        "c07js01.c09": "bf6c3f4f28d0b71d41a197ef8fb96458fdd20f169b71e720b48be38c82af3dbf",
        "c07js01.c11": "88483ec8e5ff9404e3ad87019bf9f5aba4937a8cef90013172a5036a4b5658b1",
        "c07js01.c13": "bf969e7cb7b7fd8d020cc1d63739d8dd8f090c69f3b5873668bcd25b783861db",
        "c07js01.c15": "45f39135d08e9c1135d48ef28db10fc1c96f74b04ee1c01089fc8c04129024cd",
        "c07js01.c17": "859f2bf9822834581312800a3c33c1a42bf43ae24c910767d461275ec28887d7",
        "c07js01.c19": "f3bb2fb0d6fbd04dec8addee0e6bc6dd2f0b614dc8776192e6c4bef160931306",
        "c07js01.c21": "55b6d40c3f7c1fd023e81faceeb7ff2d0c004f4434d375228a4f79e807de176d",
        "c07js01.c23": "61f2d5460db462fc66b8b7b26e4517fef76538fe1e4a3a3da0076725aeb3d22a",
        "c07js01.p7": "6a0b0481b705c2b499da3ac187c212aac4a86eb319abf05b7e99f945082092b6",
        "ffightjs01.key": "b64056b0aa20b4a01fd2c73003d8d583d51df2c833478e16bef595e862385524",
    },
}
# the arcade archives each region's stage0 is patched from
# Romset archives, in preference order.  Base names only: MAME sets ship as
# .zip at least as often as .7z, and hardcoding one extension rejected a
# perfectly good romset with "not found".  Both are tried for each name.
STAGE0_SRC = {"us": ("ffightu", "ffight"), "jp": ("ffightj",)}
ROMSET_EXTS = (".7z", ".zip")


def sh(*cmd, **kw):
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def seven_zip() -> str:
    for exe in ("7zz", "7z", "7za"):
        if shutil.which(exe):
            return exe
    raise SystemExit("need 7zz or 7z on PATH to read the arcade romset")


def stage0(region: str, romset: Path, out: Path) -> None:
    """The retimed program ROMs: the user's own, plus our patch list.

    The arcade story sequence is retimed by a few dozen bytes so the CD
    cutscenes fit the beats.  That is a PATCH, not a ROM -- the kit ships
    the offsets and values and applies them here, so no arcade code is
    distributed.
    """
    out.mkdir(parents=True, exist_ok=True)
    xml = (HERE / "data" / "stage0" / region / "retime_patches.mra.xml").read_text()
    patches = [(int(o, 16), bytes.fromhex(v.replace(" ", "")))
               for o, v in re.findall(r'<patch offset="([^"]+)">([^<]+)</patch>', xml)]
    ez = seven_zip()
    names = {"us": ("ff_36.11f", "ff_42.11h"), "jp": ("ff36.bin", "ff42.bin")}[region]
    src = None
    for stem in STAGE0_SRC[region]:
        for ext in ROMSET_EXTS:
            p = romset / f"{stem}{ext}"
            if p.exists():
                src = p
                break
        if src:
            break
    if src is None:
        want = ", ".join(f"{s}{e}" for s in STAGE0_SRC[region] for e in ROMSET_EXTS)
        raise SystemExit(f"romset not found: need one of {want} in {romset}")
    sh(ez, "e", "-y", f"-o{out}", src, *names, capture_output=True)
    a, b = (out / names[0]).read_bytes(), (out / names[1]).read_bytes()
    # the two program ROMs interleave into one 16-bit image; patch offsets
    # are image addresses, so de-interleave, patch, and write back
    # The MRA interleave names the halves: ff_36 maps "10" (the high byte,
    # even image offsets) and ff_42 maps "01" (the low byte, odd offsets).
    hi, lo = a, b                      # names[] is (ff36, ff42) per region
    img = bytearray(len(hi) + len(lo))
    img[0::2], img[1::2] = hi, lo
    for off, val in patches:
        img[off:off + len(val)] = val
    (out / "ff_36.11f").write_bytes(bytes(img[0::2]))
    (out / "ff_42.11h").write_bytes(bytes(img[1::2]))
    for n in names:
        if n not in ("ff_36.11f", "ff_42.11h"):
            (out / n).unlink(missing_ok=True)
    print(f"  stage0: {len(patches)} retime patches applied to your romset")


def stage1_parts(out: Path) -> None:
    """The appended graphics region's pattern fill.

    Our own data, generated rather than shipped: tile N is
    byte[i] = (N >> 8) ^ (N & 0xFF) ^ i, emitted in the 64-bit interleave
    every c07.cNN member uses.
    """
    out.mkdir(parents=True, exist_ok=True)
    space = bytearray(0x200000)
    for slot in range(0x4000, 0x8000):
        base = (slot - 0x4000) * 128
        n_hi, n_lo = (slot >> 8) & 0xFF, slot & 0xFF
        for i in range(128):
            space[base + i] = (n_hi ^ n_lo ^ i) & 0xFF
    for fi, name in enumerate(("c07us01.c09", "c07us01.c11",
                               "c07us01.c13", "c07us01.c15")):
        d = bytearray(0x80000)
        for w in range(len(d) // 2):
            d[w * 2:w * 2 + 2] = space[w * 8 + fi * 2:w * 8 + fi * 2 + 2]
        (out / name).write_bytes(bytes(d))
    print("  stage1 parts: generated")


def write_mra(region: str, zip_path: Path, out: Path) -> Path:
    """The MiSTer MRA, with its CRCs taken from the zip we just built.

    Computed, never copied: an MRA whose checksums are pinned by hand drifts
    from the set the moment the set is rebuilt.
    """
    base = (HERE / "mras" / f"{SETS[region]}.mra").read_text()
    with zipfile.ZipFile(zip_path) as z:
        crc = {n: format(zlib.crc32(z.read(n)) & 0xFFFFFFFF, "08x")
               for n in z.namelist()}
    missing = []

    def fix(m):
        name = m.group(1)
        if name not in crc:
            missing.append(name)
            return m.group(0)
        return f'name="{name}" crc="{crc[name]}"'

    text = re.sub(r'name="([^"]+)"\s+crc="[0-9a-fA-F]+"', fix, base)
    if missing:
        raise SystemExit(f"MRA names parts not in the built set: {missing}")
    name = {"us": "Final Fight (CD Cutscenes)",
            "jp": "Final Fight (CD Cutscenes, Japan)"}[region]
    text = re.sub(r"<name>[^<]*</name>", f"<name>{name}</name>", text, count=1)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{name}.mra"
    p.write_text(text)
    return p


def disk_mb(root: Path) -> float:
    """Megabytes root actually occupies.

    The renderer hardlinks repeated frames -- a sweep of 7790 frames holds
    about 2000 distinct ones -- so adding up file sizes reports roughly four
    times the disk that deleting the tree gives back.  Count each inode once.
    """
    seen: set[int] = set()
    total = 0
    for f in root.rglob("*"):
        if f.is_file():
            st = f.stat()
            if st.st_ino not in seen:
                seen.add(st.st_ino)
                total += st.st_size
    return total / 1e6


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--region", default=None, choices=("us", "jp", "both"),
                    help="default: whichever regions you supplied a disc for")
    ap.add_argument("--disc-jp", type=Path, default=None,
                    help="your Final Fight CD (Japan) disc image")
    ap.add_argument("--disc-us", type=Path, default=None,
                    help="your Final Fight CD (USA) disc image")
    ap.add_argument("--romset", required=True, type=Path,
                    help="directory holding the arcade ffight romset archives")
    ap.add_argument("--out-dir", type=Path, default=Path("out"))
    ap.add_argument("--cache", action="store_true",
                    help="keep work/ after a successful build, to inspect the "
                         "renders and intermediates (a re-run rebuilds them "
                         "either way)")
    a = ap.parse_args()

    # Preflight the third-party deps.  Without this the first missing one
    # surfaces as a ModuleNotFoundError inside a stage subprocess, five
    # frames deep in someone else's traceback, several stages into a build.
    missing = []
    for mod, pkg in (("numpy", "numpy"), ("PIL", "Pillow")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        raise SystemExit(
            f"missing Python package(s): {', '.join(missing)}\n"
            f"  install them for THIS interpreter ({sys.executable}):\n"
            f"    {sys.executable} -m pip install {' '.join(missing)}\n"
            f"  (the stages run under the same interpreter that starts this "
            f"script, so installing them elsewhere will not help)")
    if not shutil.which("7zz") and not shutil.which("7z") and not shutil.which("7za"):
        raise SystemExit("need 7zz, 7z or 7za on PATH to read the arcade romset")

    discs = {r: d for r, d in (("jp", a.disc_jp), ("us", a.disc_us)) if d}
    for r, d in discs.items():
        if not d.exists():
            raise SystemExit(f"disc image not found: {d}")
        # catch the wrong container now, not as a hash mismatch minutes in.
        # The chain reads raw 2352-byte sectors, so the file must be the
        # track-1 .img/.bin, which starts with the CD sync pattern.
        if d.suffix.lower() in (".cue", ".7z", ".zip", ".chd", ".iso", ".mds", ".mdf"):
            raise SystemExit(
                f"--disc-{r}: {d.name} is not a raw track-1 image.\n"
                f"Pass the .img or .bin beside your rip's .cue sheet "
                f"(extract the archive first if the rip is compressed).")
        with open(d, "rb") as fh:
            head = fh.read(12)
        if head != b"\x00" + b"\xff" * 10 + b"\x00" or d.stat().st_size % 2352:
            raise SystemExit(
                f"--disc-{r}: {d.name} does not look like a raw 2352-byte "
                f"track-1 image (.img/.bin beside the .cue). A 2048-byte .iso "
                f"or a compressed rip will not work as-is.")
    if not discs:
        raise SystemExit("supply --disc-us and/or --disc-jp")
    if not a.romset.is_dir():
        raise SystemExit(f"romset directory not found: {a.romset}")

    # each region builds from its own disc; the region list follows the
    # discs unless --region narrows it
    regions = tuple(r for r in ("jp", "us") if r in discs)
    if a.region and a.region != "both":
        if a.region not in discs:
            raise SystemExit(f"--region {a.region} needs --disc-{a.region}")
        regions = (a.region,)
    elif a.region == "both" and len(discs) < 2:
        raise SystemExit("--region both needs both discs")
    print("building: " + ", ".join(SETS[r] for r in regions))

    # the chain resolves data/ and work/ against this directory
    cfg = {r: str(d.resolve()) for r, d in discs.items()}
    cfg["romset"] = str(a.romset.resolve())
    (HERE / "paths.json").write_text(json.dumps(cfg))

    py = sys.executable
    print("[1/5] preparing inputs from your own files")
    gen = HERE / "work" / "generated"
    for r in regions:
        stage0(r, a.romset, gen / "stage0" / r)
    stage1_parts(gen / "stage1_parts")

    print("[2/5] rendering the scenes (the slow part -- several minutes)")
    for r in regions:
        for bundle, dest in (("O", "open"), ("E", "end")):
            print(f"      {r} {bundle}")
            d = HERE / "work" / "build" / "vmsweep" / f"{r}_{dest}"
            # the renderer hardlinks repeated frames and will not write over
            # an existing sweep, so a re-run starts each one clean
            shutil.rmtree(d, ignore_errors=True)
            sh(py, HERE / "build" / "render.py", r, bundle, d)
    print("[3/5] slicing the ending")
    sh(py, HERE / "build" / "ending.py", "esnaps")
    print("[4/5] converting the openings")
    for r in regions:
        sh(py, HERE / "build" / "opening.py", r)
    print("[5/5] assembling the set(s)")
    for r in regions:
        sh(py, HERE / "build" / "pipeline.py", r, "--no-gate",
           "--stage0", gen / "stage0" / r,
           "--parts", gen / "stage1_parts")

    for r in regions:
        built = (HERE / "work" / "build" / "vmsweep" /
                 f"{r}_set" / f"{SETS[r]}.zip")
        if not built.exists():
            raise SystemExit(f"the chain did not produce {built}")
        import zipfile
        with zipfile.ZipFile(built) as z:
            got = {n: hashlib.sha256(z.read(n)).hexdigest()
                   for n in z.namelist()}
        want = EXPECTED[SETS[r]]
        bad = sorted(n for n in set(got) | set(want)
                     if got.get(n) != want.get(n))
        if bad:
            raise SystemExit(
                f"{SETS[r]}: {len(bad)} member(s) do not match the published "
                f"set: {', '.join(bad[:5])}\nThis usually means the disc or "
                f"romset is not the expected dump. Nothing was staged.")
        print(f"  {SETS[r]}: all {len(want)} members match the published set")
        hb = a.out_dir / "hbmame"
        hb.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, hb / f"{SETS[r]}.zip")
        mis = a.out_dir / "mister"
        (mis / "games" / "hbmame").mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, mis / "games" / "hbmame" / f"{SETS[r]}.zip")
        mra = write_mra(r, built, mis / "_Arcade" / "_Backports")
        print(f"\n  {SETS[r]}:")
        print(f"    {hb / (SETS[r] + '.zip')}")
        print(f"    {mra}")

    print(f"\nCopy the contents of {a.out_dir / 'mister'} to your MiSTer SD "
          f"card root.\nHBMAME and MAME warn about checksums -- this is an "
          f"unofficial build, and those warnings are expected.")

    # The sets are copied out by now, so work/ is spent: renders, convs and the
    # assembled sets, none of which the user needs.  paths.json goes with it --
    # it holds the absolute paths to your discs and romset, and every run
    # rewrites it.  Only reached when the build succeeded; a failure raises.
    scratch = HERE / "work"
    if not a.cache and scratch.is_dir():
        mb = disk_mb(scratch)
        shutil.rmtree(scratch)
        (HERE / "paths.json").unlink(missing_ok=True)
        print(f"\nremoved work/ ({mb:.0f} MB of renders and intermediates). "
              f"Pass --cache to keep them for inspection.")
    elif a.cache and scratch.is_dir():
        print(f"\n--cache: work/ kept ({disk_mb(scratch):.0f} MB of renders "
              f"and intermediates).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
