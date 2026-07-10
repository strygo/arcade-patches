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
                out += parse_hex_blob(node.text)
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


def make_patch_mra(base_text: str, *, name: str, setname: str, patched_rom: bytes,
                   runs, note: str) -> str:
    """Derive a patch-overlay MRA from an official base MRA."""
    text = base_text
    text = re.sub(r"<name>.*?</name>", f"<name>{name}</name>", text, count=1)
    text = re.sub(r"<setname>.*?</setname>", f"<setname>{setname}</setname>", text, count=1)
    # The declared assembly hash must describe the patched output.
    new_md5 = hashlib.md5(patched_rom).hexdigest()
    text = re.sub(r'asm_md5="[0-9a-f]+"', f'asm_md5="{new_md5}"', text, count=1)
    text = text.replace("<misterromdescription>", f"<!--\n{note}\n-->\n<misterromdescription>", 1)
    text = text.replace("    </rom>", _patch_elements(patched_rom, runs) + "\n    </rom>", 1)
    return text
