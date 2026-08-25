"""`build_pack.py ssf2-arrange` — SSF2 / SSF2T ARRANGE pack from the HSF2 AE disc.

Builds `ssf2_arrange.cpk` / `ssf2t_arrange.cpk`: the console-arranged SF2
soundtrack (HSF2 AE ARRANGE bank) re-keyed onto the STANDALONE Super Street
Fighter II / Super SF2 Turbo arcade QSound stage commands.

This is a RE-KEY, not a new arrangement (internal research notes:
the HSF2 AE arrange bank IS the SSF2/SSF2T arrangement).  The arrange ADX +
Capcom header loop points are reused byte-exact — the SAME extraction path as
`build_pack.py hsf2 --bank arrange` (build_common.adx_entry_to_track) — only
the trigger keying changes:

  * HSF2's own arrange pack keys entry N by HSF2's unified command (0x01=Ken ..
    0x10=Dee-Jay, 0xd5=Gouki).
  * The STANDALONE ssf2/ssf2t QSound driver (Phase-0 banner "1.04 /CPS2 1993")
    uses its own stage commands.  Ground truth (manifests/ssf2*_arrange_trigger_
    map.tsv): a sound-test latch-poke sweep, each per-command clip identified by
    log-RMS envelope NCC against the LABELLED HSF2 CPS2-bank recordings (same
    QSound masters), plus an in-game stage-start anchor.  Result: cmd 0x01..0x10
    are the 16 SF2 characters in exact HSF2 order (so cmd 0xNN -> arrange entry
    NN), and every non-stage command passes through to real QSound.

The trigger map is the tracked, human-reviewable source of truth; this builder
only reads entries from it, extracts the arrange ADX, and writes play triggers.

Run it through the uniform entry point (defaults resolve to the tracked
manifest + HSF2 disc):
  build_pack.py ssf2-arrange --game ssf2t \
      --iso "roms/ps2/Hyper Street Fighter II - The Anniversary Edition (Japan).zip"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import protocols
from .afs import AfsArchive
from .build_common import (adx_entry_to_track, crosscheck_pack, MANIFESTS,
                           PACKS_DIR)
from .format import PackWriter, TriggerRow, VERB_PLAY
from .isofs import IsoFS
from .sources import resolve_image

# Measured loudness match (EBU R128 vs the native chip music,
# tools/loudness_match.py).  Shipped packs carried this in their trigger rows
# via a post-build set_gain.py patch, which a from-source rebuild silently
# discarded; baked here so rebuilds reproduce the shipped packs byte-exactly.
TRIG_GAIN = 0x1d   # same measured value for ssf2 / ssf2t

HSF2_AFS_NAME = protocols.HSF2_AFS_NAME
HSF2_MAP = MANIFESTS / "hsf2_bgm_command_map.tsv"


def _read_trigger_map(path: Path):
    """Rows of (cmd, arrange_entry, character, confidence, note)."""
    rows = []
    for line in path.read_text().splitlines():
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        f = line.split("\t")
        if f[0].strip().lower() == "cmd":          # header row
            continue
        cmd = int(f[0], 0)
        entry = int(f[1])
        char = f[2] if len(f) > 2 else ""
        conf = f[3] if len(f) > 3 else ""
        note = f[4] if len(f) > 4 else ""
        rows.append((cmd, entry, char, conf, note))
    return rows


def _arrange_vols() -> dict[int, int]:
    """HSF2 ARRANGE-bank authored volume per afs_entry (so the pack is
    level-identical to the HSF2 arrange pack; build_hsf2 reads the same byte)."""
    out: dict[int, int] = {}
    if not HSF2_MAP.exists():
        return out
    rows = HSF2_MAP.read_text().splitlines()
    hdr = rows[0].split("\t")
    for line in rows[1:]:
        d = dict(zip(hdr, line.split("\t")))
        if d.get("set") == "ARRANGE":
            out[int(d["afs_entry"])] = int(d["vol"], 16)
    return out


def build(game: str, iso_path: str, map_path: str | None = None,
          out: str | None = None, crosscheck: bool = True) -> Path:
    if game not in ("ssf2", "ssf2t"):
        raise ValueError("game must be 'ssf2' or 'ssf2t'")
    map_file = Path(map_path) if map_path else \
        MANIFESTS / f"{game}_arrange_trigger_map.tsv"
    trig = _read_trigger_map(map_file)
    vols = _arrange_vols()

    img = resolve_image(iso_path, member_hint=".iso")
    iso = IsoFS(img)
    from .build_common import iso_find_basename
    _, lba, _ = iso_find_basename(iso, HSF2_AFS_NAME)
    afs = AfsArchive(iso.f, iso.byte_offset(lba))

    proto = protocols.get_protocol(game)
    w = PackWriter(proto, title=f"{game.upper()} arranged SF2 soundtrack "
                                f"(HSF2 AE arrange bank, byte-exact + Capcom "
                                f"header loops)")

    track_of_entry: dict[int, int] = {}
    track_sources: dict[int, int] = {}
    built = []
    for cmd, entry, char, conf, note in trig:
        if entry not in track_of_entry:
            raw = afs.read(entry)
            name = f"{char}_arr_e{entry}" if char else f"arr_e{entry}"
            gain = min(vols.get(entry, 0x7f), 0x7f)
            stream, meta, _ = adx_entry_to_track(
                raw, name=name, source=f"HSF2.AFS#{entry}", gain=gain)
            ti = w.add_track(stream, meta)
            track_of_entry[entry] = ti
            track_sources[ti] = entry
        ti = track_of_entry[entry]
        w.set_trigger(cmd, TriggerRow(verb=VERB_PLAY, track=ti,
                                      gain=TRIG_GAIN, suppress=1))
        built.append((cmd, entry, char, conf))

    out_path = Path(out) if out else PACKS_DIR / f"{game}_arrange.cpk"
    w.write(out_path)
    size = out_path.stat().st_size
    print(f"[{game}-arrange] {out_path}  {size / 1e6:.1f} MB, "
          f"{len(w.tracks)} tracks, {len(built)} play triggers "
          f"(standalone {game} stage commands -> HSF2 arrange entries)")
    print(f"[{game}-arrange] cmd   arrange_entry  character   confidence")
    for cmd, entry, char, conf in built:
        print(f"    0x{cmd:04x}   e{entry:<3}          {char:<10} {conf}")

    if crosscheck:
        res = crosscheck_pack(out_path, afs, track_sources)
        print(f"[{game}-arrange] byte-exact vs HSF2.AFS: "
              f"{res['byte_exact_tracks']}/{len(track_sources)} tracks; "
              f"decode-exact: {res['decode_exact_tracks']} tracks")
    iso.close()
    return out_path


if __name__ == "__main__":
    REPO = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(REPO / "cpsplus"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True, choices=["ssf2", "ssf2t"])
    ap.add_argument("--iso", required=True,
                    help="HSF2 AE PS2 disc (zip or extracted iso)")
    ap.add_argument("--map")
    ap.add_argument("--out")
    ap.add_argument("--no-crosscheck", action="store_true")
    a = ap.parse_args()
    build(a.game, a.iso, map_path=a.map, out=a.out,
          crosscheck=not a.no_crosscheck)
