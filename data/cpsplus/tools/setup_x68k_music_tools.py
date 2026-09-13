#!/usr/bin/env python3
"""Fetch and build the pinned open-source X68000 music extractors.

Third-party source and binaries are placed under ``--root`` (a work
directory beside this tree by default) and are never part of any download.
Existing checkouts at a different revision are rejected so this command never
overwrites somebody's local work.  Needs git, cmake and a C/C++ compiler
(clang, gcc, MinGW-w64 or Visual Studio); Ninja is used when present, and a
``CMAKE_GENERATOR`` set in the environment is respected.

The CMake helpers here are shared with setup_x68k_capture_tools.py.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class SetupError(RuntimeError):
    pass


EXE = ".exe" if os.name == "nt" else ""
CONFIGS = ("RELEASE", "DEBUG", "RELWITHDEBINFO", "MINSIZEREL")
COMPILERS = ("cc", "gcc", "clang", "cl")
# These checkouts are ours, made moments earlier.  Git refuses to operate in a
# repository it can't prove the user owns ("dubious ownership"), which is every
# repository on a filesystem that records no owner: exFAT/FAT32 drives and
# network shares.  Trusting just these commands is safe and changes nothing
# in the user's git configuration.
GIT = ("git", "-c", "safe.directory=*")


def absolute(path) -> Path:
    """An absolute path that keeps a mapped drive letter.

    On Windows, Path.resolve() rewrites a mapped drive (Z:\\...) to its
    network form (\\\\server\\share\\...), which MinGW gcc and CMake's
    compiler checks can't build in.  Paths handed to tools stay as the user
    wrote them, made absolute."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


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

# MidiConverters has no build system; this one-file project goes through the
# same CMake/compiler path as everything else.  GNU C99 keeps strcasecmp
# visible on gcc/MinGW; MSVC ignores the C standard request.
SINGLE_C_PROJECT = """\
cmake_minimum_required(VERSION 3.10)
project({target} LANGUAGES C)
set(CMAKE_C_STANDARD 99)
set(CMAKE_C_EXTENSIONS ON)
add_executable({target} "{source}")
"""


def _run(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        command, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        details = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise SetupError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{details}"
        )
    return result.stdout.strip()


def _excerpt(output: str, context: int = 4, limit: int = 40) -> str:
    """The lines that explain a CMake/compiler failure, not just the first."""
    lines = output.splitlines()
    for marker in (re.compile(r"CMake Error"), re.compile(r"(?i)\berror\b")):
        picked: list[str] = []
        last = -1
        for index, line in enumerate(lines):
            if marker.search(line) and index > last:
                if picked and index > last + 1:
                    picked.append("  ...")
                last = min(len(lines) - 1, index + context)
                picked.extend(lines[index:last + 1])
        if picked:
            return "\n".join(picked[:limit])
    return "\n".join(lines[-20:])


def _cmake(command: list[str], log: Path) -> None:
    result = subprocess.run(
        command, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"$ {' '.join(command)}\n{output}\n", encoding="utf-8")
    if result.returncode:
        raise SetupError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{_excerpt(output)}\nfull log: {log}"
        )


def have_compiler() -> bool:
    return bool(os.environ.get("CC") or any(shutil.which(c) for c in COMPILERS))


def cmake_generator(build: Path) -> list[str]:
    """Pick a generator for a fresh build directory.

    Ninja when it and a compiler are on PATH; MinGW Makefiles for a MinGW
    toolchain on Windows; otherwise CMake's own default (Visual Studio when
    MSVC is installed, Unix Makefiles elsewhere).  An existing cache keeps its
    generator, and CMAKE_GENERATOR in the environment wins over the choice.
    """
    if (build / "CMakeCache.txt").is_file() or os.environ.get("CMAKE_GENERATOR"):
        return []
    if shutil.which("ninja") and have_compiler():
        return ["-G", "Ninja"]
    if os.name == "nt" and shutil.which("gcc") and shutil.which("mingw32-make"):
        return ["-G", "MinGW Makefiles"]
    return []


def cmake_build(
    source: Path, build: Path, target: str,
    options: tuple[str, ...] = (), output: Path | None = None,
) -> None:
    """Configure (Release) and build one target.  Executables land directly in
    ``output`` whatever the generator, so single- and multi-config builds put
    them in the same place."""
    # A configure that never generated (no compiler, unusable generator)
    # leaves a cache that would pin its generator; start that directory over.
    generated = (
        (build / "build.ninja").is_file() or (build / "Makefile").is_file()
        or any(build.glob("*.sln")) or any(build.glob("*.xcodeproj"))
    )
    if (build / "CMakeCache.txt").is_file() and not generated:
        (build / "CMakeCache.txt").unlink()
        shutil.rmtree(build / "CMakeFiles", ignore_errors=True)
    runtime: list[str] = []
    if output is not None:
        runtime.append(f"-DCMAKE_RUNTIME_OUTPUT_DIRECTORY={absolute(output).as_posix()}")
        runtime.extend(
            f"-DCMAKE_RUNTIME_OUTPUT_DIRECTORY_{config}={absolute(output).as_posix()}"
            for config in CONFIGS
        )
    # Pinned upstream revisions declare old cmake_minimum_required values;
    # CMake 4 refuses those without an explicit policy floor.
    _cmake([
        "cmake", "-S", str(source), "-B", str(build),
        *cmake_generator(build),
        "-Wno-deprecated", "--no-warn-unused-cli",
        "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        "-DCMAKE_BUILD_TYPE=Release",
        *runtime,
        *options,
    ], build / "cpsplus-configure.log")
    _cmake([
        "cmake", "--build", str(build), "--config", "Release", "--target", target,
    ], build / "cpsplus-build.log")


