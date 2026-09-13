"""Turn a user-supplied CD rip into a directory of trNN.wav files.

The builders never search for disc images or match filenames — the user
passes the rip explicitly.  Accepted forms:

  * a .cue sheet (single-image or redump multi-bin, bins beside it);
  * a .zip/.7z archive that contains exactly one cue sheet, unpacked once
    into work/discs/<archive-stem>/ (a cache, safe to delete).  .7z needs a
    7-Zip binary (7zz/7z/7za/7zr on PATH, or 7-Zip installed on Windows;
    brew install sevenzip); .zip is native.

Audio is cut exactly at the cue's INDEX 01 boundaries by tools/extract_cdda
(raw CD-DA sectors, nothing trimmed/faded/resampled). Verified:
this reproduces a hand-ripped Final Fight cd_full byte-for-byte.

BOTH CACHES ARE KEYED BY THEIR SOURCE.  Each cache directory records what
it was made from (path, size and modification time of the archive, or of
the cue/.mds and every image file it names, plus the extraction settings)
and is reused only while that still describes the source.  Pointing at a
different disc, replacing a .bin, or an extraction that was interrupted
half-way therefore re-extracts instead of silently building from the old
audio: every cache is filled in a sibling .partial directory and renamed
into place only once complete.
"""
from __future__ import annotations

import importlib.util
import json
import os
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
STAMP = ".cpsplus_source.json"
# Bump when extraction output could change for the same rip, so caches made
# by an older extractor are not reused.  2: PREGAP honoured, INDEX 02+ ignored.
EXTRACT_VERSION = 2


def find_7z() -> str | None:
    """A 7-Zip command-line binary: PATH first, then a Windows install."""
    for exe in ("7zz", "7z", "7za", "7zr"):
        hit = shutil.which(exe)
        if hit:
            return hit
    if os.name == "nt":
        roots = [os.environ.get(v) for v in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")]
        for root in filter(None, roots):
            cand = Path(root) / "7-Zip" / "7z.exe"
            if cand.is_file():
                return str(cand)
        try:
            import winreg
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\7-Zip",
                                        0, winreg.KEY_READ | view) as k:
                        for name in ("Path64", "Path"):
                            try:
                                cand = Path(winreg.QueryValueEx(k, name)[0]) / "7z.exe"
                            except OSError:
                                continue
                            if cand.is_file():
                                return str(cand)
                except OSError:
                    continue
        except ImportError:
            pass
    return None


