"""Rebuild the Arrange sets from your own romsets and disc.

Each ROM member is a list of copy/literal operations (recipe.py) over a pool of
sources derived on your machine: the romsets' own members (as stored and
byte-swapped), the disc's arrange packages, its sprite tiles converted to CPS-2
form, its voices converted to QSound samples, and the romsets' sprite graphics
decoded to tiles.  Graphics are rebuilt per four-ROM group at tile level and
re-encoded; everything else at byte level.  Literal operations carry only our
own work.  Every member is checksum-verified against the known build.
"""
import base64
import hashlib
import zlib

import cps2tiles as tiles
import recipe as rc
import voices


def wordswap(b):
    out = bytearray(len(b))
    out[0::2], out[1::2] = b[1::2], b[0::2]
    return bytes(out)


def suffix(name):
    return name.rsplit(".", 1)[-1]


def gfx_groups(members):
    """{(letter, group index): [four member names]} for a set's 4 MiB sprite ROMs."""
    out = {}
    for letter in ("m", "h"):
        for g, nums in enumerate(tiles.GROUPS):
            names = [next((n for n in members if suffix(n) == num + letter), None) for num in nums]
            if all(names):
                out[(letter, g)] = names
    return out


class Inputs:
    """The named source pool: every recipe source resolves to bytes here."""

    def __init__(self, roms, disc, spec):
        self.roms, self.disc, self.spec = roms, disc, spec
        self.cache = {}

    def source(self, name):
        if name in self.cache:
            return self.cache[name]
        kind, _, rest = name.partition(":")
        if name.endswith(":ws"):
            data = wordswap(self.source(name[:-3]))
        elif kind == "rom":
            rom, member = rest.split(":")
            data = self.roms[rom][member]
        elif kind == "pkg":
            package, part = rest.split(":")
            entry, md5 = self.spec["packages"][package]
            comp1, comp2 = self.disc.package(entry, md5)
            self.cache[f"pkg:{package}:comp1"], self.cache[f"pkg:{package}:comp2"] = comp1, comp2
            data = comp1 if part == "comp1" else comp2
        elif kind == "disctiles":
            entry, md5 = next(t for t in self.spec["tile_entries"] if str(t[0]) == rest)
            data = self.disc.tiles(entry, md5)
        elif kind == "disclayer":
            entry, md5 = next(t for t in self.spec["layer_entries"] if str(t[0]) == rest)
            data = self.disc.layer(entry, md5)
        elif kind == "voices":
            data = self._voices()
        elif kind == "gfx":
            rom, letter, group = rest.split(":")
            names = gfx_groups(self.roms[rom])[(letter, int(group))]
            data = tiles.decode_group([self.roms[rom][n] for n in names])
        else:
            raise ValueError(f"unknown recipe source {name}")
        self.cache[name] = data
        return data

    def _voices(self):
        out, banks = bytearray(), {}
        for entry, md5, vag, rate_in, rate_out in self.spec["voices"]:
            if entry not in banks:
                banks[entry] = self.disc.vags(entry, md5)
            rate, data = banks[entry][vag]
            if rate != rate_in:
                raise ValueError(f"voice bank {entry} VAG {vag} rate differs")
            out += voices.convert(data, rate_in, rate_out)
            out += bytes(-len(out) % 16)      # 16-byte aligned, as the sample ROM stores them
        return bytes(out)

    def pool(self, names):
        return [self.source(n) for n in names]


# --- generator: recipes from a verified target ---------------------------------

def encode_tiles(target, pool):
    """Copy/literal ops over decoded graphics at whole-tile granularity."""
    index = {}
    for sid, data in enumerate(pool):
        for k in range(0, len(data) - tiles.TILE + 1, tiles.TILE):
            index.setdefault(data[k:k + tiles.TILE], (sid, k))
    ops, i, n, lit = [], 0, len(target), bytearray()

    def flush():
        if lit:
            ops.append(("L", bytes(lit)))
            lit.clear()

    while i < n:
        t = target[i:i + tiles.TILE]
        last = ops[-1] if ops and not lit else None
        if last and last[0] == "C":
            _, sid, off, length = last
            src = pool[sid]
            if src[off + length:off + length + tiles.TILE] == t:
                ops[-1] = ("C", sid, off, length + tiles.TILE)
                i += tiles.TILE
                continue
        hit = index.get(t)
        if hit is None:
            lit += t
            i += tiles.TILE
            continue
        flush()
        sid, off = hit
        ops.append(("C", sid, off, tiles.TILE))
        i += tiles.TILE
    flush()
    return ops


def encode_member(target, pool, base=None):
    """Greedy copy/literal ops, then an alignment pass over the literal runs: a run
    that is mostly the same bytes as some source at one alignment (a record with a
    few relocated fields) becomes short copies around only the differing bytes."""
    ops = rc.encode_member(target, [(k, d) for k, d in enumerate(pool)], base_id=base)
    seeds = {}
    # 68000 data aligns on words; byte-oriented pools (the Z80's) are small enough to index every offset.
    step = 1 if sum(map(len, pool)) <= 4 << 20 else 2
    for sid, data in enumerate(pool):
        for i in range(0, len(data) - 3, step):
            hits = seeds.setdefault(data[i:i + 4], [])
            if len(hits) < 8:
                hits.append((sid, i))
    out = []
    for op in ops:
        if op[0] != "L" or len(op[1]) < 16:
            out.append(op)
            continue
        out.extend(_align_literal(op[1], pool, seeds, step))
    return _merge_ops(out)


def _align_literal(lit, pool, seeds, step=2, min_copy=4):
    votes = {}
    for k in range(0, len(lit) - 3, step):
        for sid, i in seeds.get(lit[k:k + 4], ()):
            votes[(sid, i - k)] = votes.get((sid, i - k), 0) + 1
    if not votes:
        return [("L", lit)]
    sid, base = max(votes, key=votes.get)
    src = pool[sid]
    ops, k, n = [], 0, len(lit)
    while k < n:
        j = k
        while j < n and 0 <= base + j < len(src) and src[base + j] == lit[j]:
            j += 1
        if j - k >= min_copy:
            ops.append(("C", sid, base + k, j - k))
            k = j
            continue
        j = max(j, k + 1)
        while j < n and not all(0 <= base + m < len(src) and src[base + m] == lit[m]
                                for m in range(j, min(j + min_copy, n))):
            j += 1
        ops.append(("L", lit[k:j]))
        k = j
    return ops


def _merge_ops(ops):
    out = []
    for op in ops:
        if out and op[0] == "L" and out[-1][0] == "L":
            out[-1] = ("L", out[-1][1] + op[1])
        else:
            out.append(op)
    return out


# --- applier ------------------------------------------------------------------

def reconstruct(inputs, recipes):
    """{member: bytes} for one set, every member verified by MD5."""
    out = {}
    for unit in recipes["units"]:
        pool = inputs.pool(unit["pool"])
        data = rc.apply_member(rc.load_ops(zlib.decompress(base64.b64decode(unit["ops"]))), pool)
        if unit["level"] == "tiles":
            for name, rom in zip(unit["members"], tiles.encode_group(data)):
                out[name] = rom
        elif unit["level"] == "samples":       # encoded in sample (un-swapped) order
            out[unit["members"][0]] = wordswap(data)
        else:
            out[unit["members"][0]] = data
    for name, want in recipes["md5"].items():
        if name not in out:
            raise ValueError(f"recipe has no unit for {name}")
        if hashlib.md5(out[name]).hexdigest() != want:
            raise ValueError(f"{name}: checksum mismatch — wrong disc or romset?")
    return out
