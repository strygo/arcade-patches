#!/usr/bin/env python3
"""Build `sfa2_snes.cpk` — a DRAFT CPS+ novelty pack that plays arcade Street
Fighter Alpha 2 (`sfa2`) with the SNES Street Fighter Zero 2 soundtrack.

Source: SF Alpha Anthology Y_DATA entries e440..e459 (SNES SFZ2 renders, human
ear-confirmed by prior research; 48 kHz stereo ADX). 19 of the 20 entries carry
Capcom-authored ADX HEADER loop points (loop_flag=1) — used byte-exact, exactly
like build_zero1's in-game path. e459 (Australia, 48.3 s, loop_flag=0) gets a
best-effort authored loop.

Trigger keying: the pack matches the arcade sfa2 16-bit QSound command
(cmd_hi@0x618001 = 0x00, cmd_lo@0x618003 = sound code). Sound code N -> command
0x00NN. The arcade sound-test code == the in-game QSound command (CPS2 sound
test writes the identical latch record — verified for the sgemf capture and
consistent with the sfa2 in-game latch trace). The arcade command -> character
mapping is GROUND-TRUTH, not chroma: arcade stage command = FAQ CPS2 order
position 0x01-0x13 (anchored by the in-game-validated 0x07=Birdie), joined to
the SNES entries through the human-verified +1-corrected entry->character map;
four clips (0x01/07/12/13) were ear-confirmed. See
manifests/sfa2_snes_trigger_map.tsv (map) + manifests/sfa2_snes_loops.tsv (loops).

Run it through the uniform entry point (defaults resolve to the tracked
manifests):
  build_pack.py sfa2-snes \
      --iso "roms/ps2/Street Fighter Alpha Anthology.iso" \
      [--map manifests/sfa2_snes_trigger_map.tsv --loops manifests/sfa2_snes_loops.tsv]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]      # the tree holding pack/
sys.path.insert(0, str(REPO / "cpsplus"))

from pack import adxcodec, protocols               # noqa: E402
from pack.afs import AfsArchive                     # noqa: E402
from pack.build_common import PACKS_DIR, adx_entry_to_track    # noqa: E402
from pack.format import (PackWriter, TrackMeta, TriggerRow,   # noqa: E402
                         CODEC_ADX, VERB_PLAY)
from pack.isofs import IsoFS                         # noqa: E402
from pack.sources import resolve_image              # noqa: E402

# Measured loudness match (EBU R128 vs the native chip music,
# tools/loudness_match.py).  Shipped packs carried this in their trigger rows
# via a post-build set_gain.py patch, which a from-source rebuild silently
# discarded; baked here so rebuilds reproduce the shipped packs byte-exactly.
TRIG_GAIN = 0x1c



def _round32(v: int) -> int:
    return (v + 16) // 32 * 32


def _authored_loop_track(raw: bytes, ls: int, le: int, *, name: str,
                         source: str, gain: int = 0x7f):
    """Byte-exact hard-cut loop from frame-aligned sample points (for entries
    with no ADX header loop, e.g. e459)."""
    info = adxcodec.parse_header(raw)
    ch = info.channels
    ls, le = _round32(ls), _round32(le)
    ls_b = adxcodec.samples_to_stream_byte(ls, ch)
    le_b = adxcodec.samples_to_stream_byte(le, ch)
    stream = raw[info.data_offset:info.data_offset + le_b]
    if len(stream) != le_b:
        raise ValueError(f"{name}: source shorter than authored loop_end")
    c1, c2 = adxcodec.calc_coeffs(info.cutoff or adxcodec.DEFAULT_CUTOFF,
                                  info.sample_rate)
    meta = TrackMeta(sample_rate=info.sample_rate, channels=ch, codec=CODEC_ADX,
                     gain=gain, coef1=c1, coef2=c2, name=name, source=source,
                     loop_start_sample=ls, loop_start_byte=ls_b,
                     loop_end_sample=le, loop_end_byte=le_b)
    return stream, meta


def _read_map(path: Path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        cmd, entry, char, conf = int(f[0], 0), int(f[1]), f[2], f[3]
        note = f[4] if len(f) > 4 else ""
        rows.append((cmd, entry, char, conf, note))
    return rows


def _read_loops(path: Path):
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("entry"):
            continue
        f = line.split("\t")
        out[int(f[0])] = dict(ls=int(f[1]), le=int(f[2]), source=f[3])
    return out


def build(map_path: str, loops_path: str, iso_path: str, out: str | None):
    trig_map = _read_map(Path(map_path))
    loops = _read_loops(Path(loops_path))

    img = resolve_image(iso_path)
    iso = IsoFS(img)
    _, lba, _, _ = iso.find("/Y_DATA.BIN")
    afs = AfsArchive(iso.f, iso.byte_offset(lba))

    proto = protocols.get_protocol("sfa2")          # generic Anthology-family
    w = PackWriter(proto, title="SFA2 + SNES SFZ2 soundtrack (DRAFT novelty)")

    track_of_entry: dict[int, int] = {}
    built = []
    for cmd, entry, char, conf, note in trig_map:
        if entry not in track_of_entry:
            raw = afs.read(entry)
            lp = loops.get(entry)
            name = f"snes_{char}_e{entry}"
            src = f"Y_DATA.BIN#{entry}"
            if lp and lp["source"] == "adx_header":
                stream, meta, _ = adx_entry_to_track(
                    raw, name=name, source=src, gain=0x7f)
            elif lp and lp["source"] == "whole_track":
                stream, meta, _ = adx_entry_to_track(
                    raw, name=name, source=src, gain=0x7f, force_loop=True)
            elif lp:
                stream, meta = _authored_loop_track(
                    raw, lp["ls"], lp["le"], name=name, source=src)
            else:                                    # no loop info -> whole loop
                stream, meta, _ = adx_entry_to_track(
                    raw, name=name, source=src, gain=0x7f, force_loop=True)
            track_of_entry[entry] = w.add_track(stream, meta)
        ti = track_of_entry[entry]
        w.set_trigger(cmd, TriggerRow(verb=VERB_PLAY, track=ti,
                                      gain=TRIG_GAIN, suppress=1))
        built.append((cmd, entry, char, conf))

    # PACKS_DIR, like every other builder -- never the package source dir.
    out_path = Path(out) if out else PACKS_DIR / "sfa2_snes.cpk"
    w.write(out_path)
    iso.close()

    size = out_path.stat().st_size
    print(f"[sfa2-snes] {out_path}  {size/1e6:.1f} MB, {len(w.tracks)} tracks, "
          f"{len(built)} play triggers (keyed by arcade sfa2 QSound command)")
    print("[sfa2-snes] arcade_cmd  snes_entry  character   confidence")
    for cmd, entry, char, conf in built:
        print(f"    0x{cmd:04x}     e{entry}     {char:<10} {conf}")
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--map",
                    default=str(REPO / "manifests" / "sfa2_snes_trigger_map.tsv"))
    ap.add_argument("--loops",
                    default=str(REPO / "manifests" / "sfa2_snes_loops.tsv"))
    ap.add_argument("--iso", required=True,
                    help="SF Alpha Anthology PS2 iso")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    build(a.map, a.loops, a.iso, a.out)


if __name__ == "__main__":
    main()
