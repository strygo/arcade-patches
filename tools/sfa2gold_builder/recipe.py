"""Copy/literal recipe codec for the SFA2 Gold reconstruction.

A target ROM member is expressed as a sequence of ops over a *source pool* the
user can reproduce from their own files (arcade romset members, comp2 extracted
from their PS2 disc, cps2-encoded entry531 tiles, disc samples). Whatever is
not copyable from those sources is a LITERAL — our own authored bytes (vasm
output + tables), which are ours to ship.

Ops (compact binary):
    'C' src_id:u16  off:u32  length:u32     copy from pool[src_id][off:off+len]
    'L' length:u32  <bytes>                 literal (authored) bytes

The generator (encode_member) runs where the target and full pool are present;
the applier (apply_member) needs only the recipe + the user-reproducible pool.
apply_member re-hashes its output, so a wrong source can never yield a wrong ROM.
"""
import hashlib
import struct
from collections import defaultdict

SEED = 8
# Cap candidates examined per position. A verbatim run is found among the first
# few seed hits; the cap bounds worst-case cost on highly repetitive data
# (silence in audio, blank gfx tiles) without affecting correctness.
MAX_CANDIDATES = 96


def _index(pool: bytes):
    seeds = defaultdict(list)
    for i in range(len(pool) - SEED):
        seeds[pool[i:i + SEED]].append(i)
    return seeds


def encode_member(target: bytes, sources: list[tuple[str, bytes]],
                  base_id: str | None = None) -> list:
    """Greedy copy/literal encode of `target` over the named `sources`.

    `sources` is an ordered [(id, bytes), ...]; ids are stored as their index.
    If `base_id` is given, a same-offset run against that source is preferred
    first (captures the unchanged arcade bulk cheaply).
    """
    base = None
    for idx, (sid, data) in enumerate(sources):
        if sid == base_id:
            base = (idx, data)
    # one shared seed index over the concatenated pool, with per-source spans
    spans = []
    blob = bytearray()
    for idx, (_sid, data) in enumerate(sources):
        spans.append((len(blob), idx))
        blob += data
    blob = bytes(blob)
    seeds = _index(blob)
    # per-source [start, end) in the concatenated blob, so a copy never spans
    # two sources (which would read past a member boundary on apply).
    starts = [s for s, _ in spans]
    ends = [starts[k + 1] if k + 1 < len(starts) else len(blob)
            for k in range(len(starts))]

    def src_end(gpos):
        for k in range(len(starts) - 1, -1, -1):
            if gpos >= starts[k]:
                return ends[k]
        return len(blob)

    def locate(gpos):
        for start, idx in reversed(spans):
            if gpos >= start:
                return idx, gpos - start
        return 0, gpos

    ops = []
    lit = bytearray()
    i, n = 0, len(target)

    def flush_lit():
        if lit:
            ops.append(("L", bytes(lit)))
            lit.clear()

    while i < n:
        if base is not None:
            bidx, bdata = base
            if i < len(bdata) and target[i] == bdata[i]:
                j = i
                while j < n and j < len(bdata) and target[j] == bdata[j]:
                    j += 1
                if j - i >= SEED:
                    flush_lit()
                    ops.append(("C", bidx, i, j - i))
                    i = j
                    continue
        best_len, best_pos = 0, 0
        if i + SEED <= n:
            cands = seeds.get(target[i:i + SEED], ())
            if len(cands) > MAX_CANDIDATES:
                cands = cands[-MAX_CANDIDATES:]
            for p in cands:
                l = 0
                lim = min(n - i, src_end(p) - p)
                while l < lim and blob[p + l] == target[i + l]:
                    l += 1
                if l > best_len:
                    best_len, best_pos = l, p
                    if best_len >= 4096:
                        break
        if best_len >= SEED:
            flush_lit()
            idx, off = locate(best_pos)
            ops.append(("C", idx, off, best_len))
            i += best_len
        else:
            lit.append(target[i])
            i += 1
    flush_lit()
    return ops


def apply_member(ops: list, sources: list[bytes], want_md5: str | None = None) -> bytes:
    out = bytearray()
    for op in ops:
        if op[0] == "C":
            _, idx, off, length = op
            out += sources[idx][off:off + length]
        else:
            out += op[1]
    result = bytes(out)
    if want_md5 and hashlib.md5(result).hexdigest() != want_md5:
        raise ValueError("reconstructed member failed checksum")
    return result


def literal_bytes(ops: list) -> int:
    return sum(len(op[1]) for op in ops if op[0] == "L")


# --- compact serialization (recipe file) ---

def dump_ops(ops: list) -> bytes:
    out = bytearray()
    for op in ops:
        if op[0] == "C":
            out += b"C" + struct.pack("<HII", op[1], op[2], op[3])
        else:
            out += b"L" + struct.pack("<I", len(op[1])) + op[1]
    out += b"E"
    return bytes(out)


def load_ops(buf: bytes) -> list:
    ops = []
    pos = 0
    while True:
        tag = buf[pos:pos + 1]
        pos += 1
        if tag == b"E":
            break
        if tag == b"C":
            idx, off, length = struct.unpack_from("<HII", buf, pos)
            pos += 10
            ops.append(("C", idx, off, length))
        elif tag == b"L":
            (length,) = struct.unpack_from("<I", buf, pos)
            pos += 4
            ops.append(("L", buf[pos:pos + length]))
            pos += length
        else:
            raise ValueError(f"bad recipe op {tag!r}")
    return ops


def self_test() -> None:
    import random
    rng = random.Random(7)
    for _ in range(200):
        base = bytes(rng.randrange(256) for _ in range(4000))
        extra = bytes(rng.randrange(256) for _ in range(2000))
        target = bytearray(base)
        for _ in range(rng.randrange(1, 6)):
            at = rng.randrange(len(target))
            if rng.random() < 0.5:  # graft a run from `extra`
                s = rng.randrange(len(extra) - 40)
                target[at:at + 30] = extra[s:s + 30]
            else:                    # authored literal
                target[at:at + 20] = bytes(rng.randrange(256) for _ in range(20))
        target = bytes(target)
        sources = [("base", base), ("extra", extra)]
        ops = load_ops(dump_ops(encode_member(target, sources, base_id="base")))
        assert apply_member(ops, [d for _, d in sources]) == target
    print("recipe self-test OK")


if __name__ == "__main__":
    self_test()
