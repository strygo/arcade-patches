"""M4 stage A driver: number the VM sweep into the shot table's frame
numbering, run the SHIPPED convert.py with the canonical shot table,
and byte-compare against the shipped conv artifacts.  Usage:
    opening.py jp|us [--align-only]

ONE ROUTE, AND IT READS NO CAPTURE (Steve: "opening should not
read captures").  The shot tables are in VM frames, so there is no mapping
left to do -- see the note above build_aligned.

WHAT THE "aligned" DIRECTORY IS, because this confused everyone including
me: it was NEVER capture pixels.  It holds VM sweep frames hardlinked under
the shot table's numbering.  When that numbering was capture-derived the
capture was a NUMBERING ORACLE, never a pixel source; now it is not even
that.

There is no DTW search, no frame-exact refine, no pin-run flicker restore,
and no --align=const route.  A search can only score against the capture, and
the capture is a defective baseline: scoring against it places the Jessica
cel inside Damnd's laugh.  Measuring fidelity to a defective baseline
measures fidelity to defects.

No flicker restore is needed BY CONSTRUCTION: pinned runs -- many capture
frames collapsing onto one VM frame during a search -- do not arise when the
sweep is linked directly.  There are no pins, no re-linking, and the VM's own
flicker plays at the phase the disc's code produces.

That phase will not match the capture's everywhere -- the JP bar-panel
marquee-bulb cycle is the known case (capture 5055-5651).  That is provenance,
not drift: the capture is ONE playthrough's sample of a cycling animation,
and matching it would mean reproducing an accident of that recording.

--derive-offsets is the (capture-reading, DEVELOPMENT-ONLY) search that
produced the constants below and re-derives them whenever the VM clock
changes.  It is not part of the build; nothing on the build path calls it.

Scratch runs: --vmsweep / --aligned / --conv-out override the
sweep/aligned/conv dirs (defaults = the canonical vmsweep paths, behaviour
unchanged); --align-only stops after the per-shot
fidelity report (no prep/convert), for latency-model verification.
"""
import argparse
import sys, os, subprocess, hashlib
from pathlib import Path
from PIL import Image

HERE = Path(__file__).resolve().parent
TRACK = HERE.parent
_ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
_ap.add_argument("region", choices=("jp", "us"))
_ap.add_argument("--align-only", action="store_true",
                 help="stop after the per-shot fidelity report")
# Scratch-run overrides.  As flags (not FFCD_VMSWEEP / FFCD_ALIGNED /
# FFCD_CONV_OUT / FFCD_JP_ALIGNED environment variables) they cannot be set
# by accident, and --help says they exist.
_ap.add_argument("--vmsweep", type=Path, default=None,
                 help="sweep dir to read (default: the canonical vmsweep)")
_ap.add_argument("--aligned", type=Path, default=None,
                 help="aligned dir to write")
_ap.add_argument("--conv-out", type=Path, default=None,
                 help="conv dir to write")
_a = _ap.parse_args()
REGION = _a.region
ALIGN_ONLY = _a.align_only



# WHY THERE IS NO OFFSET CONSTANT HERE.
#
# The shot tables are authored in VM frames, so the mapping to the sweep is
# the identity: no  vm_frame = capture_frame - ALIGN_OFFSET[region]  step.
# A capture-derived offset would be valid at only one CD-clock rate -- the
# true offset walks monotonically down (jp 3709 -> 3700) whenever the VM
# ticks the nominal 60 Hz instead of the physical 59.92275 -- which would
# make the CD clock unfixable without captures: correcting the rate would
# invalidate the offset, and re-deriving it needs the very dependency this
# route exists to remove.  With the tables in VM frames there is no constant,
# no CD-rate guard, and no capture-reading search; nothing on the build path
# is derived from a recording.
# Shot-boundary tables: 22 lines of (shot, f_start, f_end) recipe metadata,
# no pixels.  They were measured against the capture but live in data/ending
# with their peers (*_ending_shots.tsv), NOT in work/capture -- nothing in the
# shipped build path may take an input from there (audit).
SHOTS = {"jp": TRACK / "data/ending/jp_opening_shots.tsv",
         "us": TRACK / "data/ending/us_opening_shots.tsv"}[REGION]
VMSWEEP = _a.vmsweep or TRACK / f"work/build/vmsweep/{REGION}_open"
ALIGNED = _a.aligned or TRACK / f"work/build/vmsweep/{REGION}_open_aligned"
CONV_OUT = _a.conv_out or TRACK / f"work/build/vmsweep/{REGION}_conv"
SHIPPED = TRACK / f"work/build/{REGION}/conv"
VENV = sys.executable          # stages run under the interpreter that launched us


def shot_rows():
    rows = []
    for line in SHOTS.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        f = line.split("\t")
        rows.append((int(f[0]), int(f[1]), int(f[2])))
    return rows


def thumb(im):
    return list(im.resize((40, 28)).getdata())


