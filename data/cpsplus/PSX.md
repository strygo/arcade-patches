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

- **MAME** on your PATH, or its path in `mame_bin` in `discs.toml`.
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
from 0.288. Reproduction has been verified on a Mac; whether every MAME
build on every platform is bit-identical has not been proven, which is why
the check is there.

The capture script runs MAME with its own empty configuration folder, so
your personal MAME settings do not affect the recording, and it never
touches your MAME installation.
