"""Minimal IPS (International Patching System) encoder/decoder.

Standard IPS records only (no RLE, no truncation extension). Suitable for
same-size binaries under 16 MiB, which covers CPS-2 program ROMs.
"""

MAGIC = b"PATCH"
EOF = b"EOF"
MAX_OFFSET = 0xFFFFFF
MAX_RUN = 0xFFFF
# Offset 0x454F46 spells "EOF"; a record must not start there.
EOF_OFFSET = 0x454F46
# Gaps of identical bytes shorter than a record header (5 bytes) are cheaper
# to include in the surrounding record than to split into two records.
JOIN_GAP = 5


def make_ips(src: bytes, dst: bytes) -> bytes:
    """Build an IPS patch transforming src into dst (equal lengths required)."""
    if len(src) != len(dst):
        raise ValueError(f"size mismatch: {len(src)} != {len(dst)}")
    if len(dst) > MAX_OFFSET:
        raise ValueError("IPS cannot address beyond 16 MiB")

    # Collect [start, end) runs of differing bytes, merging nearby runs.
    runs = []
    i = 0
    n = len(dst)
    while i < n:
        if src[i] == dst[i]:
            i += 1
            continue
        start = i
        while i < n and src[i] != dst[i]:
            i += 1
        if runs and start - runs[-1][1] < JOIN_GAP:
            runs[-1][1] = i
        else:
            runs.append([start, i])

    out = [MAGIC]
    for start, end in runs:
        while start < end:
            if start == EOF_OFFSET:
                start -= 1  # re-emit one already-written byte to shift the header
            size = min(end - start, MAX_RUN)
            out.append(start.to_bytes(3, "big"))
            out.append(size.to_bytes(2, "big"))
            out.append(dst[start : start + size])
            start += size
    out.append(EOF)
    return b"".join(out)


def apply_ips(patch: bytes, src: bytes) -> bytes:
    """Apply an IPS patch (standard records + RLE) to src."""
    if patch[:5] != MAGIC:
        raise ValueError("not an IPS patch (bad magic)")
    buf = bytearray(src)
    pos = 5
    while True:
        if pos + 3 > len(patch):
            raise ValueError("truncated IPS patch")
        if patch[pos : pos + 3] == EOF:
            break
        offset = int.from_bytes(patch[pos : pos + 3], "big")
        size = int.from_bytes(patch[pos + 3 : pos + 5], "big")
        pos += 5
        if size == 0:  # RLE record
            rle_size = int.from_bytes(patch[pos : pos + 2], "big")
            data = patch[pos + 2 : pos + 3] * rle_size
            pos += 3
        else:
            data = patch[pos : pos + size]
            pos += size
        end = offset + len(data)
        if end > len(buf):
            buf.extend(b"\x00" * (end - len(buf)))
        buf[offset:end] = data
    return bytes(buf)


def self_test() -> None:
    import random

    rng = random.Random(1234)
    for trial in range(50):
        n = rng.randrange(1, 200_000)
        src = bytes(rng.randrange(256) for _ in range(64)) * (n // 64 + 1)
        src = src[:n]
        dst = bytearray(src)
        for _ in range(rng.randrange(1, 40)):
            at = rng.randrange(n)
            ln = min(rng.randrange(1, 300), n - at)
            for j in range(at, at + ln):
                dst[j] = rng.randrange(256)
        dst = bytes(dst)
        patch = make_ips(src, dst)
        assert apply_ips(patch, src) == dst, f"round-trip failed on trial {trial}"
    # EOF-offset edge case: a lone diff exactly at 0x454F46.
    n = EOF_OFFSET + 16
    src = bytes(n)
    dst = bytearray(src)
    dst[EOF_OFFSET] = 0xAA
    patch = make_ips(src, bytes(dst))
    assert apply_ips(patch, src) == bytes(dst)
    print("ipsutil self-test OK")


if __name__ == "__main__":
    self_test()
