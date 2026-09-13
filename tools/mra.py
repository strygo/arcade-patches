"""MiSTer MRA assembly and patch-overlay generation.

The assembler is a faithful re-implementation of Main_MiSTer's MRA ROM
assembly (support/arcade/mra_loader.cpp): parts and interleaves stream into
one buffer, and <patch offset="..."> data is copied into the assembled image
(offsets therefore include the MRA config header and everything before the
target bytes). It reproduces the asm_md5 values published in jotego's jtbin
MRAs bit-for-bit, which is how the build verifies itself.

Patch-overlay MRAs reference the ORIGINAL romset and carry the translation as
inline <patch> elements, so no ROM data is distributed and nothing on the
user's card is modified.
"""

import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from io import StringIO

# Merge diff runs separated by fewer than this many identical bytes.
JOIN_GAP = 8
# Cap single <patch> elements to keep them conventional.
MAX_PATCH_BYTES = 4096


def parse_hex_blob(text: str) -> bytes:
    return bytes(int(t, 16) for t in text.split())


def _map_offsets(imap: int, unitlen: int):
    """Port of the byte-lane computation in mra_loader.cpp rom_data()."""
    idx = 0
    reg = imap
    for _ in range(unitlen):
        if reg & 0xF:
            break
        reg >>= 4
        idx += 1
    offsets = []
    reg = imap
    first = True
    gaps = 0
    for _ in range(unitlen):
        if reg & 0xF:
            offsets.append(idx + (reg & 0xF) - 1 + gaps)
            first = False
        elif not first:
            gaps += 1
        reg >>= 4
    return offsets


class ZipSource:
    """Zip member lookup across one or more set zips (first hit wins)."""

    def __init__(self, zip_paths, check_crc=True):
        self.entries = {}
        self.crcs = {}
        for zp in zip_paths:
            with zipfile.ZipFile(zp) as z:
                for info in z.infolist():
                    if info.filename not in self.entries:
                        self.entries[info.filename] = z.read(info.filename)
                        self.crcs[info.filename] = info.CRC
        self.check_crc = check_crc

    def read_part(self, part) -> bytes:
        name = part.get("name")
        if name not in self.entries:
            raise KeyError(f"romset is missing {name}")
        data = self.entries[name]
        if self.check_crc and part.get("crc"):
            want = int(part.get("crc"), 16)
            if self.crcs[name] != want:
                raise ValueError(f"{name}: crc {self.crcs[name]:08x} != expected {want:08x}")
        start = int(part.get("offset", "0"), 0)
        if start:
            data = data[start:]
        if part.get("length"):
            data = data[: int(part.get("length"), 0)]
        return data


def assemble(mra_text: str, source: ZipSource) -> bytes:
    """Assemble ROM index 0 of an MRA exactly as Main_MiSTer does."""
    root = ET.parse(StringIO(mra_text)).getroot()
    rom = next(r for r in root.iter("rom") if r.get("index") == "0")
    out = bytearray()
    for node in rom:
        if node.tag == "part":
            if node.get("name"):
                out += source.read_part(node)
            else:
                # Inline data honours repeat="N" (mra_loader.cpp sends the
                # blob N times), which is how fillers are written compactly.
                out += parse_hex_blob(node.text) * int(node.get("repeat", "1"), 0)
        elif node.tag == "interleave":
            unitlen = int(node.get("output", "8")) // int(node.get("input", "8"))
            block_start = len(out)
            for part in node:
                if part.tag != "part":
                    continue
                data = source.read_part(part)
                offsets = _map_offsets(int(part.get("map"), 16), unitlen)
                groups = len(data) // len(offsets)
                need = block_start + groups * unitlen
                if need > len(out):
                    out += bytes(need - len(out))
                pos = 0
                for g in range(groups):
                    base = block_start + g * unitlen
                    for off in offsets:
                        out[base + off] = data[pos]
                        pos += 1
        elif node.tag == "patch":
            offset = int(node.get("offset", "0"), 0)
            data = parse_hex_blob(node.text)
            if offset + len(data) > len(out):
                raise ValueError("patch extends beyond assembled data")
            if (node.get("operation") or "").lower() == "xor":
                for i, b in enumerate(data):
                    out[offset + i] ^= b
            else:
                out[offset : offset + len(data)] = data
    return bytes(out)


def declared_asm_md5(mra_text: str) -> str | None:
    m = re.search(r'asm_md5="([0-9a-f]+)"', mra_text)
    return m.group(1) if m else None


def diff_runs(stock: bytes, patched: bytes):
    if len(stock) != len(patched):
        raise ValueError("assembled images differ in size")
    runs, i, n = [], 0, len(stock)
    while i < n:
        if stock[i] == patched[i]:
            i += 1
            continue
        start = i
        while i < n and stock[i] != patched[i]:
            i += 1
        if runs and start - runs[-1][1] <= JOIN_GAP:
            runs[-1][1] = i
        else:
            runs.append([start, i])
    return runs


