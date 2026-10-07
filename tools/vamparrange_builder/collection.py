"""Read what the Arrange kits need from your PS2 Vampire: Darkstalkers Collection
(Japan) disc image: the X_DATA archive's arrange packages, sprite tile blobs and
voice banks.  Every slice is identified by size and MD5 before it is used; this
module holds no game data.
"""
import hashlib
import struct

import bizlz
import cps2tiles
import tilelz

AFS_BASE, AFS_COUNT = 0xE3001800, 483          # X_DATA.BIN inside the ISO
AFS_HEADER_MD5 = "6a69007bcaeadedd808b54eb541e5285"


def md5(b):
    return hashlib.md5(b).hexdigest()


class Disc:
    def __init__(self, iso):
        self.iso = iso
        head = self._read(AFS_BASE, 8 + 8 * AFS_COUNT)
        if head[:4] != b"AFS\0" or md5(head) != AFS_HEADER_MD5:
            raise ValueError("this is not the Vampire: Darkstalkers Collection (Japan) disc image")
        self.entries = [struct.unpack_from("<II", head, 8 + 8 * e) for e in range(AFS_COUNT)]

    def _read(self, offset, size):
        with open(self.iso, "rb") as f:
            f.seek(offset)
            data = f.read(size)
        if len(data) != size:
            raise ValueError("disc image is truncated")
        return data

    def entry(self, e, want_md5=None):
        off, size = self.entries[e]
        data = self._read(AFS_BASE + off, size)
        if want_md5 and md5(data) != want_md5:
            raise ValueError(f"X_DATA entry {e} differs from the expected disc")
        return data

    def package(self, e, want_md5=None):
        """An arrange package: (comp1, comp2) from its BIZ-compressed MWo3 payload."""
        payload, _ = bizlz.decompress(self.entry(e, want_md5), 0, max_out=64 * 1024 * 1024)
        if payload[:4] != b"MWo3":
            raise ValueError(f"X_DATA entry {e} is not an MWo3 package")
        c1, c2 = struct.unpack_from("<II", payload, 0x0C)
        return payload[0x40:0x40 + c1], payload[0x40 + c1:0x40 + c1 + c2]

    def tiles(self, e, want_md5=None):
        """A BIZ sprite tile blob as decoded CPS-2 tiles (128 bytes each)."""
        chunky, used = bizlz.decompress(self.entry(e, want_md5), 0)
        if not chunky or len(chunky) % cps2tiles.TILE:
            raise ValueError(f"X_DATA entry {e} is not a tile blob")
        return cps2tiles.disc_to_planar(chunky)

    def layer(self, e, want_md5=None):
        """A layer tile container (three pointer-table sections; each tile raw or
        tilelz-compressed) as decoded CPS-2 tiles, in section and slot order."""
        blob = self.entry(e, want_md5)
        chunky = bytearray()
        for i in range(3):
            base, count, table = struct.unpack_from("<III", blob, 0x10 * i)
            if not count:
                continue
            ptrs = struct.unpack_from(f"<{count}I", blob, table)
            starts = sorted({p & 0x7FFFFFFF for p in ptrs if p})
            end = {o: (starts[k + 1] if k + 1 < len(starts) else len(blob)) for k, o in enumerate(starts)}
            for p in ptrs:
                if not p:
                    continue
                block = blob[p & 0x7FFFFFFF:end[p & 0x7FFFFFFF]]
                if p >> 31:
                    tile, _ = tilelz.decompress_verbose(block, out_len=cps2tiles.TILE)
                    if tile is None or len(tile) != cps2tiles.TILE:
                        continue
                elif len(block) >= cps2tiles.TILE:
                    tile = block[:cps2tiles.TILE]
                else:
                    continue
                chunky += tile
        return cps2tiles.disc_to_planar(bytes(chunky))

    def vags(self, e, want_md5=None):
        """A voice bank's VAG table: [(rate, adpcm bytes)]."""
        blob = self.entry(e, want_md5)
        if blob[:4] != b"MOMO":
            raise ValueError(f"X_DATA entry {e} is not a voice bank")
        n = struct.unpack_from("<I", blob, 4)[0]
        members = [blob[o:o + s] for o, s in (struct.unpack_from("<II", blob, 8 + 8 * k) for k in range(n))]
        hd, bd = members[0], members[1]
        at = hd.find(b"IECS" + b"igaV")
        if at < 0:
            raise ValueError(f"X_DATA entry {e} has no VAG table")
        count = struct.unpack_from("<I", hd, at + 12)[0] + 1
        rows = [list(struct.unpack_from("<IH", hd, at + struct.unpack_from("<I", hd, at + 16 + 4 * k)[0]))
                for k in range(count)]
        out = []
        for k, (start, rate) in enumerate(rows):
            end = rows[k + 1][0] if k + 1 < len(rows) else len(bd)
            data = bd[start:end]
            stop = next((a + 16 for a in range(0, len(data), 16) if data[a + 1] & 1), len(data))
            out.append((rate, data[:stop]))
        return out