def build_aligned(rows):
    """Materialise the shot table's window out of the sweep.

    THERE IS NO ALIGNMENT.  The shot tables are in VM frames, so the mapping
    is the identity -- aligned[cf] = VMSWEEP[cf].  There is no  cf - K  offset
    with K a capture-derived constant, and so no CD-rate guard and no
    capture-reading search to maintain it; the build path takes nothing from
    a capture.

    The clamps stay: the window's tail (jp, the white flash into black) runs
    a few frames past the end of the sweep, and the sweep's last frame is
    the correct content to hold there.
    """
    vm_frames = sorted(int(p.stem[1:]) for p in VMSWEEP.glob("f*.png"))
    vmin, vmax = vm_frames[0], vm_frames[-1]
    lo = min(r[1] for r in rows)
    hi = max(r[2] for r in rows)
    ALIGNED.mkdir(parents=True, exist_ok=True)
    n = head = tail = 0
    for cf in range(lo, hi + 1):
        vf = cf
        if vf < vmin:
            vf, head = vmin, head + 1
        elif vf > vmax:
            vf, tail = vmax, tail + 1
        dst = ALIGNED / f"f{cf:06d}.png"
        if dst.exists():
            dst.unlink()
        os.link(VMSWEEP / f"f{vf:06d}.png", dst)
        n += 1
    print(f"aligned snapdir: {n} frames "
          f"(clamped head {head}, tail {tail})", flush=True)


def _hash_chunk(paths):
    """Full-resolution pixel hash of each frame (worker-pool entry point)."""
    import numpy as np
    return [hashlib.blake2b(np.asarray(img_at(p)).tobytes(),
                            digest_size=16).digest() for p in paths]


def main():
    rows = shot_rows()
    lo = min(r[1] for r in rows)
    hi = max(r[2] for r in rows)
    print(f"{REGION}: table {SHOTS.name} window {lo}-{hi}", flush=True)
    build_aligned(rows)
    if ALIGN_ONLY:
        print("--align-only: stopping before prep/convert", flush=True)
        return
    # prep stage (mirrors build.py stage_prep): the committed hand-repairs
    # that ride between capture and convert.  The aligned entries are
    # HARDLINKS into the VM sweep — always unlink before replacing, never
    # write through (build.py learned this the hard way).
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as td:
        flick = Path(td) / "flick"
        subprocess.run([VENV, str(HERE / "repair.py"), "flicker",
                        str(ALIGNED), str(flick), REGION], check=True)
        for f in sorted(flick.glob("f*.png")) if flick.exists() else []:
            (ALIGNED / f.name).unlink(missing_ok=True)
            shutil.copy2(f, ALIGNED / f.name)
        raster = Path(td) / "raster"
        subprocess.run([VENV, str(HERE / "repair.py"), "raster",
                        str(ALIGNED), str(raster), REGION], check=True)
        for f in sorted(raster.glob("f*.png")) if raster.exists() else []:
            (ALIGNED / f.name).unlink(missing_ok=True)
            shutil.copy2(f, ALIGNED / f.name)
        lbox = Path(td) / "lbox"
        subprocess.run([VENV, str(HERE / "repair.py"), "letterbox",
                        str(ALIGNED), str(lbox), REGION], check=True)
        for f in sorted(lbox.glob("f*.png")) if lbox.exists() else []:
            (ALIGNED / f.name).unlink(missing_ok=True)
            shutil.copy2(f, ALIGNED / f.name)
        if REGION == "us":
            mouth = Path(td) / "mouth"
            subprocess.run([VENV, str(HERE / "repair.py"), "mouth",
                            str(ALIGNED), str(mouth)],
                           check=True)
            for f in sorted(mouth.glob("f*.png")) if mouth.exists() else []:
                (ALIGNED / f.name).unlink(missing_ok=True)
                shutil.copy2(f, ALIGNED / f.name)
    CONV_OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run([VENV, str(HERE / "convert.py"),
                    str(ALIGNED), str(SHOTS), str(CONV_OUT),
                    "--window", f"{lo}-{hi}", "--crop", "320x224"],
                   check=True)
    print("\n=== parity vs shipped conv ===", flush=True)
    for name in ("tiles.bin", "palblocks.bin", "basemaps.bin", "deltas.bin",
                 "script.bin"):
        a, b = CONV_OUT / name, SHIPPED / name
        if not b.exists():
            print(f"{name}: shipped missing")
            continue
        da, db = a.read_bytes(), b.read_bytes()
        same = hashlib.md5(da).hexdigest() == hashlib.md5(db).hexdigest()
        if same:
            print(f"{name}: IDENTICAL ({len(da)} bytes)")
        else:
            nd = sum(1 for x, y in zip(da, db) if x != y)
            print(f"{name}: differs — {len(da)} vs {len(db)} bytes, "
                  f"{nd} differing in overlap")


if __name__ == "__main__":
    main()