def _patch_elements(patched: bytes, runs) -> str:
    out = []
    for start, end in runs:
        for chunk_start in range(start, end, MAX_PATCH_BYTES):
            chunk_end = min(chunk_start + MAX_PATCH_BYTES, end)
            lines = []
            for off in range(chunk_start, chunk_end, 16):
                lines.append(" ".join(f"{b:02X}" for b in patched[off : min(off + 16, chunk_end)]))
            body = "\n            ".join(lines)
            out.append(f'        <patch offset="{chunk_start:#x}">\n            {body}\n        </patch>')
    return "\n".join(out)


# jtcps2's 44-byte config header opens with four big-endian 16-bit region
# starts (audiocpu, qsound, gfx, firmware) in 0x40000 units.
CPS2_REGION_UNIT = 0x40000
FILLER_BYTE = 0xFF


def _shift_cps2_header(text: str, grow: int) -> str:
    """Move every region after maincpu down by `grow` bytes."""
    if grow % CPS2_REGION_UNIT:
        raise ValueError(f"maincpu growth {grow:#x} is not a multiple of {CPS2_REGION_UNIT:#x}")
    m = re.search(r"<rom index=\"0\"[^>]*>.*?<part>(.*?)</part>", text, re.S)
    if not m:
        raise ValueError("base MRA has no inline config header")
    header = bytearray(parse_hex_blob(m.group(1)))
    if len(header) != 44:
        raise ValueError(f"config header is {len(header)} bytes, expected 44")
    for i in range(0, 8, 2):
        start = int.from_bytes(header[i:i + 2], "big") + grow // CPS2_REGION_UNIT
        header[i:i + 2] = start.to_bytes(2, "big")
    rows = [" ".join(f"{b:02X}" for b in header[r:r + 8]) for r in range(0, 44, 8)]
    body = "\n            " + "\n            ".join(rows) + " "
    return text[:m.start(1)] + body + text[m.end(1):]


def insert_program_parts(base_text: str, after: str, parts, placeholder: bool) -> str:
    """Add program ROMs the stock set does not have.

    `parts` is a list of (name, crc32 hex, size).  They are inserted after the
    stock part named `after` (the last maincpu ROM) and the CPS-2 header is
    shifted to match.  With placeholder=True each part becomes an inline
    filler of the same size instead, so an overlay over the STOCK set can
    assemble to the same layout and <patch> the real bytes in."""
    m = re.search(rf"^([ \t]*)<part name=\"{re.escape(after)}\"[^>]*/>[ \t]*\n", base_text, re.M)
    if not m:
        raise ValueError(f"base MRA has no part named {after}")
    indent = m.group(1)
    lines = []
    for name, crc, size in parts:
        if placeholder:
            lines.append(f'{indent}<!-- {name} - {size:#x} bytes, written by the patches below -->\n'
                         f'{indent}<part repeat="{size:#x}"> {FILLER_BYTE:02X} </part>\n')
        else:
            lines.append(f'{indent}<part name="{name}" crc="{crc}"/>\n')
    text = base_text[:m.end()] + "".join(lines) + base_text[m.end():]
    return _shift_cps2_header(text, sum(size for _, _, size in parts))


# Fields MiSTer's Arcade Organizer sorts by.  The jotego bases carry only a
# <region>, and it describes the stock game, so every one of these is set
# from the patch entry.  Order follows the MRA template.
META_FIELDS = ("region", "platform", "category", "catver", "mraauthor")


def set_meta(text: str, meta: dict) -> str:
    """Set the organizer fields, replacing or inserting each after <region>."""
    missing = []
    for field in META_FIELDS:
        value = meta.get(field)
        if value is None:
            continue
        tag = f"<{field}>{value}</{field}>"
        if re.search(rf"<{field}>.*?</{field}>", text):
            text = re.sub(rf"<{field}>.*?</{field}>", tag, text, count=1)
        else:
            missing.append(tag)
    if missing:
        anchor = re.search(r"([ \t]*)<region>.*?</region>\n", text)
        if not anchor:
            raise ValueError("base MRA has no <region> to anchor organizer fields")
        block = "".join(f"{anchor.group(1)}{tag}\n" for tag in missing)
        text = text[:anchor.end()] + block + text[anchor.end():]
    return text


def make_patch_mra(base_text: str, *, name: str, setname: str, patched_rom: bytes,
                   runs, note: str, meta: dict | None = None) -> str:
    """Derive a patch-overlay MRA from an official base MRA."""
    text = base_text
    text = re.sub(r"<name>.*?</name>", f"<name>{name}</name>", text, count=1)
    text = re.sub(r"<setname>.*?</setname>", f"<setname>{setname}</setname>", text, count=1)
    if meta:
        text = set_meta(text, meta)
    # The declared assembly hash must describe the patched output.
    new_md5 = hashlib.md5(patched_rom).hexdigest()
    text = re.sub(r'asm_md5="[0-9a-f]+"', f'asm_md5="{new_md5}"', text, count=1)
    text = text.replace("<misterromdescription>", f"<!--\n{note}\n-->\n<misterromdescription>", 1)
    text = text.replace("    </rom>", _patch_elements(patched_rom, runs) + "\n    </rom>", 1)
    return text
