"""Self-contained ISO extraction front-end for the SFA2 Gold reconstruction.

Given a user's PlayStation 2 anthology disc image and manifest-pinned offsets,
this reproduces the intermediate inputs the reconstruction consumes — entry531
Cammy tiles, the zero6 comp2 68K data, and the QSound audio — verifying each
against known checksums. It reads only from the user's own disc; it embeds no
game data. Pure stdlib + bundled bizlz.
"""
import hashlib
import struct

import bizlz


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def afs_entries(iso, base):
    with open(iso, "rb") as f:
        f.seek(base)
        hdr = f.read(16)
        if hdr[:4] != b"AFS\0":
            raise ValueError(f"no AFS archive at {base:#x} in {iso}")
        count = struct.unpack_from("<I", hdr, 4)[0]
        f.seek(base + 8)
        tbl = f.read(8 * count)
    return [struct.unpack_from("<II", tbl, 8 * i) for i in range(count)]


def read_span(iso, off, size):
    with open(iso, "rb") as f:
        f.seek(off)
        return f.read(size)


def decode_container(blob):
    """entry531 tile container -> list of 128-byte chunky tiles."""
    count = struct.unpack_from("<I", blob, 0x14)[0]
    table = struct.unpack_from("<I", blob, 0x18)[0]
    pointers = [struct.unpack_from("<I", blob, table + i * 4)[0] for i in range(count)]
    offsets = sorted({p & 0x7FFFFFFF for p in pointers})
    nxt = {v: (offsets[i + 1] if i + 1 < len(offsets) else len(blob))
           for i, v in enumerate(offsets)}
    tiles = []
    for p in pointers:
        o = p & 0x7FFFFFFF
        block = blob[o:nxt[o]]
        tile = bizlz_tile(block) if p >> 31 else block[:128]
        if len(tile) != 128:
            raise ValueError(f"container tile at {o:#x} is not 128 bytes")
        tiles.append(bytes(tile))
    return tiles


def bizlz_tile(block):
    """Per-tile control-bit LZSS (tilelz) -> 128-byte chunky tile."""
    out = bytearray()
    src = 0
    ctrl = block[0]
    bitpos = 0
    src = 1
    while len(out) < 128:
        if bitpos == 8:
            ctrl = block[src]
            src += 1
            bitpos = 0
        bit = (ctrl >> (7 - bitpos)) & 1
        bitpos += 1
        if bit == 0:
            out.append(block[src])
            src += 1
        else:
            b = block[src]
            src += 1
            offset = (b >> 4) + 1
            length = (b & 0x0F) + 2
            start = len(out) - offset
            for _ in range(length):
                out.append(out[start])
                start += 1
    return bytes(out[:128])


def wordswap(b):
    b = bytearray(b)
    b[0::2], b[1::2] = b[1::2], b[0::2]
    return bytes(b)


def _split_audio(z80, samples):
    return {
        "sza.01": z80[:0x8000] + z80[0x10000:0x28000],
        "sza.02": z80[0x28000:0x48000],
        "sza.11m": wordswap(samples[:0x200000]),
        "sza.12m": wordswap(samples[0x200000:]),
    }


def extract_audio(iso, profile):
    """Extract both QSound source sets (zero4 Alpha + zero6 Gold) from the disc.

    Returns {'zero4': {sza.0x}, 'zero6': {sza.0x}} — the two sample/Z80 sources
    the 8MB corpus is repacked from. Each verified against sound_sets MD5s.
    """
    base = profile["afs_base"]
    entries = afs_entries(iso, base)
    out = {}
    for pkg, spec in profile["audio_packages"].items():
        blobs = {}
        for role in ("z80", "sample"):
            idx, off, size = spec[role]
            e_off, e_size = entries[idx]
            if (e_off, e_size) != (off, size):
                raise ValueError(f"{pkg} {role} AFS span does not match manifest")
            blobs[role] = read_span(iso, base + e_off, e_size)
        if len(blobs["z80"]) != 0x48000 or len(blobs["sample"]) != 0x400000:
            raise ValueError(f"{pkg} audio source entries are truncated")
        members = _split_audio(blobs["z80"], blobs["sample"])
        for name, want in spec["md5"].items():
            if md5(members[name]) != want:
                raise ValueError(f"{pkg} {name} base-audio identity mismatch")
        out[pkg] = members
    return out


