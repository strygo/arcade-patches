#!/usr/bin/env python3
"""Extract the arcade attract-story caption system (Stage 3 caption work).

Reverse-engineered layout, from the ffight program image:
  - $283A enqueues a text-job id; consumer at $4B5A dispatches on id>>8
    via the long-pointer table at $4B6C.
  - group 0xA (ids 0xA00+): instant draws; string table at 0x67082
    (word offsets), entries = segments of [u16 cell][u16 attr] then
    glyph words (tile = 0x4400+g) until 0 (end) / bit15 (new segment).
  - group 0x1 (ids 0x100+): typewriter task at $1380; string table at
    0x68566, entry = [u8 delay][u8 mode] then segments as above; mode
    selects single-height or double-height (kana: second row = glyph
    +0x10, except the sokuon/dash exceptions at $141C).
  - map/phone/TV/Cody lines are fired via $1796A with d1 = line index;
    per-region d1 -> id-low-byte tables at 0x17986 (US/World) and
    0x179A1 (Japan).
  - cell offset -> scroll1 position: cell/0x80 = column, (cell%0x80)/4
    = row (scroll1 cells are 4 bytes, column stride 0x80).
  - attr low 5 bits = palette.

Outputs data/captions/rom_captions_<region>.tsv with one row per line:
  scene, d1, id, delay, mode, segments (col,row,pal,glyph-codes,ascii)

Run under the repo venv:
    venv/bin/python captions.py <outdir>
"""
from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from ffcd import romset

_here = Path(__file__).resolve()
REPO = _here.parents[3] if len(_here.parents) > 3 else _here.parents[-1]
TRACK = Path(__file__).resolve().parents[1]
ROMSET = REPO / "roms" / "mame0260"
_cfg = TRACK / "paths.json"
if _cfg.exists():
    import json as _json
    ROMSET = Path(_json.loads(_cfg.read_text()).get("romset", ROMSET))

GROUP1_TAB = 0x68566
GROUPA_TAB = 0x67082
D1_TABLES = {"us": 0x17986, "jp": 0x179A1}
# d1 -> scene, from the entry-1..4 handler inventory
D1_SCENES = ([("map", d) for d in range(4)] +
             [("phone", d) for d in range(4, 10)] +
             [("tv", d) for d in range(10, 16)] +
             [("cody", d) for d in range(16, 20)])


def build_image(region: str) -> bytes:
    img = bytearray(0x100000)
    with tempfile.TemporaryDirectory() as td:
        romset.extract(ROMSET, ("ffight",),
                       ["ff_36.11f", "ff_42.11h", "ff_37.12f", "ff-32m.8h"], td)
        stem, member = (("ffightu", "ffu_43.12h") if region == "us"
                        else ("ffightj", "ff43.bin"))
        romset.extract(ROMSET, (stem, "ffight"), [member], td)
        img[0:0x40000:2] = Path(td, "ff_36.11f").read_bytes()
        img[1:0x40000:2] = Path(td, "ff_42.11h").read_bytes()
        img[0x40000:0x80000:2] = Path(td, "ff_37.12f").read_bytes()
        img[0x40001:0x80000:2] = Path(td, member).read_bytes()
        img[0x80000:0x100000] = Path(td, "ff-32m.8h").read_bytes()
    return bytes(img)


def read_segments(img: bytes, p: int):
    segs = []
    while True:
        cell, attr = struct.unpack(">HH", img[p:p + 4])
        p += 4
        glyphs = []
        while True:
            g = struct.unpack(">H", img[p:p + 2])[0]
            p += 2
            if g == 0 or g & 0x8000:
                break
            glyphs.append(g)
            assert len(glyphs) < 128, "runaway string"
        segs.append((cell, attr, glyphs))
        if g == 0:
            return segs, p


def decode_group1(img: bytes, idx: int):
    off = struct.unpack(">H", img[GROUP1_TAB + idx * 2:
                                  GROUP1_TAB + idx * 2 + 2])[0]
    p = GROUP1_TAB + off
    delay, mode = img[p], img[p + 1]
    segs, _ = read_segments(img, p + 2)
    return delay, mode, segs


def decode_groupA(img: bytes, idx: int):
    off = struct.unpack(">H", img[GROUPA_TAB + idx * 2:
                                  GROUPA_TAB + idx * 2 + 2])[0]
    segs, _ = read_segments(img, GROUPA_TAB + off)
    return segs


def seg_text(glyphs) -> str:
    return "".join(chr(g) if 0x20 <= g < 0x7F else f"[{g:03x}]"
                   for g in glyphs)


def fmt_segs(segs) -> str:
    out = []
    for cell, attr, glyphs in segs:
        col, row = cell // 0x80, (cell % 0x80) // 4
        out.append(f"col={col} row={row} pal={attr & 0x1F}"
                   f" attr={attr:04x} |{seg_text(glyphs)}|")
    return " || ".join(out)


def main() -> int:
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    for region in ("us", "jp"):
        img = build_image(region)
        rows = ["# scene\td1\tid\tdelay\tmode\tsegments"]
        d1tab = img[D1_TABLES[region]:D1_TABLES[region] + 0x1B]
        for scene, d1 in D1_SCENES:
            idx = d1tab[d1]
            delay, mode, segs = decode_group1(img, idx)
            rows.append(f"{scene}\t{d1}\t{0x100 + idx:#x}\t{delay}\t{mode}"
                        f"\t{fmt_segs(segs)}")
        for name, idx in (("1990s-line", 8 if region == "us" else 9),
                          ("tv-extra-a01", 1)):
            segs = decode_groupA(img, idx)
            rows.append(f"{name}\t-\t{0xA00 + idx:#x}\t-\t-\t{fmt_segs(segs)}")
        out = outdir / f"rom_captions_{region}.tsv"
        out.write_text("\n".join(rows) + "\n")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
