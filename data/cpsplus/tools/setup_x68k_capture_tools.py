#!/usr/bin/env python3
"""Fetch and build the pinned headless SC-55 renderer.

Third-party source, dependencies, and binaries are kept below ``--root``
(a work directory beside this tree by default) and are never part of any
download.  Needs git, cmake and a C++ compiler.  SC-55 firmware is not
downloaded; use a dump from hardware you own when running the renderer.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


class SetupError(RuntimeError):
    pass


RTMIDI_REPOSITORY = "https://github.com/thestk/rtmidi.git"
RTMIDI_COMMIT = "a3233c22949342f6697681e2cf2403e27fcf0c9e"
NUKED_REPOSITORY = "https://github.com/JohnMama12/Nuked-SC55-GUI-Float.git"
NUKED_COMMIT = "4b639496f7fab9d3fae8ddd5370e7ba8e785fa8b"


def _run(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        details = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise SetupError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{details}"
        )
    return result.stdout.strip()


def _checkout(path: Path, repository: str, commit: str) -> None:
    if not path.exists():
        _run(["git", "clone", "--no-checkout", repository, str(path)])
        _run(["git", "checkout", "--detach", commit], path)
    elif not (path / ".git").is_dir():
        raise SetupError(f"existing path is not a Git checkout: {path}")
    head = _run(["git", "rev-parse", "HEAD"], path)
    if head != commit:
        raise SetupError(
            f"{path} is at {head}, expected {commit}; move it aside rather "
            "than having this setup command overwrite it"
        )
    dirty = _run(["git", "status", "--porcelain", "--untracked-files=no"], path)
    if dirty:
        raise SetupError(f"tracked files are modified in {path}; move it aside")


def _generator(build: Path) -> list[str]:
    if (build / "CMakeCache.txt").is_file():
        return []
    return ["-G", "Ninja"] if shutil.which("ninja") else []


def setup(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rtmidi = root / "rtmidi"
    nuked = root / "Nuked-SC55-GUI-Float"
    _checkout(rtmidi, RTMIDI_REPOSITORY, RTMIDI_COMMIT)
    _checkout(nuked, NUKED_REPOSITORY, NUKED_COMMIT)

    rtmidi_build = rtmidi / "build"
    rtmidi_prefix = rtmidi / "install"
    _run([
        "cmake", "-S", str(rtmidi), "-B", str(rtmidi_build),
        *_generator(rtmidi_build),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DRTMIDI_BUILD_TESTING=OFF",
        f"-DCMAKE_INSTALL_PREFIX={rtmidi_prefix.resolve()}",
    ])
    _run(["cmake", "--build", str(rtmidi_build), "--target", "install"])

    nuked_build = nuked / "build"
    _run([
        "cmake", "-S", str(nuked), "-B", str(nuked_build),
        *_generator(nuked_build),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=ON",
        f"-DCMAKE_PREFIX_PATH={rtmidi_prefix.resolve()}",
        f"-DNUKED_SOURCE=JohnMama12/Nuked-SC55-GUI-Float@{NUKED_COMMIT[:12]}",
    ])
    _run([
        "cmake", "--build", str(nuked_build), "--target", "nuked-sc55-render"
    ])
    renderer = nuked_build / "nuked-sc55-render"
    if not renderer.is_file():
        raise SetupError(f"build succeeded but {renderer} is missing")
    version = _run([str(renderer), "--version"])
    print(f"ready: {renderer}\n{version}")
    return renderer


def self_test() -> None:
    assert len(RTMIDI_COMMIT) == 40
    assert len(NUKED_COMMIT) == 40
    assert RTMIDI_REPOSITORY.startswith("https://github.com/")
    assert NUKED_REPOSITORY.startswith("https://github.com/")
    print("setup_x68k_capture_tools self-test: OK")


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--root", type=Path,
        default=repo / "cpsplus" / "work" / "x68000" / "upstream",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    setup(args.root)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SetupError) as exc:
        print(f"setup_x68k_capture_tools: error: {exc}", file=sys.stderr)
        raise SystemExit(1)
