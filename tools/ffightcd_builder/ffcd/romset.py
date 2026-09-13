"""Read members out of the user's arcade romset, whatever form it takes.

MAME romsets turn up as .zip at least as often as .7z, split or merged, and
on Windows 7-Zip is 7z.exe -- often not on PATH at all.  Every read goes
through here so the build never names an archive format or a tool itself:

  * <stem>.zip is read with the standard zipfile module (no 7-Zip needed);
  * <stem>.7z goes through whichever 7-Zip is installed (7zz, 7z, 7za, 7zr,
    or 7-Zip's Windows install folder);
  * several stems are searched in order, so a clone's files are found in its
    own archive or, for a merged set, in the parent's;
  * anything missing is reported by member AND the archives searched,
    before the build gets a chance to fail somewhere less obvious.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


def find_7z() -> str | None:
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


def archives(romset: Path, stems) -> list[Path]:
    """The archives that exist for these stems, in search order."""
    found = []
    for stem in stems:
        for ext in (".zip", ".7z"):
            p = Path(romset) / f"{stem}{ext}"
            if p.is_file():
                found.append(p)
    return found


def _pick(names, member: str, clone: str) -> str | None:
    """The archive path for member.  A merged set stores a clone's files that
    share a name with the parent's under <clone>/, so that copy wins over the
    parent's same-named file; otherwise match the bare file name."""
    by_path = {n.replace("\\", "/").lower(): n for n in names}
    hit = by_path.get(f"{clone}/{member}".lower())
    if hit:
        return hit
    flat = [n for n in names if "/" not in n.replace("\\", "/")]
    for n in flat + list(names):
        if Path(n.replace("\\", "/")).name.lower() == member.lower():
            return n
    return None


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        return [n for n in z.namelist() if not n.endswith("/")]


def _7z_names(exe: str, path: Path) -> list[str]:
    r = subprocess.run([exe, "l", "-slt", "-ba", str(path)],
                       capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise SystemExit(f"{exe} could not list {path}:\n{r.stderr or r.stdout}")
    return [line[7:].strip() for line in r.stdout.splitlines() if line.startswith("Path = ")]


def read(romset: Path, stems, members) -> dict[str, bytes]:
    """{member: bytes} for every member, searching stems' archives in order."""
    members = list(members)
    stems = list(stems)
    clone = stems[0]
    got: dict[str, bytes] = {}
    arcs = archives(romset, stems)
    for arc in arcs:
        todo = [m for m in members if m not in got]
        if not todo:
            break
        if arc.suffix == ".zip":
            names = _zip_names(arc)
            with zipfile.ZipFile(arc) as z:
                for m in todo:
                    hit = _pick(names, m, clone)
                    if hit:
                        got[m] = z.read(hit)
        else:
            exe = find_7z()
            if exe is None:
                raise SystemExit(
                    f"{arc.name} is a .7z archive, which needs 7-Zip to read.\n"
                    f"  Install 7-Zip (https://www.7-zip.org), or supply the romset as "
                    f".zip files instead (no extra tools needed).")
            names = _7z_names(exe, arc)
            take = {m: _pick(names, m, clone) for m in todo}
            take = {m: h for m, h in take.items() if h}
            for m, hit in take.items():
                with tempfile.TemporaryDirectory() as td:
                    r = subprocess.run([exe, "x", "-y", f"-o{td}", str(arc), hit],
                                       capture_output=True, text=True, errors="replace")
                    if r.returncode:
                        raise SystemExit(f"{exe} failed reading {arc}:\n{r.stderr or r.stdout}")
                    p = Path(td) / hit
                    if p.is_file():
                        got[m] = p.read_bytes()
    missing = [m for m in members if m not in got]
    if missing:
        searched = ", ".join(a.name for a in arcs) or "none found"
        wanted = ", ".join(f"{s}.zip/.7z" for s in stems)
        raise SystemExit(
            f"romset is missing {', '.join(missing)}\n"
            f"  looked for: {wanted} in {romset}\n"
            f"  archives searched: {searched}")
    return got


def extract(romset: Path, stems, members, dest: Path) -> None:
    """Write members into dest (a drop-in for `7zz e -o<dest> <archive> ...`)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in read(romset, stems, members).items():
        (dest / name).write_bytes(data)