def find_binary(directory: Path, name: str) -> Path | None:
    """Locate a built executable: ``directory`` itself, then ``bin/``, then
    anywhere below it except CMake's scratch and Debug configurations."""
    filename = name + EXE
    for candidate in (directory / filename, directory / "bin" / filename):
        if candidate.is_file():
            return candidate
    if not directory.is_dir():
        return None
    skip = {"CMakeFiles", "Debug", "RelWithDebInfo"}
    matches = sorted(
        (path for path in directory.rglob(filename)
         if path.is_file() and not skip & set(path.relative_to(directory).parts)),
        key=lambda path: ("Release" not in path.parts, len(path.parts), str(path)),
    )
    return matches[0] if matches else None


def require_binary(directory: Path, name: str) -> Path:
    binary = find_binary(directory, name)
    if binary is None:
        raise SetupError(
            f"build succeeded but {name + EXE} is missing: looked in "
            f"{directory}, {directory / 'bin'} and below {directory}"
        )
    return binary


def setup(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not have_compiler() and os.name != "nt":
        # On Windows CMake can find Visual Studio without cl on PATH.
        raise SetupError("no C compiler found (cc/gcc/clang, or set CC)")
    for tool in TOOLS:
        source = root / tool.name
        if not source.exists():
            _run([*GIT, "clone", "--no-checkout", tool.repository, str(source)])
            _run([*GIT, "checkout", "--detach", tool.commit], source)
        elif not (source / ".git").is_dir():
            raise SetupError(f"existing path is not a Git checkout: {source}")
        head = _run([*GIT, "rev-parse", "HEAD"], source)
        if head != tool.commit:
            raise SetupError(
                f"{source} is at {head}, expected {tool.commit}; move it aside "
                "rather than having this setup command overwrite it"
            )
        build = source / "build"
        build.mkdir(parents=True, exist_ok=True)
        if tool.build == "cmake":
            cmake_build(source, build, tool.target, output=build)
        elif tool.build == "single-c":
            project = build / "project"
            project.mkdir(parents=True, exist_ok=True)
            (project / "CMakeLists.txt").write_text(SINGLE_C_PROJECT.format(
                target=tool.target,
                source=absolute(source / f"{tool.target}.c").as_posix(),
            ))
            cmake_build(project, build / "cmake", tool.target, output=build)
        else:
            raise SetupError(f"unknown build method {tool.build}")
        binary = require_binary(build, tool.target)
        print(f"ready: {binary} ({tool.commit[:12]})")


def self_test() -> None:
    assert len(TOOLS) == 2
    assert all(len(tool.commit) == 40 for tool in TOOLS)
    assert {tool.target for tool in TOOLS} == {"x68k_sps_dec", "m2seq22mid"}
    log = "-- ok\nCMake Error at CMakeLists.txt:2 (project):\n  No CMAKE_C_COMPILER\n\n\n\n-- done"
    assert _excerpt(log).startswith("CMake Error at")
    assert "No CMAKE_C_COMPILER" in _excerpt(log)
    assert "fatal error: x.h" in _excerpt("-Werror\nb\nfoo.c:1: fatal error: x.h\n")
    assert "-Werror" not in _excerpt("-Werror\nb\nfoo.c:1: fatal error: x.h\n")
    assert "project(m2seq22mid LANGUAGES C)" in SINGLE_C_PROJECT.format(
        target="m2seq22mid", source="m2seq22mid.c")
    with tempfile.TemporaryDirectory() as temporary:
        # A multi-config tree: Release wins, a Debug-only build is not reused.
        build = Path(temporary)
        (build / "Debug").mkdir()
        (build / "Debug" / f"tool{EXE}").write_bytes(b"")
        assert find_binary(build, "tool") is None
        (build / "x" / "Release").mkdir(parents=True)
        (build / "x" / "Release" / f"tool{EXE}").write_bytes(b"")
        assert find_binary(build, "tool") == build / "x" / "Release" / f"tool{EXE}"
        (build / f"tool{EXE}").write_bytes(b"")
        assert find_binary(build, "tool") == build / f"tool{EXE}"
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
