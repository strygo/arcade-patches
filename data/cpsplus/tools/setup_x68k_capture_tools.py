#!/usr/bin/env python3
"""Fetch and build the pinned headless SC-55 renderer.

Third-party source, dependencies, and binaries are kept below ``--root``
(a work directory beside this tree by default) and are never part of any
download.  Needs git, cmake and a C++ compiler; the build goes through the
same CMake helpers as setup_x68k_music_tools.py.  SC-55 firmware is not
downloaded; use a dump from hardware you own when running the renderer.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from setup_x68k_music_tools import (
    GIT, SetupError, _run, absolute, cmake_build, require_binary,
)


RTMIDI_REPOSITORY = "https://github.com/thestk/rtmidi.git"
RTMIDI_COMMIT = "a3233c22949342f6697681e2cf2403e27fcf0c9e"
NUKED_REPOSITORY = "https://github.com/JohnMama12/Nuked-SC55-GUI-Float.git"
NUKED_COMMIT = "4b639496f7fab9d3fae8ddd5370e7ba8e785fa8b"
RTMIDI_NEEDED = os.name != "nt"


def _checkout(path: Path, repository: str, commit: str) -> None:
    if not path.exists():
        _run([*GIT, "clone", "--no-checkout", repository, str(path)])
        _run([*GIT, "checkout", "--detach", commit], path)
    elif not (path / ".git").is_dir():
        raise SetupError(f"existing path is not a Git checkout: {path}")
    head = _run([*GIT, "rev-parse", "HEAD"], path)
    if head != commit:
        raise SetupError(
            f"{path} is at {head}, expected {commit}; move it aside rather "
            "than having this setup command overwrite it"
        )
    dirty = _run([*GIT, "status", "--porcelain", "--untracked-files=no"], path)
    if dirty:
        raise SetupError(f"tracked files are modified in {path}; move it aside")


def setup(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    nuked = root / "Nuked-SC55-GUI-Float"
    _checkout(nuked, NUKED_REPOSITORY, NUKED_COMMIT)
    options = [
        "-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=ON",
        f"-DNUKED_SOURCE=JohnMama12/Nuked-SC55-GUI-Float@{NUKED_COMMIT[:12]}",
    ]
    # The pinned Nuked-SC55 requires rtmidi on every platform except Windows
    # (USE_RTMIDI is set only if NOT WIN32), and only its SDL frontend links
    # it; the headless renderer never does.
    if RTMIDI_NEEDED:
        rtmidi = root / "rtmidi"
        _checkout(rtmidi, RTMIDI_REPOSITORY, RTMIDI_COMMIT)
        rtmidi_prefix = rtmidi / "install"
        cmake_build(rtmidi, rtmidi / "build", "install", options=(
            "-DBUILD_SHARED_LIBS=OFF",
            "-DRTMIDI_BUILD_TESTING=OFF",
            f"-DCMAKE_INSTALL_PREFIX={absolute(rtmidi_prefix).as_posix()}",
        ))
        options.append(f"-DCMAKE_PREFIX_PATH={absolute(rtmidi_prefix).as_posix()}")

    nuked_build = nuked / "build"
    cmake_build(nuked, nuked_build, "nuked-sc55-render",
                options=tuple(options), output=nuked_build)
    renderer = require_binary(nuked_build, "nuked-sc55-render")
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
