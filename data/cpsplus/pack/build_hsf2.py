"""`build_pack.py hsf2` — fully automatic HSF2 AE pack from the PS2 disc.

Recipe (internal research notes):
  * boot ELF (from SYSTEM.CNF) carries the dispatch table at file offset
    0x63a0f0: 8-byte rows {u32 type; u8 afs_entry; u8 vol; u16 param},
    indexed by the arcade QSound command, cmd < 0x500.  Type 1 = BGM; the
    soundtrack bank adds +0x000 (Arrange) / +0x300 (CPS2) / +0x400 (CPS1)
    to the row index and re-reads entry/vol from the banked row.
  * HSF2.AFS holds all music as CRI ADX v4 stereo 48 kHz with sample-exact
    header loop points.  Blank entries ("___blank___.adx", 1 s silence) are
    the family's stop-to-silence idiom -> emitted as `stop` trigger rows.
  * cmd 0x00 is a no-op (never a music start in-game) -> left verb=none.

Generated map is cross-checked against manifests/hsf2_bgm_command_map.tsv.
"""
from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path

from . import protocols
from .afs import AfsArchive
from .build_common import (adx_entry_to_track, crosscheck_pack, MANIFESTS,
                           PACKS_DIR)
from .format import PackWriter, TriggerRow, VERB_PLAY, VERB_STOP
from .isofs import IsoFS
from .sources import resolve_image

# Measured loudness match (EBU R128 vs the native chip music,
# tools/loudness_match.py).  Shipped packs carried this in their trigger rows
# via a post-build set_gain.py patch, which a from-source rebuild silently
# discarded; baked here so rebuilds reproduce the shipped packs byte-exactly.
TRIG_GAIN = {"arrange": 0x2d, "cps1": 0x25}   # per bank; unmeasured banks keep the AFS vol

# "HERE COMES A NEW CHALLENGER".  The PS2 dispatch table types 0x38 as BGM, so
# the generated table would claim it -- but on the ARCADE board (1.06b as well
# as the standalone 1.04 driver) the Z80 plays this cue OVER the running BGM
# and then hands the channels back, which no pack verb can express.  Claiming
# it replaced the music with a 2.35 s one-shot and left the game silent until
# the next command (28.6 s on the measured P2-joins-at-select route).  It
# therefore fails open to the board, like the native QSound logo at 0x3d.
# Evidence and method: manifests/ssf2_arrange_trigger_map.tsv.
NATIVE_RESTORE_CMDS = frozenset({0x38})


def _boot_elf_name(iso: IsoFS) -> str:
    cnf = iso.read_file("/SYSTEM.CNF").decode("ascii", "replace")
    m = re.search(r"BOOT2\s*=\s*cdrom0:\\([^;\s]+)", cnf)
    if not m:
        raise ValueError("cannot parse SYSTEM.CNF BOOT2")
    return m.group(1)


def _load_manifest_map(bank: str) -> dict[int, tuple[int, int, str]]:
    """cmd -> (afs_entry, vol, in_game) for the chosen bank, from the TSV."""
    path = MANIFESTS / "hsf2_bgm_command_map.tsv"
    if not path.exists():
        return {}
    want_set = {"arrange": "ARRANGE", "cps2": "CPS2", "cps1": "CPS1"}[bank]
    out = {}
    rows = path.read_text(encoding="utf-8").splitlines()
    hdr = rows[0].split("\t")
    for line in rows[1:]:
        f = dict(zip(hdr, line.split("\t")))
        if f["set"] != want_set:
            continue
        out[int(f["cmd"], 16)] = (int(f["afs_entry"]), int(f["vol"], 16),
                                  f["in_game"])
    return out


