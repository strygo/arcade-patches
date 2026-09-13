"""M4 frame sweeps: run the script player and emit a 1-frame PNG sweep
(convert.py's snapdir format, fNNNNNN.png).  Hold frames are
hardlinked to the previous frame's file, so only unique video states pay
a render.  Stops automatically when the sequence loops (timer reset after
a part-end restart).  Usage:
    render.py jp|us O|E <outdir> [max_frames] [--parts a,b,c]

WHY `E` NEEDS THREE PARTS.  The ending was swept with the
player's default `PART_CODE = [5]`, one code part.  `after_tick` bumps
`part` at every part end, so the FIRST one took `part` to 1, tripped the
loop-detect below, and the sweep reported "done" at f2854 -- which read as
the end of the ending and is really the end of its first third.

Measured with `parts=[5,5,5]`: the ending is THREE invocations of the same
code chunk, ~2760 frames each, and the scene chunks pipeline across them --
fn 0x0C stages c1 at f4 and c2 at f1826 during part 0, then c3 (the CD-6
farewell) at f2859, five frames into part 1.  So the farewell was never
blocked by the driver's $94F8 state machine; it simply lives past a window
we stopped at.

The default is DELIBERATELY still [5].  `[5,5,5]` re-loads chunk 5 at each
part end where [5] only re-inits it, and guycody_export.py runs the same
player with no `parts` to reach the Guy/Cody window at f5600-6540 -- so
flipping the default would silently re-cut a scene that is already
reviewed and approved.  Pass --parts=5,5,5 to get the full ending; whether
the reload changes anything after f2854 is a measurement, not an
assumption.
"""
import argparse
import sys, os, zlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # track root: ffcd/ lives there
from ffcd.player import ScriptPlayer
from ffcd import megadrive
from ffcd.files import link_or_copy

_ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
_ap.add_argument("region", choices=("jp", "us"))
_ap.add_argument("bundle", choices=("O", "E"), help="O=opening, E=ending")
_ap.add_argument("out", help="output frame directory")
_ap.add_argument("maxf", type=int, nargs="?", default=12000,
                 help="frame ceiling (default 12000)")
_ap.add_argument("--parts", default=None,
                 help="comma-separated PART_CODE list; default [14] for O, "
                      "the player's own for E.  --parts=5,5,5 gets the full "
                      "ending -- see the note above before changing it.")
_a = _ap.parse_args()
REGION, BUNDLE, OUT, MAXF = _a.region, _a.bundle, _a.out, _a.maxf
os.makedirs(OUT, exist_ok=True)
# conversion parity: MAME's Genesis color ramp, not the linear debug ramp
megadrive.RAMP = megadrive.MAME_RAMP

if _a.parts:
    PARTS = [int(x) for x in _a.parts.split(",")]
else:
    PARTS = [14] if BUNDLE == "O" else None
p = ScriptPlayer(region=REGION, bundle=BUNDLE, parts=PARTS)
last_crc, last_path = None, None
nrend = 0
started = False
for f in range(MAXF):
    p.vblank()
    p.tick()
    prev_part = p.part
    p.after_tick()
    # loop detection: the cutscene-only part handler wraps past the last
    # part and restarts -- that's the end of one full playback
    if p.part != prev_part and p.part >= len(p.PART_CODE):
        print(f"loop at f{f}", flush=True)
        break
    if p.part < prev_part:
        print(f"restart at f{f}", flush=True)
        break
    v = p.bus.vdp
    crc = zlib.crc32(bytes(v.vram))
    crc = zlib.crc32(bytes(v.cram), crc)
    crc = zlib.crc32(bytes(v.vsram), crc)
    crc = zlib.crc32(bytes(v.regs[:24]), crc)
    path = f"{OUT}/f{f:06d}.png"
    if crc == last_crc and last_path:
        link_or_copy(last_path, path)
    else:
        vram, cram, regs, vsram = p.snapshot()
        img = megadrive.render_np(None, vscroll=(megadrive.vs(vsram[0]),
                                                megadrive.vs(vsram[1])),
                                 state=(vram, cram, regs, None))
        img.save(path, compress_level=1)
        nrend += 1
        last_crc = crc
    last_path = path
    if f % 1000 == 0:
        print(f"...f{f} renders={nrend}", flush=True)
print(f"done frames={f+1} renders={nrend}", flush=True)
