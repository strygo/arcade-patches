"""Reading the Final Fight CD disc.

Two layers, together because neither is useful without the other:

  * the bundle CODEC -- the Sub-CPU decompressor, ported from PRG-RAM
    0x9A6C, that turns a stored chunk into tiles, nametables or code;
  * the bundle IO -- locating OPENING.BIN / ENDING.BIN in the ISO and
    handing back their decompressed chunks per region.

Ported from the Sub-CPU routine at PRG-RAM 0x9A6C (found by trapping
reads of a chunk's source region).  Chunk payload header:

    +0  u32  uncompressed size
    +4  u8   method  (0 = none, 1 = row-delta, 2 = LZSS)
    +5       body

Method 1 (0x9A86) -- 4-byte-stride row delta, natural for 4bpp MD tiles where
one 8-pixel row is 4 bytes.  Groups of 8 output bytes: a flag byte, then per
bit MSB-first, 1 = literal, 0 = copy the byte 4 back.  Flag byte 0 is the
shortcut "repeat the previous longword twice".

Method 2 (0x9B0E) -- LZSS.  Flag byte, 8 bits MSB-first: 1 = literal byte;
0 = two big-endian bytes v, offset = v >> 4 (12 bits), length = (v & 0xF) + 3,
copied from output[-offset].
"""
from __future__ import annotations
import struct


def unpack(buf: bytes, pos: int = 0) -> tuple[bytes, int, int]:
    """-> (data, method, consumed).  Raises on short/над-run streams."""
    size, = struct.unpack_from(">I", buf, pos)
    method = buf[pos + 4]
    i = pos + 5
    out = bytearray()
    if method == 0:
        out += buf[i:i + size]
        return bytes(out), 0, 5 + size

    if method == 1:
        groups = size >> 3
        for _ in range(groups):
            flag = buf[i]; i += 1
            if flag == 0:
                prev = out[-4:]
                out += prev; out += prev
                continue
            for b in range(8):
                if (flag << b) & 0x80:
                    out.append(buf[i]); i += 1
                else:
                    out.append(out[-4])
        return bytes(out), 1, i - pos

    if method == 2:
        remaining = size
        while remaining > 0:
            flag = buf[i]; i += 1
            for b in range(8):
                if remaining <= 0:
                    break
                if (flag << b) & 0x80:
                    out.append(buf[i]); i += 1
                    remaining -= 1
                else:
                    v = (buf[i] << 8) | buf[i + 1]; i += 2
                    off = v >> 4
                    ln = (v & 0xF) + 3
                    if off == 0 or off > len(out):
                        raise ValueError(f"bad match off={off} at out={len(out)}")
                    src = len(out) - off
                    for k in range(ln):
                        out.append(out[src + k])
                    remaining -= ln
        return bytes(out), 2, i - pos

    raise ValueError(f"unknown method {method}")


def chunk_table(bundle: bytes, load: int = 0x02A800):
    """-> [(src, dest, file_off)] from the bundle's zero-terminated table."""
    out, i = [], 0
    while i + 8 <= len(bundle):
        src, dest = struct.unpack_from(">II", bundle, i)
        if src == 0 and dest == 0:
            break
        out.append((src, dest, src - load))
        i += 8
    return out


import struct, collections
from pathlib import Path

SEC, USER, HDR = 2352, 2048, 16
# Located relative to this file rather than to one machine.  ffcd/ sits at
# <repo>/backports/ffightcd/ffcd, so TRACK is the backport and REPO the
# capcom tree; the US image is the on-demand extraction of the multi-track
# .7z rip, which is why it lives under work/ and the JP .img does not.
TRACK = Path(__file__).resolve().parents[1]
REPO = TRACK.parents[1] if len(TRACK.parents) > 1 else TRACK.parents[-1]
JP_IMG = str(REPO / "roms/segacd/Final Fight CD (JP).img")
US_IMG = str(TRACK / "work/build/disc/us/Final Fight CD (USA) (Track 01).bin")

# A caller that keeps its discs elsewhere -- the reconstruction kit, whose
# user has them wherever they have them -- writes paths.json beside this
# tree rather than editing the paths above.  Stage processes are separate
# interpreters, so a module-level assignment would not reach them; a file
# does.  Absent, the tree-relative defaults stand.
_cfg = TRACK / "paths.json"
if _cfg.exists():
    import json as _json
    _d = _json.loads(_cfg.read_text())
    JP_IMG = _d.get("jp", JP_IMG)
    US_IMG = _d.get("us", US_IMG)
# (lba, length) per bundle, per region
EXTENTS = {
    "jp": {"O": (3713, 303631), "E": (8801, 251516)},
    "us": {"O": (3713, 298822), "E": (8801, 254646)},
}

def read_iso_file(img, lba, length):
    out = bytearray(); n = (length + USER - 1) // USER
    with open(img, "rb") as f:
        for i in range(n):
            f.seek((lba + i) * SEC + HDR); out += f.read(USER)
    return bytes(out[:length])

def load_chunks(bundle_bytes):
    """chunk index -> decompressed bytes (MD 4bpp tiles / nametables / code)."""
    ch = {}
    for k, (src, dest, fo) in enumerate(chunk_table(bundle_bytes)):
        if not (0 <= fo < len(bundle_bytes)): continue
        try: ch[k] = unpack(bundle_bytes, fo)[0]
        except Exception: pass
    return ch

_cache = {}
def region_chunks(region):
    """{'O': chunks, 'E': chunks} for 'jp' or 'us' (cached)."""
    if region not in _cache:
        img = JP_IMG if region == "jp" else US_IMG
        _cache[region] = {b: load_chunks(read_iso_file(img, *EXTENTS[region][b]))
                          for b in ("O", "E")}
    return _cache[region]

def detect_palettes(buf):
    """Embedded MD CRAM 16-color blocks -> [(offset, [16 words])]."""
    out = []; off = 0
    while off < len(buf) - 32:
        w = [struct.unpack_from(">H", buf, off + 2 * i)[0] for i in range(16)]
        if (all((x & 0xF111) == 0 for x in w)
                and len([x for x in w if x]) >= 6 and len(set(w)) >= 8):
            out.append((off, w)); off += 32
        else:
            off += 2
    return out
