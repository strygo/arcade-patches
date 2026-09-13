#!/usr/bin/env python3
"""Build the shipped cutscene set for one region, from the VM sweeps.

This is the canonical chain: it mirrors build.py's
stages but reads the MODELED VM sweeps -- the clock-phase fix --
rather than the MAME capture.  Stages before this one are long; run them
yourself and reuse the output:

    build/render.py {jp,us} {O,E} work/build/vmsweep/<r>_{open,end}
    build/ending.py
    build/opening.py {jp,us}              # writes <r>_open_aligned, <r>_conv

This script runs the rest, which is where the base-code CHAINING lives:
every conv's base_code continues after the previous conv's tiles, and
getting that wrong is silent (the set builder asserts, or worse, the pan
art lands on the wrong codes).  Order and bases:

    econv  base = 0x4000 + conv.tiles
    e6conv base = 0x4000 + conv.tiles + econv.tiles

    jp: convert_opening(jp_esnap, jp_ending_shots) -> rconv
        export_layers -> gclayers; build_jp_guycody -> econv
        regrade; presplit 12
        render_farewell -> convert_pan_v --camera-from; insert_hold (CD6)
    us: convert_opening(us_esnap, us_ending_shots) -> econv; regrade; presplit 32
        render_farewell -> convert_pan_v --camera-from; insert_hold (CD6)

Presplit counts are sized to the head stretch the anchors apply (the entry
fade needs ~64 CPS frames of event boundaries: jp stretch x5.5 -> 12
slices, us x2.07 -> 32).  Counts of 8/5 under these anchors would give a
12-frame fade instead of 64.

    pipeline.py {jp,us} [--skip-convs] [--root DIR]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACK = HERE.parent
VENV = sys.executable          # stages run under the interpreter that launched us

# Entry-fade lead slices, derived against the head-delay anchors.  The
# builder spreads the head delay D over n slices, so each lead event is
# (D + n)/n CPS frames; asking for `steps` steps across the efade-frame fade
# window means
# n = D / (efade/steps - 1)
# At the shipped efade = 64 and the 8 steps in game:
# us D=228 -> 32.6, which is the 32 committed (the model reproduces it, which
# is why US is left alone); jp D=256 -> 36.6 -> 37.  A jp value of 12 would
# give 3 steps of 22 frames -- below the builder's own >= 4 warning.
PRESPLIT = {"jp": 37, "us": 32}
# THE FAREWELL IS VM-EXECUTED, LIKE EVERY OTHER SCENE.
# The frames come from the script player's own ending run (the third scene,
# reachable once MAIN1.BIN is resident at 0x207800) and the pan's scroll ramp
# is READ FROM THE DISC, not correlated: `camera.tsv` is the VM's own plane-B
# vscroll, unwrapped.  Nothing here is fitted against the disc-composed
# reference.
#
# The US set needs no lip-sync warp.  Such a warp (1128 -> 1342 frames)
# compensates for a source whose pan runs at the wrong rate; the VM's pan is
# the game's own, so the scene needs only a lead-in HOLD to sit at the right
# place against the spliced arcade track.
#
# Windows are the scene's own extent -- first full-bright frame (the head is
# a fade the ENGINE draws, and baking its levels costs 3623 tiles against
# 722) through to the shipped slot length.  The tail past that is the END
# card settling, and including it generated seven eighths of the refresh
# churn.
#
# Verified against THE DISC, per CELL, over all 362 events, by replaying the
# emitted conv offline (tools/render_pan_offline.py -- plane deltas, the
# per-event palette block, and the OBJ patch layer with its real gates):
# jp 0.00 and us 0.00 cells over 20 error, worst single residual 15.4 in
# both regions.  Per-cell is the metric that matters -- a
# miscoloured 16x16 cell is 0.36% of a frame, invisible in a frame mean.
# Spot-checked in game with EM_MAGIC=CAFC: scene head 0
# cells over 20, END card reveal 0 at every step, and the scene-end sprite
# flash gone.
# THE LEAD-IN HOLD IS NOW DERIVED FROM THE DISC.
# The hold sets WHEN the scene's content plays against the CD track.  The
# track is anchored to the ending INIT (CAFD) and the scene start (CAFC) is
# fixed by the sequencer, so a voice event sits at a fixed scene-relative
# frame WHATEVER the hold is -- the hold moves only the picture.  That makes
# the right value measurable rather than tuned:
#
# * the farewell VO transcribed from the disc's own audio, the method that
# produced the reunion word tables (transcribe_ending_vo.py);
# * the VM's CD timecode clock (cd_frames, +1.2516617/vblank after the
# part-entry hold) converts any disc frame to a track position.
#
# us   disc f5660 = 94.79 s;  first word "How" 99.06 s  -> 255 f apart
# in game that word lands at f5115, so content starts f4860 -> 94
# jp   disc f7180 = 120.18 s; first word "なぜ" 121.22 s ->  62 f apart
# in game that word lands at f6263, so content starts f6201 -> 145
#
# TUNED BY EAR THESE COME OUT ~8-14 FRAMES LATER (us 102, jp 159 approved),
# and that gap is the PREVIEW HARNESS, not the scene: ffight{us,jp}01_preview
# .lua says in its own header that afplay startup adds ~0.1 s and it "is not
# a mastering reference".  The shipped pack has no such latency, so the
# disc-derived numbers are the ones to carry.
#
# THE SCENE FILLS A FIXED SLOT, and that -- not a rule about where removed
# frames go -- is what sets the tail.  The invariant is
#
# hold + window + tail == slot
#
# so the tail is COMPUTED, never tuned.  Two hand-set numbers under a rule
# that the frames taken off the lead-in go to the tail would fit jp (ear
# 159 - derived 145 = 14) but not us, whose 97 has nothing to do with its
# 8-frame lead-in change.
#
# Why each slot is what it is:
# jp 1310  the scene's own length; the lead-in came off the front and went
# straight back on the tail, so the slot never moved.
# us 1342  inherited from the anchor era.  The US anchors were a lip-sync
# warp that stretched 1128 -> 1342 frames; removed the
# warp but the slot after it is what shipped and was verified in
# game, so the scene still has to fill 1342.
#
# The tail costs nothing at the back because the closing fade is already a
# frozen hold.
CD6 = {
    "jp": dict(window="7180-8330", anchors=None, hold=(145, 0), slot=1310),
    "us": dict(window="5660-6810", anchors=None, hold=(94, 0), slot=1342),
}


def cd6_tail(c6: dict) -> int:
    """slot - (window + hold): the padding that makes the scene fill its slot."""
    lo, hi = (int(v) for v in c6["window"].split("-"))
    n = hi - lo + 1
    tail = c6["slot"] - n - c6["hold"][0]
    if tail < 0:
        raise SystemExit(
            f"CD6 slot {c6['slot']} is shorter than the scene itself "
            f"({n} frames + {c6['hold'][0]} lead-in): the hold cannot fit.")
    return tail


def run(cmd, env=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    t0 = time.monotonic()
    rc = subprocess.run([str(c) for c in cmd],
                        env=dict(os.environ, **(env or {}))).returncode
    if rc:
        stage_failed(cmd, rc)
    print(f"  [{Path(cmd[1]).name} {time.monotonic() - t0:.1f}s]", flush=True)


def stage_failed(cmd, rc):
    """One readable line instead of a traceback stacked on the stage's own:
    the stage has already printed what went wrong."""
    raise SystemExit(f"stage {Path(str(cmd[1])).name} failed (exit {rc}); "
                     f"its error is printed above")


def spawn(cmd, log: Path, env=None):
    """Start a stage without waiting for it.

    Its output is held in `log` rather than interleaved with the stages
    running alongside it, and replayed in one piece by join(), so a
    parallel build's log still reads in stage order.
    """
    print("+ (parallel)", " ".join(str(c) for c in cmd), flush=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "wb")
    p = subprocess.Popen([str(c) for c in cmd], stdout=fh,
                         stderr=subprocess.STDOUT,
                         env=dict(os.environ, **(env or {})))
    return p, cmd, fh, log, time.monotonic()


def join(handle):
    p, cmd, fh, log, t0 = handle
    rc = p.wait()
    el = time.monotonic() - t0
    fh.close()
    sys.stdout.write(log.read_text(errors="replace"))
    # Wall time, not CPU: it overlapped the stages above, so a small number
    # here means the overlap absorbed it, not that the stage was cheap.
    print(f"  [{Path(cmd[1]).name} {el:.1f}s wall, overlapped]", flush=True)
    sys.stdout.flush()
    if rc:
        stage_failed(cmd, rc)


def tiles(d: Path) -> int:
    return json.loads((d / "manifest.json").read_text())["unique_tiles"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("region", choices=("jp", "us"))
    ap.add_argument("--skip-convs", action="store_true",
                    help="only rebuild the set from the existing convs")
    ap.add_argument("--no-gate", action="store_true",
                    help="skip the Guy/Cody acceptance gate (a development "
                         "check against chain regressions; a reproduction "
                         "build is judged by its output hashes instead)")
    ap.add_argument("--stage0", type=Path, default=None,
                    help="retimed program ROMs (default data/stage0/<region>)")
    ap.add_argument("--parts", type=Path, default=None,
                    help="appended gfx parts (default data/stage1_parts)")
    ap.add_argument("--root", default=None,
                    help="intermediates root (default work/build/vmsweep)")
    ap.add_argument("--gc-phase", default=None,
                    help="jp only: CD_PHASE for the Guy/Cody layer export, "
                         "for building review candidates of that scene")
    ap.add_argument("--gc-frame-off", default=None,
                    help="the FRAME_OFF that belongs with --gc-phase "
                         "(required with it; the phase translates the scene)")
    ap.add_argument("--gc-keystep", default=None,
                    help="jp only: how often the Guy/Cody export samples a "
                         "POSE.  The capture could only manage 10 (its VDP "
                         "dumps were 10 frames apart) and the VM inherited "
                         "that; 2 matches the track's own row cadence and "
                         "removes the pose aliasing for +3 tiles")
    a = ap.parse_args()
    if (a.gc_phase is None) != (a.gc_frame_off is None):
        raise SystemExit("--gc-phase and --gc-frame-off go together")
    r = a.region
    V2 = Path(a.root) if a.root else TRACK / "work/build/vmsweep"
    conv, econv = V2 / f"{r}_conv", V2 / f"{r}_econv"
    e6conv, esnap = V2 / f"{r}_e6conv", V2 / f"{r}_esnap"
    rconv = V2 / f"{r}_rconv"
    for d in (conv, esnap):
        if not (d.exists() and any(d.iterdir())):
            raise SystemExit(f"missing prerequisite: {d}")

    if not a.skip_convs:
        ebase = 0x4000 + tiles(conv)
        # ---- CD-6 farewell pan, STARTED FIRST.
        # The farewell's frames and its CAMERA come from the VM's own ending
        # run -- no disc composer, no correlated scroll.  farewell.py
        # writes both, so the only inputs are the user's disc and the ROM.
        #
        # It takes no input from the ending conv below it, and it is the
        # longest stage in the build, so it runs alongside that conv instead
        # of after it.  What DOES depend on the conv is convert_pan_v's tile
        # base (0x4000 + conv + econv tiles), which is why the join sits
        # immediately above convert_pan_v rather than here.
        c6 = CD6[r]
        e6frames = V2 / f"{r}_e6frames_vm"
        farewell = spawn([VENV, HERE / "ending.py", "farewell",
                          e6frames, r, "--window", c6["window"]],
                         V2 / f"{r}_render_farewell.log")
        try:
            build_ending_conv(a, r, econv, rconv, esnap, ebase, V2)
        except BaseException:
            farewell[0].kill()                # do not orphan it on failure
            farewell[2].close()
            raise
        join(farewell)
        build_farewell_conv(conv, econv, e6conv, e6frames, c6)

    # The two convs are ARGUMENTS, not environment variables: passing them
    # through the environment makes a genuinely stray variable
    # indistinguishable from the driver doing its job.
    run([VENV, HERE / "rom.py", conv,
         a.stage0 or TRACK / f"data/stage0/{r}",
         a.parts or TRACK / "data/stage1_parts",
         V2 / f"{r}_set", r,
         "--ending-conv", econv, "--pan-conv", e6conv])
    print(f"\nset: {V2 / (r + '_set')}")
    return 0


def build_ending_conv(a, r, econv, rconv, esnap, ebase, V2):
    # ---- ending conv (reunion; JP merges the Guy/Cody take-over)
    if r == "jp":
        run([VENV, HERE / "convert.py", esnap,
             TRACK / "data/ending/jp_ending_shots.tsv", rconv,
             "--window", "0-1557", "--crop", "320x224",
             "--base-code", hex(ebase)])
        # JP Guy/Cody pillar-hall scene: LAYERS GENERATED HERE BY THE VM
        # from chunk5's own cody_pan, never read from a capture.
        # Carried as layers, not frames, because composited frames cost
        # 11,377 tiles against 527 for plane B + character cels.
        #
        # NO COMPENSATION TERM.  A "sprites from frame k+1" pairing can be
        # made to byte-match work/capture/jp_guycody_layers, but it has no
        # mechanism -- it is an artefact of the OBJECTIVE.
        # That capture is one emulator session, and matching it byte-for-
        # byte is agreement with one sample, not with hardware.  Scored
        # against what that session's screen ACTUALLY SHOWED
        # (work/capture/jp_clear_live3's displayed frames, per frame,
        # sprites and planes in one image), the VM's own snapshot is the
        # best pairing there is: sprites-from-k+1 scores 56.1% exact and
        # k-1 58.1%, against 70.2% for the plain snapshot, and Guy's box
        # and Cody's box want the SAME shift at every event.  There is no
        # sprite/plane skew to compensate, so none is applied.
        #
        # What remains is the sub-frame CD clock phase, which is a real
        # per-run quantity with no canonical value -- so it is not fitted
        # to a recording, it is reviewed.  --gc-phase/--gc-frame-off build
        # A/B candidates of this scene alone; the default is the player's
        # own committed clock.
        gclayers = V2 / f"{r}_gclayers"
        cmd = [VENV, HERE / "ending.py", "layers", gclayers, r]
        if a.gc_keystep is not None:
            cmd += ["5600", "6540", "2", a.gc_keystep]
        if a.gc_phase is not None:
            cmd += ["--phase", a.gc_phase, "--frame-off", a.gc_frame_off]
        run(cmd)
        run([VENV, HERE / "ending.py", "merge", gclayers, rconv, econv,
             esnap])
        # GATE THE SCENE.  Dropping --gc-keystep regresses this scene to the
        # aliased poses, and a validator that needs two layer sets cannot
        # catch it -- a build has one.  --self needs no reference: it
        # measures the export's cel-change quantum (aliasing) and cross-checks
        # gcspr's SPREVT rows against gc_first_evt/gc_n_evt (the walker index
        # a plain comparison mode never looks at).
        if not a.no_gate and (HERE / "validate_guycody.py").exists():
            run([VENV, HERE / "validate_guycody.py",
                 "--self", gclayers, econv])
    else:
        run([VENV, HERE / "convert.py", esnap,
             TRACK / "data/ending/us_ending_shots.tsv", econv,
             "--window", "0-1512", "--crop", "320x224",
             "--base-code", hex(ebase)])
    run([VENV, HERE / "retime.py", "regrade", econv])
    run([VENV, HERE / "retime.py", "presplit", econv, PRESPLIT[r]])


def build_farewell_conv(conv, econv, e6conv, e6frames, c6):
    # ---- CD-6 farewell pan (DISC-COMPOSED).
    # farewell.py has already run, alongside the ending conv.
    # ---- CHECK the window against the disc instead of only
    # asserting it in a comment.  CD6's window is documented as "first
    # full-bright frame ... through to the shipped slot length", but
    # that rule has never been run.  Run it and SAY when it disagrees.
    # Measured: jp cannot test it (its render is already cropped at the
    # window start), and us gives 5656 against the committed 5660 -- a real
    # 4-frame discrepancy.
    # This prints; it deliberately does NOT change the window, because
    # the committed values are what shipped and were verified in game.
    try:
        import numpy as _np
        from PIL import Image as _Im
        _w0 = int(c6["window"].split("-")[0])
        _fs = sorted(int(f.stem[1:]) for f in e6frames.glob("f*.png"))
        _first = next((n for n in _fs
                       if int(_np.asarray(_Im.open(e6frames / f"f{n:06d}.png")
                                          .convert("RGB"))[32:160].max()) >= 250),
                      None)
        if _first is not None and _first != _w0:
            print(f"  NOTE window start {_w0} but first full-bright frame "
                  f"is {_first} ({_first - _w0:+d}); the committed value is "
                  f"what shipped")
        elif _first is not None:
            print(f"  window start {_w0} == first full-bright frame")
    except Exception as _e:                 # a check must never break a build
        print(f"  (window check skipped: {_e})")
    pbase = 0x4000 + tiles(conv) + tiles(econv)
    cmd = [VENV, HERE / "convert_pan.py", e6frames, e6conv,
           "--window", c6["window"], "--base-code", hex(pbase),
           "--camera-from", str(e6frames / "camera.tsv")]
    if c6["anchors"]:
        cmd += ["--anchors", c6["anchors"]]
    run(cmd)
    if c6["hold"]:
        cmd = [VENV, HERE / "retime.py", "hold", e6conv, c6["hold"][0],
               c6["hold"][1]]
        _tail = cd6_tail(c6)
        if _tail:
            cmd.append(f"--tail={_tail}")
        run(cmd)
    run([VENV, HERE / "retime.py", "regrade", e6conv])


if __name__ == "__main__":
    raise SystemExit(main())
