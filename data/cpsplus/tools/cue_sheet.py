#!/usr/bin/env python3
"""Emit a per-game TESTING CUE SHEET: what is arranged, what deliberately is
not, and where in play to hear each one.

Written because hardware testing was guesswork -- "it seemed like more songs
should have been arranged" is impossible to act on without knowing what the
pack actually claims to cover.  This reads the built pack (the ground truth
that ships) rather than any manifest, so it cannot drift from reality.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pack.format import _parse_header, TriggerRow, PackReader   # noqa: E402

MANIFESTS = Path(__file__).resolve().parent.parent / "manifests"

# manifest -> (cmd column, label column) for the human-readable cue names
LABELS = {
    "sf2_arrange":   ("sf2_arrange_trigger_map.tsv", 0, 3),
    "ffight_arrange": ("ffight_arrange_trigger_map.tsv", 0, 4),
    # the region packs share one trigger map -- both play the opening on 0x52
    # and gate only that cue, so the labels apply verbatim.  Missing since the
    # region split, which left the JP testing sheet with no cue names at all.
    "ffight_arrange_jp": ("ffight_arrange_trigger_map.tsv", 0, 4),
    "forgottn_arrange": ("forgottn_arrange_trigger_map.tsv", 0, 4),
    "mtwins_arrange": ("mtwins_arrange_trigger_map.tsv", 0, 4),
    "ssf2_arrange":  ("ssf2_arrange_trigger_map.tsv", 0, 2),
    "ssf2t_arrange": ("ssf2t_arrange_trigger_map.tsv", 0, 2),
}


def load_labels(pack_stem: str) -> dict[int, str]:
    ent = LABELS.get(pack_stem)
    if not ent:
        return {}
    path, ccol, lcol = ent
    out = {}
    f = MANIFESTS / path
    if not f.exists():
        return {}
    for line in f.read_text().splitlines():
        if not line.startswith("0x"):
            continue
        p = line.split("\t")
        if len(p) <= max(ccol, lcol):
            continue
        try:
            out[int(p[ccol], 16)] = p[lcol].strip()
        except ValueError:
            pass
    # extension cues live in their own manifest (cmd col 0, label col 1) --
    # e.g. ffight's voiced cutscene rows for the ffightcd backport
    ext = MANIFESTS / f"{pack_stem.split('_')[0]}_voiced_cues.tsv"
    if ext.exists():
        for line in ext.read_text().splitlines():
            if not line.startswith("0x"):
                continue
            p = line.split("\t")
            if len(p) > 1:
                try:
                    out[int(p[0], 16)] = p[1].strip()
                except ValueError:
                    pass
    return out


def sheet(pack: Path) -> str:
    d = pack.read_bytes()
    h = _parse_header(d)
    rd = PackReader(pack)
    labels = load_labels(pack.stem)
    rows = []
    for i in range(h.trigger_rows):
        b = d[h.trigger_offset + 4*i: h.trigger_offset + 4*i + 4]
        if len(b) < 4 or b[0] == 0:
            continue
        r = TriggerRow.unpack(bytes(b))
        if r.verb != 1:
            # not a PLAY row (2 = stop): no track/duration to report, and
            # showing track 0's length here would read as a mapped song
            rows.append((i, f"(verb {r.verb}: stop)", -1, 0.0, False, r.gain))
            continue
        m = rd.tracks[r.track]
        # CODEC_ADX is 0, so `if m.codec` is False for every ADX track -- that
        # truthiness test silently zeroed every duration on the first run.
        from pack.format import CODEC_ADX
        if m.codec == CODEC_ADX:
            unit = 18 * m.channels
            dur = (m.data_length // unit * 32) / m.sample_rate
        else:
            dur = m.data_length / (2 * m.channels * m.sample_rate)
        loops = m.loop_end_byte > 0
        rows.append((i, labels.get(i, ""), r.track, dur, loops, r.gain))
    # merge playthrough evidence when a capture exists: a cue that never fires
    # in a full game is not "missing" on hardware, it is unreachable in that run
    # (nine of SF2's rows are OTHER characters' endings -- one per playthrough).
    seen = {}
    cap = MANIFESTS.parent / "work/capture"
    for name in (f"{pack.stem.split('_')[0]}_playthrough.tsv",
                 "sf2ce_playthrough.tsv" if pack.stem.startswith("sf2") else ""):
        f = cap / name if name else None
        if f and f.exists():
            import csv as _csv
            for row in _csv.DictReader(open(f), delimiter="\t"):
                try:
                    c = int(row["cmd"], 16)
                except (KeyError, ValueError):
                    continue
                seen.setdefault(c, float(row["sec"]))
            break

    out = [f"# {pack.stem} -- testing cue sheet",
           f"#   {len(rows)} arranged cues, {h.track_count} tracks, "
           f"gain {'/'.join(sorted({hex(r[5]) for r in rows}))} (0x7f = unity)",
           "#",
           "# If a cue below plays the ORIGINAL music on hardware, that is a defect.",
           "# Anything NOT listed here is expected to play the original -- unmapped",
           "# commands fail open by design.",
           "",
           f"  {'cmd':<6} {'cue':<32} {'trk':>4} {'len':>7}  loop  seen in play"]
    for cmd, lbl, trk, dur, loops, _ in rows:
        when = f"yes @{seen[cmd]:.0f}s" if cmd in seen else ("-" if seen else "")
        if trk < 0:   # non-PLAY verb (stop): silences the arranged track
            out.append(f"  0x{cmd:02x}   {lbl[:32]:<32} {'-':>4} {'-':>7}  stop  {when}")
            continue
        out.append(f"  0x{cmd:02x}   {lbl[:32]:<32} {trk:>4} {dur:>6.1f}s  "
                   f"{'loop' if loops else 'once'}  {when}")
    if seen:
        out += ["",
                "# 'seen in play' comes from a full automated playthrough.  A '-' means",
                "# that run never reached the cue -- other characters' endings, continue,",
                "# ranking -- NOT that it is unmapped.  Those still need their own path."]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packs", nargs="+", type=Path)
    ap.add_argument("--out-dir", type=Path)
    a = ap.parse_args(argv)
    for p in a.packs:
        s = sheet(p)
        if a.out_dir:
            a.out_dir.mkdir(parents=True, exist_ok=True)
            (a.out_dir / f"{p.stem}.txt").write_text(s)
        else:
            print(s)
    if a.out_dir:
        print(f"  wrote {len(a.packs)} cue sheets to {a.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
