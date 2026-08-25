"""Turn a user-supplied CD rip into a directory of trNN.wav files.

The builders never search for disc images or match filenames — the user
passes the rip explicitly.  Accepted forms:

  * a .cue sheet (single-image or redump multi-bin, bins beside it);
  * a .zip/.7z archive that contains exactly one cue sheet, unpacked once
    into work/discs/<archive-stem>/ (a cache, safe to delete).  .7z needs a
    7-Zip binary (7zz/7z on PATH; brew install sevenzip); .zip is native.

Audio is cut exactly at the cue's INDEX 01 boundaries by tools/extract_cdda
(raw CD-DA sectors, nothing trimmed/faded/resampled). Verified:
this reproduces a hand-ripped Final Fight cd_full byte-for-byte.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import zipfile
from pathlib import Path

from .build_common import PKG_ROOT

_spec = importlib.util.spec_from_file_location(
    "extract_cdda", Path(__file__).resolve().parent.parent / "tools" / "extract_cdda.py")
extract_cdda = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_cdda)

DISC_CACHE = PKG_ROOT / "work" / "discs"


def _unpack(archive: Path) -> Path:
    """Unpack `archive` into the disc cache (once) and return the cache dir."""
    dest = DISC_CACHE / archive.stem
    if any(dest.glob("**/*.cue")) or any(dest.glob("**/*.mds")):
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    elif archive.suffix.lower() == ".7z":
        exe = shutil.which("7zz") or shutil.which("7z")
        if not exe:
            raise FileNotFoundError(
                f"{archive.name}: need a 7-Zip binary to unpack it "
                f"(brew install sevenzip), or pre-extract the rip and pass "
                f"the .cue directly")
        subprocess.run([exe, "x", "-y", f"-o{dest}", str(archive)],
                       check=True, capture_output=True)
    else:
        raise ValueError(
            f"{archive}: unsupported input — pass a .cue, .zip or .7z")
    return dest


def extract_disc(source: Path, dest: Path, pregap: str = "trim") -> None:
    """Extract every audio track of the rip at `source` to `dest`/trNN.wav.

    `source` is exactly what the user pointed at: a .cue, or a .zip/.7z
    containing one cue.  No searching, no filename matching.
    """
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"{source} does not exist")
    if source.suffix.lower() in (".cue", ".mds"):
        cue = source
    else:
        udir = _unpack(source)
        cues = sorted(udir.glob("**/*.cue")) or sorted(udir.glob("**/*.mds"))
        if not cues:
            raise FileNotFoundError(
                f"{source.name}: no .cue or .mds descriptor inside the "
                f"archive — pass the rip's descriptor file directly")
        if len(cues) > 1:
            names = ", ".join(c.name for c in cues)
            raise FileExistsError(
                f"{source.name}: multiple cue sheets inside ({names}) — "
                f"pass the one you mean directly")
        cue = cues[0]
    print(f"[discsrc] {dest.name}: extracting CD-DA from {cue.name} "
          f"(pregap={pregap})")
    extract_cdda.extract(cue, dest, quiet=True, pregap=pregap)


def ensure_audio_cache(dest: Path, needed: set[str], disc, flag: str,
                       pregap: str = "trim") -> None:
    """Fill the trNN.wav extraction cache at `dest` from the user-supplied
    disc rip, unless every needed track is already present."""
    if all((dest / f"{t}.wav").exists() for t in needed):
        return
    if not disc:
        raise FileNotFoundError(
            f"{dest} is missing tracks and no disc was given -- pass {flag} "
            f"pointing at the rip (.cue, or a .zip/.7z containing one)")
    extract_disc(Path(disc), dest, pregap=pregap)
    missing = [t for t in needed if not (dest / f"{t}.wav").exists()]
    if missing:
        raise FileNotFoundError(
            f"{dest}: extraction from {disc} ran but tracks are still "
            f"missing: {missing} -- is this the right disc?")
