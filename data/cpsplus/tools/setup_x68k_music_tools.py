#!/usr/bin/env python3
"""Fetch and build the pinned open-source X68000 music extractors.

Third-party source and binaries are placed under ``--root`` (a work
directory beside this tree by default) and are never part of any download.
Existing checkouts at a different revision are rejected so this command never
overwrites somebody's local work.  Needs git, cmake and a C compiler.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import subprocess
import sys
from pathlib import Path


class SetupError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Tool:
    name: str
    repository: str
    commit: str
    target: str
    build: str


TOOLS = (
    Tool(
        "ExtractorsDecoders",
        "https://github.com/ValleyBell/ExtractorsDecoders.git",
        "822cf4f559e2130544afc8be763f897098283f46",
        "x68k_sps_dec",
        "cmake",
    ),
    Tool(
        "MidiConverters",
        "https://github.com/ValleyBell/MidiConverters.git",
        "a73c748960d57bd194fb64931ce07d2c1fa1bd01",
        "m2seq22mid",
        "single-c",
    ),
)


def _run(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        details = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise SetupError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{details}"
        )
    return result.stdout.strip()


def setup(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for tool in TOOLS:
        source = root / tool.name
        if not source.exists():
            _run(["git", "clone", "--no-checkout", tool.repository, str(source)])
            _run(["git", "checkout", "--detach", tool.commit], source)
        elif not (source / ".git").is_dir():
            raise SetupError(f"existing path is not a Git checkout: {source}")
        head = _run(["git", "rev-parse", "HEAD"], source)
        if head != tool.commit:
            raise SetupError(
                f"{source} is at {head}, expected {tool.commit}; move it aside "
                "rather than having this setup command overwrite it"
            )
        build = source / "build"
        build.mkdir(parents=True, exist_ok=True)
        if tool.build == "cmake":
            # The pinned revision declares an old cmake_minimum_required;
            # CMake 4 refuses those without an explicit policy floor.
            _run(["cmake", "-S", str(source), "-B", str(build),
                  "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"])
            _run(["cmake", "--build", str(build), "--target", tool.target])
        elif tool.build == "single-c":
            compiler = os.environ.get("CC") or shutil.which("cc")
            if not compiler:
                raise SetupError("no C compiler found (set CC)")
            _run([
                compiler, "-O2", "-std=c99", str(source / f"{tool.target}.c"),
                "-o", str(build / tool.target),
            ])
        else:
            raise SetupError(f"unknown build method {tool.build}")
        binary = build / tool.target
        if not binary.is_file():
            raise SetupError(f"build succeeded but {binary} is missing")
        print(f"ready: {binary} ({tool.commit[:12]})")


def self_test() -> None:
    assert len(TOOLS) == 2
    assert all(len(tool.commit) == 40 for tool in TOOLS)
    assert {tool.target for tool in TOOLS} == {"x68k_sps_dec", "m2seq22mid"}
    print("setup_x68k_music_tools self-test: OK")


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
        print(f"setup_x68k_music_tools: error: {exc}", file=sys.stderr)
        raise SystemExit(1)