def extract_zero6(iso, profile):
    """Return dict with entry531 tiles, comp2, and raw samples/z80 for a region.

    profile: the region's manifest params (afs_base, entry531 span+md5,
    comp2 md5, zero6 package z80/sample offsets/sizes).
    """
    base = profile["afs_base"]
    entries = afs_entries(iso, base)

    # entry531 Cammy tile page (span read from the AFS table; identity by MD5)
    e_off, e_size = entries[profile["entry531_index"]]
    span = profile.get("entry531_span")
    if span is not None and (e_off, e_size) != tuple(span):
        raise ValueError("X_DATA entry531 span does not match manifest")
    entry531_blob = read_span(iso, base + e_off, e_size)
    if md5(entry531_blob) != profile["entry531_md5"]:
        raise ValueError("entry531 identity mismatch — wrong disc?")
    entry531 = decode_container(entry531_blob)

    # zero6 MWo3 package -> comp2 (68K data)
    m_off, m_size = entries[profile["mwo3_entry"]]
    raw = read_span(iso, base + m_off, m_size)
    payload, _ = bizlz.decompress(raw, 0, max_out=64 * 1024 * 1024)
    if payload[:4] != b"MWo3":
        raise ValueError("zero6 package is not MWo3")
    c1 = struct.unpack_from("<I", payload, 0x0C)[0]
    c2 = struct.unpack_from("<I", payload, 0x10)[0]
    comp2 = payload[0x40 + c1:0x40 + c1 + c2]
    if md5(comp2) != profile["comp2_md5"]:
        raise ValueError("zero6 comp2 identity mismatch")

    return {"entry531": entry531, "comp2": comp2, "payload": payload}


# US region parameters (manifest-derived); other regions added alongside.
US = {
    "afs_base": 0x9F738000,
    "entry531_index": 531,
    "entry531_span": (0x9BBE800, 0x176A33),
    "entry531_md5": "2e21caf75cce31f44addccfc20e67331",
    "mwo3_entry": 526,
    "comp2_md5": "76b1e4bc7cd5fa11dc9d3ea2354bc5a7",
    "audio_packages": {
        "zero6": {
            "z80": (532, 164845568, 0x48000),
            "sample": (533, 165140480, 0x400000),
            "md5": {
                "sza.01": "c0925edb2d3eb4c535d0306bb81879fb",
                "sza.02": "0dfbe8aa4c20b52e1b8bf3cb6cbdf193",
                "sza.11m": "d528426f67e4d5d398d8198a74f74fd0",
                "sza.12m": "76c2c32ccd8c7acf679e2ca57eff3b86",
            },
        },
        "zero4": {
            "z80": (250, 45391872, 0x48000),
            "sample": (251, 45686784, 0x400000),
            "md5": {
                "sza.01": "c6f1a90862eeb212471a47ae1a8eecbe",
                "sza.02": "c8d771e6e0c10beac734c8f8f795327d",
                "sza.11m": "9946a62ec5f02ac142e9517544affa28",
                "sza.12m": "a60784f7c510415feac7b8cc3d4692d5",
            },
        },
    },
}


# zero4/zero6 base-audio MD5s are the same across region discs (one sound set
# each); only the AFS offsets differ per disc.
_Z4_MD5 = {"sza.01": "c6f1a90862eeb212471a47ae1a8eecbe",
           "sza.02": "c8d771e6e0c10beac734c8f8f795327d",
           "sza.11m": "9946a62ec5f02ac142e9517544affa28",
           "sza.12m": "a60784f7c510415feac7b8cc3d4692d5"}
_Z6_MD5 = {"sza.01": "c0925edb2d3eb4c535d0306bb81879fb",
           "sza.02": "0dfbe8aa4c20b52e1b8bf3cb6cbdf193",
           "sza.11m": "d528426f67e4d5d398d8198a74f74fd0",
           "sza.12m": "76c2c32ccd8c7acf679e2ca57eff3b86"}
_ENTRY531_MD5 = "2e21caf75cce31f44addccfc20e67331"
_SZ80, _SSMP = 0x48000, 0x400000


def _region(afs_base, comp2_md5, z6_z80, z6_smp, z4_z80, z4_smp):
    return {
        "afs_base": afs_base, "entry531_index": 531, "entry531_span": None,
        "entry531_md5": _ENTRY531_MD5, "mwo3_entry": 526, "comp2_md5": comp2_md5,
        "audio_packages": {
            "zero6": {"z80": (532, z6_z80, _SZ80), "sample": (533, z6_smp, _SSMP), "md5": _Z6_MD5},
            "zero4": {"z80": (250, z4_z80, _SZ80), "sample": (251, z4_smp, _SSMP), "md5": _Z4_MD5},
        },
    }


JP = _region(0x9F720800, "77e656c28bd271cf8f17cf66ad177900",
             164935680, 165230592, 45393920, 45688832)
EU = _region(0x9F739000, "5119e1ba43f61221cfa30c4aa5617c99",
             164841472, 165136384, 45387776, 45682688)
ASIA = _region(0x9F738000, "76b1e4bc7cd5fa11dc9d3ea2354bc5a7",
               164845568, 165140480, 45391872, 45686784)


if __name__ == "__main__":
    import sys
    iso = sys.argv[1]
    r = extract_zero6(iso, US)
    print(f"entry531: {len(r['entry531'])} tiles (verified)")
    print(f"comp2: {len(r['comp2'])} bytes (verified)")
    a = extract_audio(iso, US)
    for pkg, mem in a.items():
        print(f"{pkg} audio: {', '.join(f'{k}={len(v)}' for k, v in mem.items())} (verified)")