def _file_id(p: Path) -> dict:
    st = p.stat()
    return {"path": str(p.resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def source_stamp(source: Path, **settings) -> dict:
    """What a cache made from `source` depends on.  A .cue or .mds counts
    together with the image files it names."""
    source = Path(source)
    files = [source]
    suffix = source.suffix.lower()
    if suffix == ".cue":
        try:
            files += [f for f in extract_cdda.cue_files(source) if f.exists()]
        except (OSError, ValueError):
            pass
    elif suffix == ".mds" and source.with_suffix(".mdf").exists():
        files.append(source.with_suffix(".mdf"))
    return {"extractor": EXTRACT_VERSION, **settings,
            "files": [_file_id(f) for f in files]}


def read_stamp(cache: Path) -> dict | None:
    try:
        return json.loads((cache / STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stamp_matches(cache: Path, stamp: dict) -> bool:
    have = read_stamp(cache)
    return have is not None and {k: v for k, v in have.items() if k != "aligned"} == stamp


def _replace_dir(tmp: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)


def _fresh_partial(dest: Path) -> Path:
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)                 # an interrupted earlier attempt
    tmp.mkdir(parents=True)
    return tmp


def _unpack(archive: Path) -> Path:
    """Unpack `archive` into the disc cache (once) and return the cache dir."""
    dest = DISC_CACHE / archive.stem
    stamp = source_stamp(archive)
    if stamp_matches(dest, stamp):
        return dest
    if archive.suffix.lower() not in (".zip", ".7z"):
        raise ValueError(
            f"{archive}: unsupported input — pass a .cue, .zip or .7z")
    exe = find_7z() if archive.suffix.lower() == ".7z" else None
    if archive.suffix.lower() == ".7z" and not exe:
        raise FileNotFoundError(
            f"{archive.name}: need a 7-Zip binary to unpack it "
            f"(brew install sevenzip, or 7-Zip from 7-zip.org on Windows), "
            f"or pre-extract the rip and pass the .cue directly")
    print(f"[discsrc] unpacking {archive.name} ...", flush=True)
    tmp = _fresh_partial(dest)
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
    else:
        r = subprocess.run([exe, "x", "-y", f"-o{tmp}", str(archive)],
                           capture_output=True)
        if r.returncode:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ValueError(
                f"{archive.name}: 7-Zip failed: "
                f"{(r.stderr or r.stdout).decode(errors='replace').strip()[:400]}")
    (tmp / STAMP).write_text(json.dumps(stamp, indent=1), encoding="utf-8")
    _replace_dir(tmp, dest)
    return dest


def _descriptor(source: Path) -> Path:
    if source.suffix.lower() in (".cue", ".mds"):
        return source
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
    return cues[0]


def extract_disc(source: Path, dest: Path, pregap: str = "trim") -> None:
    """Extract every audio track of the rip at `source` to `dest`/trNN.wav.

    `source` is exactly what the user pointed at: a .cue, or a .zip/.7z
    containing one cue.  No searching, no filename matching.
    """
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"{source} does not exist")
    cue = _descriptor(source)
    print(f"[discsrc] {dest.name.removesuffix('.partial')}: extracting CD-DA from {cue.name} "
          f"(pregap={pregap})")
    extract_cdda.extract(cue, dest, quiet=True, pregap=pregap)


def ensure_audio_cache(dest: Path, needed: set[str], disc, flag: str,
                       pregap: str = "trim") -> None:
    """Fill the trNN.wav extraction cache at `dest` from the user-supplied
    disc rip.  With a disc, the cache is reused only if it was extracted
    from that same rip with the same settings; without one, an existing
    cache holding every needed track is used as it stands."""
    if not disc:
        if all((dest / f"{t}.wav").exists() for t in needed):
            return
        raise FileNotFoundError(
            f"{dest} is missing tracks and no disc was given -- pass {flag} "
            f"pointing at the rip (.cue, or a .zip/.7z containing one)")
    source = Path(disc)
    if not source.exists():
        raise FileNotFoundError(f"{source} does not exist")
    stamp = source_stamp(source, pregap=pregap)
    if stamp_matches(dest, stamp) and all((dest / f"{t}.wav").exists() for t in needed):
        return
    if read_stamp(dest) is not None or dest.exists():
        print(f"[discsrc] {dest.name}: cache was made from another rip or "
              f"settings, or is incomplete -- extracting again")
    tmp = _fresh_partial(dest)
    extract_disc(source, tmp, pregap=pregap)
    missing = sorted(t for t in needed if not (tmp / f"{t}.wav").exists())
    if missing:
        shutil.rmtree(tmp, ignore_errors=True)
        raise FileNotFoundError(
            f"extraction from {disc} ran but tracks are still missing: "
            f"{missing} -- is this the right disc?")
    (tmp / STAMP).write_text(json.dumps(stamp, indent=1), encoding="utf-8")
    _replace_dir(tmp, dest)


def check_audio_cache(dest: Path, needed: set[str], pins_path: Path, title: str,
                      tag: str) -> dict:
    """Check the extracted tracks against the pack's input pins and print
    the table.  Tracks whose pinned samples sit elsewhere on the disc (other
    pregap handling, another dump's track split) are re-cut into the cache,
    verified, so the builder reads them like any other track.  Returns the
    checks by track name, or {} when the pack has no pins."""
    from . import inputpins
    pins = inputpins.load_pins(pins_path)
    if pins is None:
        return {}
    def num(p: Path) -> int:
        return int(p.stem[2:]) if p.stem[2:].isdigit() else 1 << 30
    order = sorted(dest.glob("tr*.wav"), key=num)
    want = {t: dest / f"{t}.wav" for t in sorted(needed)}
    checks = inputpins.check_tracks(pins, want, [order], "wav")
    stamp = read_stamp(dest) or {}
    aligned = dict(stamp.get("aligned", {}))
    redo = [c for c in checks.values() if c.status == "ALIGNED"]
    staged = []
    for c in redo:
        pcm = inputpins.load_checked(c, "wav")
        tmp = dest / f"{c.key}.wav.aligned"
        extract_cdda.write_wav(tmp, pcm.tobytes())
        staged.append((tmp, dest / f"{c.key}.wav"))
        aligned[c.key] = c.notes[0] if c.notes else "re-cut"
    for tmp, final in staged:
        tmp.replace(final)
    for key, c in checks.items():
        if c.status == "MATCH" and key in aligned:
            c.notes.append(f"re-cut earlier in this cache: {aligned[key]}")
    if staged and stamp:
        stamp["aligned"] = aligned
        (dest / STAMP).write_text(json.dumps(stamp, indent=1), encoding="utf-8")
    print(inputpins.format_report(title, checks, pins_path.name))
    return checks
