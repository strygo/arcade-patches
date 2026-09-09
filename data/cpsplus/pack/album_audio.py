"""Decode album tracks for the packs built from soundtrack releases.

The Double Impact and HD Remix packs are cut from album files (MP3 and FLAC)
rather than from a game disc.  Decoding goes through ffmpeg so that the same
files give the same PCM everywhere the ADX packs already depend on it.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import numpy as np

RATE = 48000


def sha256(path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def decode(path) -> np.ndarray:
    """The file as 48 kHz stereo s16 PCM, shape (frames, 2)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le",
                          "-ac", "2", "-ar", str(RATE), "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, "<i2").reshape(-1, 2).copy()


def locate(source_root: Path, rel: str) -> Path:
    """The album file `rel` under `source_root`.

    The recipe names files by the album's own folder layout.  If the user's
    copy is laid out differently (renamed folder, flattened), the file is
    found by its basename instead, as long as that name is unique.
    """
    direct = Path(source_root) / rel
    if direct.exists():
        return direct
    name = Path(rel).name
    hits = [p for p in Path(source_root).rglob(name) if p.is_file()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"{name} not found under {source_root}")
    raise FileExistsError(f"{name} appears {len(hits)} times under {source_root}")