def build(iso_path: str, out: str | None = None, bank: str = "arrange",
          table_offset: int | None = None, crosscheck: bool = True) -> Path:
    if bank not in protocols.HSF2_BANKS:
        raise ValueError(f"bank must be one of {list(protocols.HSF2_BANKS)}")
    bank_off = protocols.HSF2_BANKS[bank]
    table_offset = table_offset or protocols.HSF2_TABLE_OFFSET

    img = resolve_image(iso_path, member_hint=".iso")
    iso = IsoFS(img)
    elf_name = _boot_elf_name(iso)
    elf = iso.read_file("/" + elf_name)
    md5 = hashlib.md5(elf).hexdigest()
    if md5 != protocols.HSF2_ELF_MD5:
        # non-JP disc: region ELFs relocate the (byte-identical) table, so
        # locate it by signature; the manifest cross-check below still
        # hard-gates the result.  EU verified (+0x1200).
        first = elf.find(protocols.HSF2_TABLE_NEEDLE)
        hits = ([] if first < 0 else
                [first] if elf.find(protocols.HSF2_TABLE_NEEDLE,
                                    first + 1) < 0 else [first, -1])
        if len(hits) == 1 and table_offset == protocols.HSF2_TABLE_OFFSET:
            table_offset = hits[0]
            print(f"[hsf2] boot ELF {elf_name} (md5 {md5}) is not the "
                  f"verified JP disc; dispatch table located by signature "
                  f"at 0x{table_offset:x}")
        elif not hits:
            raise SystemExit(
                f"{elf_name}: dispatch-table signature not found in this "
                f"region's ELF -- pass --table-offset (see the docstring)")
    table = elf[table_offset:table_offset + protocols.HSF2_TABLE_ROWS * 8]

    from .build_common import iso_find_basename
    afs_path, afs_lba, afs_size = iso_find_basename(iso, protocols.HSF2_AFS_NAME)
    afs = AfsArchive(iso.f, iso.byte_offset(afs_lba))

    proto = protocols.get_protocol("hsf2")
    w = PackWriter(proto, title=f"HSF2 AE {bank} soundtrack (PS2 SLPM-65496)")

    def row(idx: int) -> tuple[int, int, int, int]:
        t, = struct.unpack_from("<I", table, idx * 8)
        entry = table[idx * 8 + 4]
        vol = table[idx * 8 + 5]
        param, = struct.unpack_from("<H", table, idx * 8 + 6)
        return t & 0x7f, entry, vol, param

    track_of_entry: dict[int, int] = {}
    track_sources: dict[int, int] = {}
    generated: dict[int, tuple[int, int]] = {}   # cmd -> (entry, vol)
    n_play = n_stop = 0
    # Command space is < 0x300: rows 0x300..0x4ff are the CPS2/CPS1 BANK ROW
    # area the dispatcher indexes at cmd+0x300/cmd+0x400, not commands
    # (music commands top out at 0xe4; 0x1xx/0x2xx are SE/voice types 3-6).
    for cmd in range(1, min(0x300, protocols.HSF2_TABLE_ROWS - bank_off)):
        typ, _, _, _ = row(cmd)
        if typ != protocols.HSF2_TYPE_BGM:
            continue
        if cmd in NATIVE_RESTORE_CMDS:
            continue
        _, entry, vol, _ = row(cmd + bank_off)
        generated[cmd] = (entry, vol)
        name = afs.name(entry)
        if name.startswith("___blank"):
            w.set_trigger(cmd, TriggerRow(
                verb=VERB_STOP, gain=TRIG_GAIN.get(bank, 0x7f), suppress=1))
            n_stop += 1
            continue
        if entry not in track_of_entry:
            raw = afs.read(entry)
            stream, meta, _ = adx_entry_to_track(
                raw, name=name, source=f"HSF2.AFS#{entry}", gain=0x7f)
            ti = w.add_track(stream, meta)
            track_of_entry[entry] = ti
            track_sources[ti] = entry
        w.set_trigger(cmd, TriggerRow(
            verb=VERB_PLAY, track=track_of_entry[entry],
            gain=TRIG_GAIN.get(bank, min(vol, 0x7f)), suppress=1))
        n_play += 1

    # cross-check the generated map against the tracked manifest
    manifest = _load_manifest_map(bank)
    mismatches = []
    unexpected = [c for c in generated if manifest and c not in manifest]
    if unexpected:
        raise AssertionError(
            f"ELF table yields commands absent from the verified manifest: "
            + " ".join(f"0x{c:03x}" for c in unexpected[:10]))
    for cmd, (entry, vol) in generated.items():
        if cmd in manifest and manifest[cmd][:2] != (entry, vol):
            mismatches.append((cmd, (entry, vol), manifest[cmd][:2]))
    # cmd 0x00 is deliberately skipped (no-op guard); manifest rows the ELF
    # types as non-BGM (e.g. native QSound-logo command 0x3d) are expected to
    # be absent and therefore fail open to the arcade sound hardware.
    missing_ingame = [c for c, (_, _, ig) in manifest.items()
                      if c not in generated and c != 0 and ig == "yes"
                      and c not in NATIVE_RESTORE_CMDS]
    missing_other = [c for c, (_, _, ig) in manifest.items()
                     if c not in generated and c != 0
                     and (ig != "yes" or c in NATIVE_RESTORE_CMDS)]
    if mismatches or missing_ingame:
        raise AssertionError(
            f"ELF table does not match hsf2_bgm_command_map.tsv: "
            f"mismatches={mismatches[:5]} missing={missing_ingame[:5]}")
    if missing_other:
        print(f"[hsf2:{bank}] note: manifest rows not typed BGM by the ELF "
              f"(native/sound-test-only slots), skipped: "
              + " ".join(f"0x{c:02x}" for c in missing_other))

    out_path = Path(out) if out else PACKS_DIR / f"hsf2_{bank}.cpk"
    w.write(out_path)
    from .format import PackReader, VERB_NONE
    rd = PackReader(out_path)
    try:
        for cmd in sorted(NATIVE_RESTORE_CMDS):
            trg = rd.triggers[cmd]
            if trg.verb != VERB_NONE or trg.suppress:
                raise ValueError(f"readback: command 0x{cmd:02x} must pass "
                                 "through to native QSound")
    finally:
        rd.close()
    size = out_path.stat().st_size
    print(f"[hsf2:{bank}] {out_path}  {size / 1e6:.1f} MB, "
          f"{len(w.tracks)} tracks, {n_play} play + {n_stop} stop triggers "
          f"(manifest cross-check OK: {len(manifest)} rows)")

    if crosscheck:
        res = crosscheck_pack(out_path, afs, track_sources)
        print(f"[hsf2:{bank}] byte-exact vs HSF2.AFS: "
              f"{res['byte_exact_tracks']}/{len(track_sources)} tracks; "
              f"decode-exact: {res['decode_exact_tracks']} tracks")
    iso.close()
    return out_path
