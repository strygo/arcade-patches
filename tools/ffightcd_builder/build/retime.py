"""Edits applied to a finished conv's script and palette blocks.

Three passes over the same artifacts -- script.bin (the event list the
engine walks) and palblocks.bin (its per-event palettes).  They shape
PRESENTATION, not content: where the scene's fade gets its event
boundaries, how long it holds before playing, and one palette re-grade.

    retime.py presplit <conv> <n>
    retime.py hold <conv> <frames> [after] [--tail=N]
    retime.py regrade <conv> [--factor F] [--dry-run]
"""
from __future__ import annotations
from pathlib import Path
import argparse
import json
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ffcd import cps1 as patchrec  # noqa: E402




def _presplit(a) -> int:
    conv, n = a.conv, a.n
    sp = conv / "script.bin"
    s = sp.read_bytes()
    bi, pb, d0, dcnt, dur, sx, sy = struct.unpack(">HHIHHHH", s[:16])
    assert (bi >> 12) == 0, "event 0 already carries a fade code"
    assert dur > n, f"event 0 is {dur} frames -- already split?"
    recs = [struct.pack(">HHIHHHH", bi, pb, d0, dcnt, 1, sx, sy)]
    for _ in range(n - 1):
        recs.append(struct.pack(">HHIHHHH", bi, pb, 0, 0x8000, 1, sx, sy))
    recs.append(struct.pack(">HHIHHHH", bi, pb, 0, 0x8000, dur - n, sx, sy))
    out = b"".join(recs) + s[16:]
    sp.write_bytes(out)

    man = json.loads((conv / "manifest.json").read_text())
    man["events"] = len(out) // 16
    man["script_bytes"] = len(out)
    # the builder's retime needs to know how many events are the
    # lead run, so it can SPREAD the anchors' head delay across them instead
    # of charging the whole step to the first one (which left the entry fade
    # with no boundaries and stamped zero steps in both regions).
    man["entry_slices"] = n
    if "gc_first_evt" in man:
        man["gc_first_evt"] += n
    (conv / "manifest.json").write_text(json.dumps(man, indent=2))

    with open(conv / "script.tsv", "w") as f:
        f.write("# base_block\tpal_block\tdelta_off\tdcnt_flag\t"
                "frames\tsx\tsy\n")
        for i in range(0, len(out), 16):
            r = struct.unpack(">HHIHHHH", out[i:i + 16])
            f.write("\t".join(str(v) for v in r) + "\n")
    print(f"{conv.name}: event 0 ({dur}f) -> {n} x 1f lead + {dur - n}f; "
          f"{man['events']} events"
          + (f", gc_first_evt {man['gc_first_evt']}"
             if "gc_first_evt" in man else ""))
    return 0



