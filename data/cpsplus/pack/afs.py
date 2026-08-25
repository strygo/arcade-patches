"""CRI AFS archive walker operating on a region inside a larger file.
Promoted from tools/afs.py (same parsing, importable API)."""
from __future__ import annotations

import struct


class AfsArchive:
    """AFS at `base` inside file object `f` (e.g. an AFS inside an ISO)."""

    def __init__(self, f, base: int):
        self.f = f
        self.base = base
        f.seek(base)
        hdr = f.read(8)
        if hdr[:4] != b"AFS\0":
            raise ValueError(f"not AFS at 0x{base:x}: {hdr!r}")
        self.count = struct.unpack_from("<I", hdr, 4)[0]
        tab = f.read(8 * (self.count + 1))
        self.entries = [struct.unpack_from("<II", tab, 8 * i)
                        for i in range(self.count)]          # (offset, size)
        self.toc = struct.unpack_from("<II", tab, 8 * self.count)
        self.names = self._read_names()

    def _read_names(self):
        tocoff, tocsize = self.toc
        if not tocoff or not tocsize:
            return None
        self.f.seek(self.base + tocoff)
        blk = self.f.read(tocsize)
        names = []
        for i in range(self.count):
            rec = blk[i * 48:(i + 1) * 48]
            names.append(rec[:32].split(b"\0")[0].decode("ascii", "replace"))
        return names

    def name(self, i: int) -> str:
        return self.names[i] if self.names else f"entry_{i:03d}"

    def read(self, i: int, length: int | None = None) -> bytes:
        off, size = self.entries[i]
        self.f.seek(self.base + off)
        return self.f.read(size if length is None else min(length, size))
