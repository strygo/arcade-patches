"""Input resolution: accept a raw disc image path or a .zip containing one,
extracting zip members to a reusable cache under work/cache/."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent      # the tree holding pack/
CACHE_DIR = PKG_ROOT / "work" / "cache"


def _pick_member(dest: Path, member_hint: str | None):
    """Largest extracted file matching the hint (or largest overall)."""
    if not dest.is_dir():
        return None
    cands = [f for f in dest.glob("**/*") if f.is_file()]
    if member_hint:
        cands = [f for f in cands if member_hint.lower() in f.name.lower()]
    return max(cands, key=lambda f: f.stat().st_size, default=None)


def resolve_image(path: Path | str, member_hint: str | None = None,
                  cache_dir: Path | None = None) -> Path:
    """Return a path to a raw disc image.

    If `path` is a zip, pick the member (by `member_hint` substring, else the
    largest member) and extract it once into the cache; subsequent calls
    reuse the cached copy (validated by size).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path}: does not exist")
    if path.suffix.lower() == ".cue":
        # a rip extracted beside its cue sheet: the first FILE entry is the
        # data track, which is the image every consumer of this function wants
        import re as _re
        # a cue sheet carries no encoding mark and its writer's was whatever
        # the ripping machine used, so take the reading whose file is there
        raw, files = path.read_bytes(), []
        for enc in ("utf-8-sig", "cp932", "cp1252"):
            try:
                names = _re.findall(r'FILE\s+"([^"]+)"', raw.decode(enc))
            except UnicodeDecodeError:
                continue
            files = files or names
            if names and (path.parent / names[0]).exists():
                files = names
                break
        if not files:
            raise ValueError(f"{path.name}: no FILE entries in the cue sheet")
        img = path.parent / files[0]
        if not img.exists():
            raise FileNotFoundError(
                f"{path.name} names {files[0]}, which is not beside it -- "
                f"keep the cue sheet next to its .bin files")
        return img
    if path.suffix.lower() == ".mds":
        img = path.with_suffix(".mdf")
        if not img.exists():
            raise FileNotFoundError(
                f"{path.name}: no matching .mdf beside it")
        return img
    if path.suffix.lower() == ".7z":
        import shutil as _sh
        import subprocess as _sp
        cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        dest = cache_dir / path.stem
        found = _pick_member(dest, member_hint)
        if found:
            return found
        exe = _sh.which("7zz") or _sh.which("7z")
        if not exe:
            raise FileNotFoundError(
                f"{path.name}: need a 7-Zip binary (brew install sevenzip), "
                f"or pre-extract and pass the image path")
        dest.mkdir(parents=True, exist_ok=True)
        print(f"[cache] extracting {path.name} -> {dest}")
        _sp.run([exe, "x", "-y", f"-o{dest}", str(path)],
                check=True, capture_output=True)
        found = _pick_member(dest, member_hint)
        if not found:
            raise FileNotFoundError(
                f"{path.name}: no member matching {member_hint!r} inside")
        return found
    if path.suffix.lower() != ".zip":
        return path
    cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        if member_hint:
            picks = [i for i in members if member_hint.lower()
                     in i.filename.lower()]
            if not picks:
                raise FileNotFoundError(
                    f"no member matching {member_hint!r} in {path}")
            member = max(picks, key=lambda i: i.file_size)
        else:
            member = max(members, key=lambda i: i.file_size)
        # namespace by ARCHIVE stem (like the .7z branch): two different
        # discs can share a member basename -- e.g. the PlayStation and
        # Saturn rips of SF Collection (USA) Disc 2 both carry
        # "... (Track 1).bin" -- and a flat cache would overwrite one with
        # the other on every switch
        out = cache_dir / path.stem / Path(member.filename).name
        if out.exists() and out.stat().st_size == member.file_size:
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        print(f"[cache] extracting {member.filename!r} "
              f"({member.file_size / 1e6:.0f} MB) -> {out}")
        tmp = out.with_suffix(out.suffix + ".part")
        with zf.open(member) as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        tmp.rename(out)
        return out
