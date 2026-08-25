"""ISO9660 walker for 2048 B/sector images (PS2 DVD) and raw 2352 B/sector
track bins (Saturn CD, Mode1/Mode2Form1).  Promoted from tools/iso9660.py and
tools/isolist.py.

For 2048 B images, files are contiguous byte ranges: `byte_offset(lba)` gives
direct access (the AFS archives are read in place, not extracted).
"""
from __future__ import annotations

import struct
from pathlib import Path

S2048 = 2048
S2352 = 2352


class IsoFS:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.f = open(self.path, "rb")
        self.sector_size = self._detect()
        self._entries = None

    def _detect(self) -> int:
        self.f.seek(16 * S2048)
        if self.f.read(6)[1:6] == b"CD001":
            return S2048
        self.f.seek(16 * S2352)
        raw = self.f.read(S2352)
        if len(raw) == S2352 and raw[:12] == \
                b"\x00" + b"\xff" * 10 + b"\x00":
            user = raw[16:22] if raw[15] == 1 else raw[24:30]
            if user[1:6] == b"CD001":
                return S2352
        raise ValueError(f"no ISO9660 PVD found in {self.path}")

    # -- sector access ---------------------------------------------------
    def _user_data(self, lba: int) -> bytes:
        if self.sector_size == S2048:
            self.f.seek(lba * S2048)
            return self.f.read(S2048)
        self.f.seek(lba * S2352)
        raw = self.f.read(S2352)
        if len(raw) < S2352:
            return b""
        return raw[16:16 + 2048] if raw[15] == 1 else raw[24:24 + 2048]

    def byte_offset(self, lba: int) -> int:
        """Absolute byte offset of a file extent (2048 B images only)."""
        if self.sector_size != S2048:
            raise ValueError("direct offsets only valid for 2048 B sectors")
        return lba * S2048

    def read_extent(self, lba: int, size: int) -> bytes:
        if self.sector_size == S2048:
            self.f.seek(lba * S2048)
            return self.f.read(size)
        out = bytearray()
        for i in range((size + 2047) // 2048):
            out += self._user_data(lba + i)
        return bytes(out[:size])

    # -- directory tree ---------------------------------------------------
    def _parse_dir(self, lba: int, size: int, prefix: str, out: list,
                   depth: int = 0):
        data = self.read_extent(lba, size)
        pos = 0
        while pos < len(data):
            ln = data[pos]
            if ln == 0:
                pos = (pos // 2048 + 1) * 2048
                continue
            rec = data[pos:pos + ln]
            elba = struct.unpack_from("<I", rec, 2)[0]
            esize = struct.unpack_from("<I", rec, 10)[0]
            isdir = bool(rec[25] & 2)
            nlen = rec[32]
            name = rec[33:33 + nlen].decode("ascii", "replace")
            if name not in ("\x00", "\x01"):
                full = prefix + "/" + name.split(";")[0]
                out.append((full, elba, esize, isdir))
                if isdir and depth < 8:
                    self._parse_dir(elba, esize, full, out, depth + 1)
            pos += ln

    def entries(self) -> list[tuple[str, int, int, bool]]:
        if self._entries is None:
            pvd = self._user_data(16)
            assert pvd[1:6] == b"CD001"
            rlba = struct.unpack_from("<I", pvd, 156 + 2)[0]
            rsize = struct.unpack_from("<I", pvd, 156 + 10)[0]
            self._entries = []
            self._parse_dir(rlba, rsize, "", self._entries)
        return self._entries

    def find(self, path: str) -> tuple[str, int, int, bool]:
        want = path.upper().lstrip("/")
        for e in self.entries():
            if e[0].upper().lstrip("/") == want:
                return e
        raise FileNotFoundError(f"{path} not in {self.path}")

    def read_file(self, path: str) -> bytes:
        full, lba, size, isdir = self.find(path)
        if isdir:
            raise IsADirectoryError(path)
        return self.read_extent(lba, size)

    def close(self):
        self.f.close()
