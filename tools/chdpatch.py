"""Generate data-free CHD patch manifests.

A CHD is a compressed container, so container-level diffs balloon (every hunk
recompresses) and carry copyrighted data. Instead we describe the change at
the disc-data level: which track, which offset, which bytes. The shipped
apply script drives the user's own chdman (extract -> patch -> rebuild), and
the CHD header SHA1 — which covers decompressed data plus metadata, not the
container — pins both input and output regardless of chdman version.
"""

import hashlib
import os
import shutil
import struct
import subprocess
import tempfile

MAX_PATCH_BYTES = 4096  # sanity bound: this format is for surgical patches


def chd_header_sha1(path) -> str:
    """Read the SHA1 field straight from a CHD v5 header (no chdman needed)."""
    with open(path, "rb") as f:
        h = f.read(124)
    if h[:8] != b"MComprHD":
        raise ValueError(f"{path}: not a CHD file")
    version = struct.unpack(">I", h[12:16])[0]
    if version != 5:
        raise ValueError(f"{path}: only CHD v5 is supported (found v{version})")
    return h[84:104].hex()


def chdman_hashes(path, chdman="chdman"):
    """(sha1, data_sha1) as reported by chdman info."""
    out = subprocess.run(
        [chdman, "info", "-i", str(path)], capture_output=True, text=True, check=True
    ).stdout
    sha1 = data_sha1 = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SHA1:"):
            sha1 = line.split(":", 1)[1].strip()
        elif line.startswith("Data SHA1:"):
            data_sha1 = line.split(":", 1)[1].strip()
    return sha1, data_sha1


def _sha1_file(path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(chd, outdir, chdman):
    gdi = os.path.join(outdir, "disc.gdi")
    subprocess.run(
        [chdman, "extractcd", "-i", str(chd), "-o", gdi],
        capture_output=True, text=True, check=True,
    )
    tracks = {}
    with open(gdi, encoding="ascii") as f:
        fields = f.read().split()
    for t in range(int(fields[0])):
        row = fields[1 + t * 6 : 1 + (t + 1) * 6]
        tracks[int(row[0])] = os.path.join(outdir, row[4])
    return tracks


def _diff_track(a_path, b_path):
    """All differing byte positions between two equally sized files."""
    diffs = []
    off = 0
    with open(a_path, "rb") as fa, open(b_path, "rb") as fb:
        while True:
            xa, xb = fa.read(1 << 22), fb.read(1 << 22)
            if not xa and not xb:
                break
            if len(xa) != len(xb):
                raise ValueError("track sizes differ")
            if xa != xb:
                diffs.extend(off + i for i in range(len(xa)) if xa[i] != xb[i])
            off += len(xa)
    return diffs


def generate(stock_chd, patched_chd, chdman="chdman", workdir=None) -> dict:
    """Extract both CHDs, locate the byte-level change, return a manifest."""
    stock_sha1, stock_data = chdman_hashes(stock_chd, chdman)
    patched_sha1, patched_data = chdman_hashes(patched_chd, chdman)

    tmp = tempfile.mkdtemp(prefix="chdgen-", dir=workdir)
    try:
        stock_dir = os.path.join(tmp, "stock")
        patched_dir = os.path.join(tmp, "patched")
        os.makedirs(stock_dir)
        os.makedirs(patched_dir)
        stock_tracks = _extract(stock_chd, stock_dir, chdman)
        patched_tracks = _extract(patched_chd, patched_dir, chdman)
        if set(stock_tracks) != set(patched_tracks):
            raise ValueError("track layouts differ")

        changed = []
        for t in sorted(stock_tracks):
            diffs = _diff_track(stock_tracks[t], patched_tracks[t])
            if diffs:
                changed.append((t, diffs))
        if len(changed) != 1:
            raise ValueError(f"expected changes in exactly one track, found {len(changed)}")
        track, diffs = changed[0]
        start, end = diffs[0], diffs[-1] + 1
        if end - start > MAX_PATCH_BYTES:
            raise ValueError(f"changed range too large for a surgical patch: {end - start}")
        # Align to 8-byte boundaries (DES block granularity on NAOMI GD-ROMs).
        start -= start % 8
        end += (-end) % 8
        with open(stock_tracks[track], "rb") as f:
            f.seek(start)
            old = f.read(end - start)
        with open(patched_tracks[track], "rb") as f:
            f.seek(start)
            new = f.read(end - start)

        return {
            "source": {
                "chd_sha1": stock_sha1,
                "data_sha1": stock_data,
                "size": os.path.getsize(stock_chd),
            },
            "target": {
                "chd_sha1": patched_sha1,
                "data_sha1": patched_data,
            },
            "patch": {
                "track": track,
                "offset": start,
                "old": old.hex(),
                "new": new.hex(),
                "track_sha1_before": _sha1_file(stock_tracks[track]),
                "track_sha1_after": _sha1_file(patched_tracks[track]),
            },
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
