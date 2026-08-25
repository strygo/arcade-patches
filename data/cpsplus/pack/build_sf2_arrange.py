"""`build_pack.py cps1-sf2` — SF2 World Warrior (CPS1) ARRANGE pack.

Builds `sf2_arrange.cpk`: the console-arranged SF2 soundtrack (HSF2 AE ARRANGE
bank) re-keyed onto the CPS1 `sf2` (World Warrior) byte-latch stage commands.

Like `build_pack.py ssf2-arrange`, this is a RE-KEY, not a new arrangement: the
arrange ADX + Capcom header loop points are reused byte-exact via the SAME
extraction path as `build_pack.py hsf2 --bank arrange`
(build_common.adx_entry_to_track).  Only the trigger keying changes, and here
the target is the CPS1 byte-latch dialect rather than the CPS2 QSound record:

  * The CPS1 `sf2` 68K writes a single command byte to the Z80 latch
    ($800181); the pack uses a 256-row (8-bit-keyed) trigger table
    (protocols.SF2_TRIGGER_ROWS).
  * sf2's own stage-music table (68K $6378) is keyed by CHARACTER; the HSF2 AE
    arrange bank is keyed by HSF2's own command numbers.  The mapping is a
    small static re-key (NOT identity — Ryu is 0x01 on sf2 but 0x02 on HSF2,
    Ken 0x04 vs 0x01, ...), verified in protocols.SF2_STAGE_MUSIC /
    SF2_ATTRACT_MUSIC and internal research notes.

The join chain is: sf2_cmd --(SF2_STAGE_MUSIC/SF2_ATTRACT_MUSIC)--> HSF2 arrange
cmd --(hsf2_bgm_command_map.tsv, set=ARRANGE)--> afs_entry --> arrange ADX track.

The tracked trigger map (manifests/sf2_arrange_trigger_map.tsv) is emitted from
protocols.py + the HSF2 map and is the human-reviewable source of truth; the
builder reads it, cross-checks it against the protocols.py-derived mapping (so a
stale/edited map is caught), extracts the arrange ADX, and writes play triggers.
The pack rebuilds byte-identical from that tracked map + the HSF2 AE disc.

Run it through the uniform entry point (defaults resolve to the tracked
manifest + HSF2 disc):
  build_pack.py sf2-arrange \
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
TRIG_GAIN = 0x63   # same measured value for sf2 / sf2ce / sf2hf

HSF2_AFS_NAME = protocols.HSF2_AFS_NAME
HSF2_MAP = MANIFESTS / "hsf2_bgm_command_map.tsv"
DEFAULT_MAP = MANIFESTS / "sf2_arrange_trigger_map.tsv"

# Presentation order + per-row metadata for the emitted trigger map.  The
# mapping values themselves come from protocols.py — this table only fixes the
# row ORDER, the human-readable sf2-side character/screen label, and the
# confidence tier (grounded in internal research notes).
#   group "stage": base-8 character stages — join doc "CONFIRMED".
#   group "boss":  boss stages — medium confidence (attract never reaches a
#                  boss stage; the sf2 boss-stage -> theme pairing needs a
#                  driven route / ear check).
#   group "bonus": bonus stage — join doc lists 0x0d -> fm_3b.
#   group "attract": title/select/ranking — snapshot-verified anchors.
_ROW_ORDER: list[tuple[int, str, str, str]] = [
    # sf2_cmd, character/screen label, confidence, group
    (0x01, "Ryu", "high", "stage"),
    (0x02, "E.Honda", "high", "stage"),
    (0x03, "Blanka", "high", "stage"),
    (0x04, "Ken", "high", "stage"),
    (0x05, "Guile", "high", "stage"),
    (0x06, "Chun-Li", "high", "stage"),
    (0x07, "Zangief", "high", "stage"),
    (0x08, "Dhalsim", "high", "stage"),
    (0x09, "Boss:Balrog(boxer)", "high", "boss"),
    (0x0a, "Boss:Vega(claw)", "high", "boss"),
    (0x0b, "Boss:Sagat", "high", "boss"),
    (0x0c, "Boss:M.Bison(dictator)", "high", "boss"),
    (0x0d, "Bonus", "high", "bonus"),
    (0x16, "Title/Opening", "high", "attract"),
    (0x0e, "Player-Select", "high", "attract"),
    (0x0f, "VS screen", "high", "attract"),
    (0x14, "Ranking", "high", "attract"),
    (0x11, "Continue", "high", "continue"),
    (0x18, "Ending:Ryu", "high", "ending"),
    (0x19, "Ending:E.Honda", "high", "ending"),
    (0x1a, "Ending:Blanka", "high", "ending"),
    (0x1b, "Ending:Guile", "high", "ending"),
    (0x1c, "Ending:Ken", "high", "ending"),
    (0x1d, "Ending:Chun-Li", "high", "ending"),
    (0x1e, "Ending:Zangief", "high", "ending"),
    (0x1f, "Ending:Dhalsim", "high", "ending"),
    (0x34, "Ending:Ken(2)", "high", "ending"),
    (0x35, "Ending:Chun-Li(2)", "high", "ending"),
    (0x79, "HurryUp:Ryu", "high", "hurry"),
    (0x7a, "HurryUp:E.Honda", "high", "hurry"),
    (0x7b, "HurryUp:Blanka", "high", "hurry"),
    (0x7c, "HurryUp:Guile", "high", "hurry"),
    (0x7d, "HurryUp:Ken", "high", "hurry"),
    (0x7e, "HurryUp:Chun-Li", "high", "hurry"),
    (0x7f, "HurryUp:Zangief", "high", "hurry"),
    (0x80, "HurryUp:Dhalsim", "high", "hurry"),
    (0x81, "HurryUp:M.Bison", "high", "hurry"),
    (0x82, "HurryUp:Balrog", "high", "hurry"),
    (0x83, "HurryUp:Sagat", "high", "hurry"),
    (0x84, "HurryUp:Vega", "high", "hurry"),
    (0x8d, "Credits Roll", "high", "credits"),
]

# Which protocols.py table each sf2 command is keyed from.
_SF2_CMD_TO_HSF2 = {**protocols.SF2_STAGE_MUSIC, **protocols.SF2_ATTRACT_MUSIC,
                    **protocols.SF2_ENDING_MUSIC, **protocols.SF2_HURRY_MUSIC,
                    **protocols.SF2_CONTINUE_MUSIC, **protocols.SF2_CREDITS_MUSIC}


def _arrange_bank() -> dict[int, tuple[int, str, int]]:
    """HSF2 arrange cmd -> (afs_entry, arrange track name, authored volume).

    Reads hsf2_bgm_command_map.tsv (set=ARRANGE) — the same byte-exact source
    build_hsf2 --bank arrange consumes."""
    out: dict[int, tuple[int, str, int]] = {}
    rows = HSF2_MAP.read_text().splitlines()
    hdr = rows[0].split("\t")
    for line in rows[1:]:
        d = dict(zip(hdr, line.split("\t")))
        if d.get("set") == "ARRANGE":
            out[int(d["cmd"], 0)] = (int(d["afs_entry"]),
                                     d.get("name", ""), int(d["vol"], 16))
    return out


def canonical_rows() -> list[dict]:
    """Derive the trigger map from protocols.py + the HSF2 arrange bank.

    Each row: sf2_cmd, hsf2_cmd, arrange_entry, character, confidence, vol,
    arrange_name.  Raises if a mapped HSF2 command has no ARRANGE-bank entry."""
    bank = _arrange_bank()
    rows = []
    for sf2_cmd, char, conf, group in _ROW_ORDER:
        hsf2_cmd = _SF2_CMD_TO_HSF2[sf2_cmd]
        if hsf2_cmd not in bank:
            raise KeyError(f"HSF2 arrange cmd 0x{hsf2_cmd:02x} "
                           f"(sf2 0x{sf2_cmd:02x}) not in {HSF2_MAP.name}")
        entry, name, vol = bank[hsf2_cmd]
        note = {
            "stage": f"sf2 {char} stage -> HSF2 {name}",
            "boss": f"sf2 boss stage -> HSF2 {name} (sound-test NCC + nameplate verified)",
            "bonus": f"sf2 bonus stage -> HSF2 {name}",
            "attract": f"sf2 {char.lower()} -> HSF2 {name}",
            "ending": f"sf2 ending -> HSF2 {name}; align_ncc vs the album's "
                      f"CPS1/FM bank, ear-confirmed where it floored",
            "hurry": f"sf2 time-low variant -> HSF2 {name}; bijection over the "
                     f"twelve (2) tracks, found only after skipping the 0x50 loop",
            "continue": f"sf2 continue -> HSF2 {name}",
            "credits": f"CE credits -> HSF2 {name}; found by playthrough (the "
                       f"driver is stateful -- cold-boot 0x8d plays other music)",
        }[group]
        rows.append(dict(sf2_cmd=sf2_cmd, hsf2_cmd=hsf2_cmd,
                         arrange_entry=entry, character=char,
                         confidence=conf, vol=vol, arrange_name=name,
                         note=note))
    return rows


_MAP_HEADER = "cmd\tarrange_entry\thsf2_cmd\tcharacter\tconfidence\tnote"


def emit_map(path: Path) -> Path:
    """Write the tracked trigger map from the protocols.py-derived rows."""
    lines = [
        "# SF2 World Warrior (CPS1 `sf2`) ARRANGE trigger map: sf2 byte-latch",
        "#   stage command -> HSF2 AE ARRANGE-bank ADX entry (the console-",
        "#   arranged SF2 soundtrack).  RE-KEY of the HSF2 arrange ADX",
        "#   (manifests/hsf2_bgm_command_map.tsv, set=ARRANGE), byte-exact +",
        "#   Capcom header loops — NOT a new arrangement.",
        "#",
        "# JOIN: sf2_cmd --(protocols.SF2_STAGE_MUSIC / SF2_ATTRACT_MUSIC)-->",
        "#   HSF2 arrange cmd --(hsf2 map, set=ARRANGE)--> arrange_entry.  The",
        "#   sf2 stage table is keyed by CHARACTER, HSF2 by its own command",
        "#   numbers, so the re-key is NOT identity (Ryu 0x01 vs HSF2 0x02,",
        "#   Ken 0x04 vs 0x01, ...).  Ground truth:",
        "#   internal research notes + manifests/protocol/sf2.json.",
        "#",
        "# CONFIDENCE: every row is high.  Base-8 stages confirmed;",
        "#   title/select/ranking snapshot-verified; bosses (0x09-0x0c) were",
        "#   SCRAMBLED by a US/JP naming assumption and re-keyed,",
        "#   then ear-verified.  Endings (0x18-0x1f, 0x34, 0x35) and the twelve",
        "#   time-low variants (0x79-0x84) were joined with tools/align_ncc.py",
        "#   against the album's CPS1/FM bank -- same soundtrack the board plays,",
        "#   so a true pair correlates where an ARRANGED cover does not.",
        "#",
        "# The matcher certifies its POSITIVES only.  Both Chun-Li endings floor",
        "#   against the very tracks they match (0x1d at 0.089, 0x35 at 0.123)",
        "#   and were confirmed by ear; every other ending lands 0.38-0.52.",
        "#   Never read a floor here as 'not in the album'.",
        "#",
        "# NOT mapped, on purpose: 0x38 is garbage data; 0x50 and 0xde are",
        "#   crowd-cheer LOOPS that ignore the 0xf7 stop (which is why 164",
        "#   commands were unmeasurable until sweeps started skipping them);",
        "#   0x8d-0xab are a shadow bank replaying c-0x8c; 0x8c is SF2CE's",
        "#   four-boss ending, which the arrange album has no counterpart for.",
        "#",
        "# Emitted by pack/build_sf2_arrange.py from protocols.py + the HSF2 map;",
        "#   the builder cross-checks this file against that derivation, so it",
        "#   is regenerated byte-identical.  Cols: cmd, arrange_entry, hsf2_cmd,",
        "#   character, confidence, note.",
        "#",
        _MAP_HEADER,
    ]
    for r in canonical_rows():
        lines.append("\t".join((
            f"0x{r['sf2_cmd']:02x}", str(r["arrange_entry"]),
            f"0x{r['hsf2_cmd']:02x}", r["character"], r["confidence"],
            r["note"])))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def read_map(path: Path) -> list[dict]:
    """Parse the tracked trigger map (cmd, arrange_entry, hsf2_cmd, ...)."""
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        f = line.split("\t")
        if f[0].strip().lower() == "cmd":
            continue
        rows.append(dict(
            sf2_cmd=int(f[0], 0), arrange_entry=int(f[1]),
            hsf2_cmd=int(f[2], 0) if len(f) > 2 and f[2] else None,
            character=f[3] if len(f) > 3 else "",
            confidence=f[4] if len(f) > 4 else "",
            note=f[5] if len(f) > 5 else ""))
    return rows


def build(iso_path: str, map_path: str | None = None,
          out: str | None = None, crosscheck: bool = True) -> Path:
    map_file = Path(map_path) if map_path else DEFAULT_MAP
    canon = canonical_rows()
    # The tracked map is the read source; emit it if missing, and always
    # cross-check it against the protocols.py-derived mapping so a stale or
    # hand-edited file cannot silently change the pack.
    if not map_file.exists():
        emit_map(map_file)
        print(f"[cps1-sf2] emitted trigger map -> {map_file}")
    trig = read_map(map_file)
    canon_key = [(r["sf2_cmd"], r["hsf2_cmd"], r["arrange_entry"])
                 for r in canon]
    read_key = [(r["sf2_cmd"], r["hsf2_cmd"], r["arrange_entry"])
                for r in trig]
    if read_key != canon_key:
        raise SystemExit(
            f"{map_file} does not match the protocols.py-derived mapping "
            f"(regenerate with --emit-map).\n  tracked: {read_key}\n"
            f"  derived: {canon_key}")

    vols = {r["arrange_entry"]: r["vol"] for r in canon}
    names = {r["arrange_entry"]: r["arrange_name"] for r in canon}

    img = resolve_image(iso_path, member_hint=".iso")
    iso = IsoFS(img)
    from .build_common import iso_find_basename
    _, lba, _ = iso_find_basename(iso, HSF2_AFS_NAME)
    afs = AfsArchive(iso.f, iso.byte_offset(lba))

    proto = protocols.get_protocol("sf2")
    w = PackWriter(proto, trigger_rows=protocols.SF2_TRIGGER_ROWS,
                   title="SF2 World Warrior arranged soundtrack (HSF2 AE "
                         "arrange bank, byte-exact + Capcom header loops)")

    track_of_entry: dict[int, int] = {}
    track_sources: dict[int, int] = {}
    built = []
    for r in trig:
        entry = r["arrange_entry"]
        if entry not in track_of_entry:
            raw = afs.read(entry)
            label = names.get(entry, "").replace(" ", "_").lower() or "arr"
            gain = min(vols.get(entry, 0x7f), 0x7f)
            stream, meta, _ = adx_entry_to_track(
                raw, name=f"{label}_e{entry}", source=f"HSF2.AFS#{entry}",
                gain=gain)
            ti = w.add_track(stream, meta)
            track_of_entry[entry] = ti
            track_sources[ti] = entry
        ti = track_of_entry[entry]
        w.set_trigger(r["sf2_cmd"], TriggerRow(verb=VERB_PLAY, track=ti,
                                               gain=TRIG_GAIN, suppress=1))
        built.append(r)

    out_path = Path(out) if out else PACKS_DIR / "sf2_arrange.cpk"
    w.write(out_path)
    size = out_path.stat().st_size
    print(f"[cps1-sf2] {out_path}  {size / 1e6:.1f} MB, "
          f"{len(w.tracks)} tracks, {len(built)} play triggers "
          f"(sf2 byte-latch stage commands -> HSF2 arrange entries)")
    print(f"[cps1-sf2] sf2_cmd  hsf2_cmd  arrange_entry  character       conf")
    for r in built:
        print(f"    0x{r['sf2_cmd']:02x}     0x{r['hsf2_cmd']:02x}      "
              f"e{r['arrange_entry']:<3}          {r['character']:<15} "
              f"{r['confidence']}")

    if crosscheck:
        res = crosscheck_pack(out_path, afs, track_sources)
        print(f"[cps1-sf2] byte-exact vs HSF2.AFS: "
              f"{res['byte_exact_tracks']}/{len(track_sources)} tracks; "
              f"decode-exact: {res['decode_exact_tracks']} tracks")
    iso.close()
    return out_path


if __name__ == "__main__":
    REPO = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(REPO / "cpsplus"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--iso", required=True,
                    help="HSF2 AE PS2 disc (zip or extracted iso); see "
                         "your wrapper script for the owner's paths")
    ap.add_argument("--map")
    ap.add_argument("--out")
    ap.add_argument("--emit-map", action="store_true",
                    help="(re)write the tracked trigger map and exit")
    ap.add_argument("--no-crosscheck", action="store_true")
    a = ap.parse_args()
    if a.emit_map:
        emit_map(Path(a.map) if a.map else DEFAULT_MAP)
        print(f"emitted {a.map or DEFAULT_MAP}")
        sys.exit(0)
    build(a.iso, map_path=a.map, out=a.out, crosscheck=not a.no_crosscheck)