def _hold(a) -> int:
    conv, frames, after, tail = a.conv, a.frames, a.after, a.tail
    p = conv / "script.bin"
    sb = bytearray(p.read_bytes())
    n = len(sb) // 16
    if not 0 <= after < n:
        raise SystemExit(f"event {after} out of range (0..{n-1})")
    ev = bytearray(sb[after * 16:(after + 1) * 16])
    struct.pack_into(">H", ev, 10, frames)   # duration
    struct.pack_into(">H", ev, 8, 0)         # no deltas -> frozen picture
    out = bytearray(sb[:(after + 1) * 16] + ev + sb[(after + 1) * 16:])
    if tail:
        # --tail N: lengthen the LAST event by N frames.
        # The lead-in hold sets WHEN the scene's content plays against the
        # CD track; the slot length is fixed by the sequencer.  Shortening
        # the lead-in to fix lip-sync therefore has to put the frames back
        # somewhere, and the tail is the only place it costs nothing: the
        # closing fade is already held there, so this just
        # holds the same still picture a little longer before the cut.
        n2 = len(out) // 16
        d = struct.unpack_from(">H", out, (n2 - 1) * 16 + 10)[0]
        struct.pack_into(">H", out, (n2 - 1) * 16 + 10, d + tail)
        print(f"{conv.name}: last event {d} -> {d + tail} frames (+{tail} tail)")
    p.write_bytes(bytes(out))
    total = sum(struct.unpack_from(">H", out, i * 16 + 10)[0]
                for i in range(len(out) // 16))
    print(f"{conv.name}: inserted a {frames}-frame freeze after event "
          f"{after}; {n} -> {n+1} events, {total} frames")
    # UPDATE THE MANIFEST.  Rewriting script.bin and patches.bin without
    # updating manifest.json leaves it describing the conv BEFORE the hold --
    # e.g. events=361, total_frames=1151 for a conv that now holds 362 / 1310
    # (jp) or 362 / 1342 (us).  A consumer that trusts a stale manifest gets a
    # gate two events out, so the manifest is rewritten here to match.
    mf = conv / "manifest.json"
    if mf.exists():
        man = json.loads(mf.read_text())
        man["events"] = len(out) // 16
        man["total_frames"] = total
        man["script_bytes"] = len(out)
        man["lead_in_hold"] = frames
        if tail:
            man["tail_pad"] = tail
        mf.write_text(json.dumps(man, indent=2))
        print(f"{conv.name}: manifest updated (events {n} -> {man['events']}, "
              f"frames -> {total})")

    # Sprite patches are GATED ON AN EVENT INDEX (patches.bin word 2 of each
    # 10-byte band/col/gate/code/pal record).  Inserting an event renumbers
    # every event past the insertion point, so a gate left alone fires one
    # event early.  Measured before this ran: the settled-tail patches came
    # up at event 353 while the camera only stopped at 354, and for that one
    # event -- about 10 frames -- they painted settled art over a plane still
    # in motion, spiking the calf column from 2.7 to 10.2 mean error.
    # the record is 12 bytes -- band, col, MIN, MAX, code, pal -- and BOTH
    # gates renumber.  Reading it as a 10-byte layout would walk off
    # alignment and rewrite code/palette words as if they were gates.
    pf = conv / "patches.bin"
    if pf.exists():
        pd = bytearray(pf.read_bytes())
        recs = patchrec.unpack_all(bytes(pd))   # also validates the length
        moved = 0
        for k in range(len(recs)):
            for off in patchrec.gate_offsets(k):
                gate = struct.unpack_from(">H", pd, off)[0]
                if after < gate < patchrec.NEVER:
                    struct.pack_into(">H", pd, off, gate + 1)
                    moved += 1
        pf.write_bytes(bytes(pd))
        print(f"{conv.name}: bumped {moved} patch gate(s) past event {after}")
    return 0


def is_dress_red(r: int, g: int, b: int) -> bool:
    """Red-dominant, and NOT a skin tone.

    Skin in this art sits around r:g:b = 15:10:7 / 12:8:5 -- green is
    roughly two thirds of red.  The dress and roses sit at 12:3:3, 15:5:5,
    10:0:3, 8:3:0 -- green under half of red.  The 0.55 bar separates them
    with margin on both sides.
    """
    return r >= 8 and g <= r * 0.55 and b <= r * 0.60

def _regrade(a) -> int:
    conv, factor, dry = a.conv, a.factor, a.dry_run

    p = conv / "palblocks.bin"
    d = bytearray(p.read_bytes())
    n = len(d) // 2
    words = list(struct.unpack(f">{n}H", bytes(d)))

    changed = {}
    for i, w in enumerate(words):
        r, g, b = (w >> 8) & 15, (w >> 4) & 15, w & 15
        if not is_dress_red(r, g, b):
            continue
        ng, nb = int(round(g * factor)), int(round(b * factor))
        if (ng, nb) == (g, b):
            continue
        nw = (w & 0xF000) | (r << 8) | (ng << 4) | nb
        changed.setdefault((w, nw), 0)
        changed[(w, nw)] += 1
        words[i] = nw

    if not changed:
        print(f"{conv.name}: no dress-red entries matched -- nothing to do")
        return 0
    print(f"{conv.name}: re-graded {sum(changed.values())} palette "
          f"entr{'y' if sum(changed.values()) == 1 else 'ies'} "
          f"(factor {factor}):")
    for (old, new), cnt in sorted(changed.items()):
        print(f"    {old:04x} -> {new:04x}   x{cnt}")
    if dry:
        print("  (dry run, not written)")
        return 0
    p.write_bytes(struct.pack(f">{n}H", *words))
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    sub = ap.add_subparsers(dest="edit", required=True)
    p = sub.add_parser("presplit", help="slice event 0 into 1-frame lead events")
    p.add_argument("conv", type=Path); p.add_argument("n", type=int)
    p = sub.add_parser("hold", help="insert a delta-free HOLD event")
    p.add_argument("conv", type=Path); p.add_argument("frames", type=int)
    p.add_argument("after", type=int, nargs="?", default=0)
    p.add_argument("--tail", type=int, default=0,
                   help="also lengthen the LAST event by N frames")
    p = sub.add_parser("regrade", help="deepen the dress reds")
    p.add_argument("conv", type=Path)
    p.add_argument("--factor", type=float, default=0.6)
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    return {"presplit": _presplit, "hold": _hold, "regrade": _regrade}[a.edit](a)


if __name__ == "__main__":
    raise SystemExit(main())
