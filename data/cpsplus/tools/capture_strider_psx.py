#!/usr/bin/env python3
"""Record the Strider (PlayStation) Sound Remix performances with MAME.

The PlayStation port's "Sound Remix" music is sequenced (SEP/VAB) and played
by the game's own driver through the SPU, so there is no recording on the
disc to copy.  This drives the game's sound test in MAME and captures the
console's audio output, which is how the published pack's source was made.
build_pack.py strider-psx then cuts the ten reviewed performances from the
recording and checks each against its pinned hash, so the result is exactly
the reviewed audio or nothing.

What you supply:
  --disc      your Strider (USA) PlayStation disc rip, as .chd or as a .cue
              with its .bin file(s) beside it (the disc bundled with
              Strider 2 in North America)
  --bios-dir  a folder holding MAME's PlayStation ROM sets under their MAME
              names: psu.zip (the console BIOS set) and psx_cd.zip (the CD
              controller ROMs).  The capture runs on the DTL-H1001 version
              2.0 BIOS, ps-20a.bin, which is part of the standard psu set.
  --mame      the MAME executable (default: mame on PATH)

The output is a 48 kHz stereo WAV of about 300 MB: 26 minutes of emulated
time, about five minutes of wall clock, and MAME needs around 300 MB of RAM
for it.  Verified byte-for-byte with MAME 0.288; a different MAME build may
render differently, in which case the hash check stops the build rather
than staging different audio.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LUA = HERE / "strider_psx_capture.lua"

MACHINE = "psu"
BIOS = "2.0a"                                   # DTL-H1001 (Version 2.0 05/07/95 A)
BIOS_FILE = ("ps-20a.bin", "649895efd79d14790eabb362e94eb0622093dfb9")   # sha1
ROM_SETS = ("psu", "psx_cd")
# The published pack's source: the recording this schedule produces on the
# verified disc.  It is also pinned in manifests/strider_psx_audio.json.
EXPECT_SHA256 = "7cd3835135e3090ac6d7cf1af191fe0529b93e47f5f7720d28deab88a13394a4"
VERIFIED_DISC_SHA256 = "6b268854074f78e868fd96c08514dfd0b24e4314f0c712ea6900ab99ff7df029"
SOUND_TEST_INDEX = 0x4d8cc      # the sound test's selection, observed in RAM
END_FRAME = 93510


def schedule() -> tuple[str, str]:
    """The input schedule: title -> options -> Sound Remix on -> BGM test,
    then ten plays, each preceded by a stop (Triangle) and a slot select."""
    inputs = ["2450-2470:P1_START", "2620-2622:P1_JOYSTICK_DOWN",
              "2680-2682:P1_JOYSTICK_DOWN", "2740-2742:P1_BUTTON2"]
    inputs += [f"{3500 + i * 15}-{3502 + i * 15}:P1_JOYSTICK_DOWN" for i in range(11)]
    inputs += ["3700-3702:P1_JOYSTICK_RIGHT"]
    inputs += [f"{3800 + i * 15}-{3802 + i * 15}:P1_JOYSTICK_UP" for i in range(5)]
    pokes = []
    for index in range(10):
        frame = 4000 + 9000 * index
        stop = frame - (300 if index else 20)
        inputs += [f"{stop}-{stop + 2}:P1_BUTTON4", f"{frame}-{frame + 2}:P1_BUTTON2"]
        pokes.append(f"{frame - 10}:{SOUND_TEST_INDEX:#x}={index}")
    return ";".join(inputs), ";".join(pokes)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def find_set(bios_dir: Path, name: str) -> Path | None:
    for ext in (".zip", ".7z"):
        p = bios_dir / f"{name}{ext}"
        if p.exists():
            return p
    return None


def check_bios(bios_dir: Path) -> None:
    missing = [n for n in ROM_SETS if find_set(bios_dir, n) is None]
    if missing:
        sys.exit(f"{bios_dir}: missing MAME ROM set(s) {', '.join(missing)} "
                 f"(.zip or .7z, MAME naming)")
    psu = find_set(bios_dir, "psu")
    if psu.suffix.lower() == ".zip":
        name, want = BIOS_FILE
        with zipfile.ZipFile(psu) as z:
            if name not in z.namelist():
                sys.exit(f"{psu}: does not contain {name}, the BIOS this "
                         f"capture runs on")
            got = hashlib.sha1(z.read(name)).hexdigest()
        if got != want:
            sys.exit(f"{psu}: {name} is not the expected dump (sha1 {got})")


def check_disc(disc: Path) -> None:
    if disc.suffix.lower() not in (".chd", ".cue"):
        sys.exit(f"{disc}: pass the rip as .chd or .cue (with its bins beside it)")
    if disc.suffix.lower() == ".chd":
        got = sha256(disc)
        if got != VERIFIED_DISC_SHA256:
            print(f"note: {disc.name} is not byte-identical to the verified "
                  f"CHD (sha256 {got[:12]}...). A CHD made from the same disc "
                  f"can still differ in container bytes; the recording is "
                  f"checked after the run either way.")


def mame_version(mame: str) -> str:
    try:
        out = subprocess.run([mame, "-version"], capture_output=True, text=True,
                             timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.exit(f"cannot run {mame}: {e}")
    return out.splitlines()[0] if out else "unknown"


def capture(disc: Path, bios_dir: Path, mame: str, out: Path, work: Path,
            timeout_s: float) -> int:
    work.mkdir(parents=True, exist_ok=True)
    for d in ("ini", "cfg", "nvram"):
        (work / d).mkdir(exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    inputs, pokes = schedule()
    env = os.environ | {
        "SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy",
        "STRIDER_SCHEDULE": inputs, "STRIDER_POKES": pokes,
        "STRIDER_END_FRAME": str(END_FRAME),
    }
    # -inipath points at an empty folder so a personal mame.ini (sample rate,
    # frame skip, plugins) cannot change the recording.
    cmd = [mame, MACHINE, "-bios", BIOS,
           "-rompath", str(bios_dir.resolve()),
           "-inipath", str((work / "ini").resolve()),
           "-cfg_directory", str((work / "cfg").resolve()),
           "-nvram_directory", str((work / "nvram").resolve()),
           "-skip_gameinfo", "-noautosave", "-nothrottle",
           "-video", "none", "-sound", "none", "-samplerate", "48000",
           "-autoboot_script", str(LUA),
           "-wavwrite", str(out.resolve()),
           "-cdrom", str(disc.resolve())]
    (work / "command.txt").write_text(" ".join(cmd) + "\n")
    print(f"MAME: {mame_version(mame)}")
    print(f"running the sound test ({END_FRAME} frames, about 26 minutes of "
          f"emulated time)...", flush=True)
    with open(work / "mame.log", "w") as log:
        try:
            r = subprocess.run(cmd, cwd=work, env=env, stdout=log,
                               stderr=subprocess.STDOUT, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            print(f"MAME did not finish within {timeout_s / 60:.0f} minutes; "
                  f"see {work / 'mame.log'}")
            return 1
    if r.returncode or not out.exists():
        print(f"MAME exited with {r.returncode}; see {work / 'mame.log'}")
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--disc", type=Path, required=True)
    ap.add_argument("--bios-dir", type=Path, required=True)
    ap.add_argument("--mame", default=None, help="MAME executable (default: on PATH)")
    ap.add_argument("--out", type=Path, default=HERE.parent / "work" / "strider_psx" / "audio.wav")
    ap.add_argument("--work", type=Path, default=None,
                    help="MAME's config and log folder (default: beside --out)")
    ap.add_argument("--timeout-minutes", type=float, default=45)
    a = ap.parse_args(argv)

    mame = a.mame or shutil.which("mame") or shutil.which("mame.exe")
    if not mame:
        sys.exit("MAME not found: install it and put it on PATH, or pass --mame")
    if not LUA.exists():
        sys.exit(f"missing {LUA}")
    disc = a.disc.expanduser().resolve()
    bios_dir = a.bios_dir.expanduser().resolve()
    if not disc.exists():
        sys.exit(f"{disc}: no such file")
    if not bios_dir.is_dir():
        sys.exit(f"{bios_dir}: no such folder")
    check_disc(disc)
    check_bios(bios_dir)
    if a.out.exists() and sha256(a.out) == EXPECT_SHA256:
        print(f"recording already present and verified: {a.out}")
        return 0
    work = a.work or a.out.parent / "mame"
    if capture(disc, bios_dir, mame, a.out, work, a.timeout_minutes * 60):
        return 1
    got = sha256(a.out)
    if got == EXPECT_SHA256:
        print(f"recording verified: {a.out} ({a.out.stat().st_size} bytes)")
        return 0
    print(f"recording differs from the published source (sha256 {got[:12]}..., "
          f"expected {EXPECT_SHA256[:12]}...).\n"
          f"Usual causes: a different disc release or rip, a different BIOS "
          f"revision, or a MAME build that renders differently (verified: "
          f"MAME 0.288). The file is kept at {a.out} for inspection; the pack "
          f"builder will refuse it.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
