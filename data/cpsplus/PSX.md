# The Strider (PSX Soundtrack) pack: recording the sound test

The PlayStation port of Strider has a "Sound Remix" option: ten
re-arranged performances of the arcade score. They are not audio tracks on
the disc. They are sequences (SEP/VAB) that the game's own sound driver
plays through the console's SPU, with the game's own effects. There is no
recording to copy, so the kit makes one, the same way the published pack's
source was made: MAME boots your disc, walks into the game's sound test,
plays each of the ten performances, and writes what the console outputs to
a WAV. The pack builder then cuts the ten reviewed performances out of that
recording, checks every one against its pinned hash, and encodes the pack.

## What you need

- **MAME** on your PATH, or its path in `mame_bin` in `discs.toml` (the
  executable, or the folder holding it; `mame64.exe` from older Windows
  builds and a `C:\MAME` install are found too).
  Verified with MAME 0.288 (`brew install mame` on a Mac, your package
  manager on Linux, the official build from mamedev.org on Windows).
- **Your Strider (USA) disc rip**: `.chd`, or `.cue` with its `.bin` files
  beside it. This is the disc bundled with Strider 2 in North America.
- **MAME's PlayStation ROM sets**, in a folder of your choosing, under their
  MAME names: `psu.zip` (the US console BIOS set; the recording runs on its
  DTL-H1001 version 2.0 file, `ps-20a.bin`) and `psx_cd.zip` (the CD
  controller ROMs). These are copyrighted and are not part of the kit,
  like the SC-55 ROMs the X68000 packs need.

Fill in `strider_psx_disc` and `psx_bios_dir` in `discs.toml` and run
`make_packs.py` as usual. The recording takes about five minutes of wall
clock for 26 minutes of emulated time, needs about 300 MB of RAM, and
writes a 300 MB WAV under `work/`, which a successful run removes.

## What "verified" means here

The recording is deterministic: the same disc, the same BIOS and the same
MAME build produce the same bytes, and the kit checks for exactly the
bytes the published pack was cut from. If your recording differs, the
builder stops and says so rather than encoding different audio. The usual
causes are a different disc release, a different BIOS revision selected in
`psu.zip`, or a MAME build whose PlayStation emulation renders differently
from the verified ones.

Two MAME 0.288 builds are verified: Homebrew's on a Mac (Apple Silicon) and
the official Windows build from mamedev.org. They do not record identical
bytes. MAME's PlayStation sound emulation keeps volume envelopes in
floating point, and the two builds round a few thousand samples 1-3 steps
differently, far below anything audible. The kit accepts both recordings,
and each has its own pinned hashes, so the pack you build matches the one
published for your platform. Other builds (Linux packages, Intel Macs) have
not been checked; if yours records differently the builder says so.

The capture script runs MAME with its own empty configuration folder, so
your personal MAME settings do not affect the recording, and it never
touches your MAME installation. The MAME command line it ran is saved as
`work/strider_psx/mame/command.txt`, quoted for your platform's shell, with
MAME's own output beside it in `mame.log` (the input schedule itself reaches
MAME's script through environment variables, so run the capture through the
script rather than by pasting that line). If the previous recording cannot
be replaced because another program has it open, close that program (or
delete the WAV) and run again.
