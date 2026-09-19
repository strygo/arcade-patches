#!/usr/bin/env python3
"""Batch-render extracted X68000 MIDI arrangements through an SC-55.

The input MIDI files already contain a Roland GS reset and setup window.  A
native GS sequence uses 500 ms; an MT-32 sequence uses the SC-55's compatible
tone map and a six-second initialization.  This command uses Nuked-SC55's
offline renderer, records firmware and output hashes, and emits sample-indexed
steady-state loop candidates.  Supply firmware dumped from hardware you own;
this command never downloads ROMs.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from x68k_midi import audit, parse_midi, tick_to_seconds


class RenderError(RuntimeError):
    pass


SETTLE_LOOP_PASSES = 1
EXE = ".exe" if os.name == "nt" else ""

ROM_FILES = {
    "mk1": (
        "sc55_rom1.bin", "sc55_rom2.bin", "sc55_waverom1.bin",
        "sc55_waverom2.bin", "sc55_waverom3.bin",
    ),
    "mk2": ("rom1.bin", "rom2.bin", "rom_sm.bin", "waverom1.bin", "waverom2.bin"),
}

# Hashes published with Nuked-SC55's integration-test firmware sets.
KNOWN_ROMSETS = {
    "SC-55 v1.21": {
        "sc55_rom1.bin": "7e1bacd1d7c62ed66e465ba05597dcd60dfc13fc23de0287fdbce6cf906c6544",
        "sc55_rom2.bin": "effc6132d68f7e300aaef915ccdd08aba93606c22d23e580daf9ea6617913af1",
        "sc55_waverom1.bin": "5655509a531804f97ea2d7ef05b8fec20ebf46216b389a84c44169257a4d2007",
        "sc55_waverom2.bin": "c655b159792d999b90df9e4fa782cf56411ba1eaa0bb3ac2bdaf09e1391006b1",
        "sc55_waverom3.bin": "334b2d16be3c2362210fdbec1c866ad58badeb0f84fd9bf5d0ac599baf077cc2",
    },
    "SC-55mkII v1.01": {
        "rom1.bin": "8a1eb33c7599b746c0c50283e4349a1bb1773b5c0ec0e9661219bf6c067d2042",
        "rom2.bin": "a4c9fd821059054c7e7681d61f49ce6f42ed2fe407a7ec1ba0dfdc9722582ce0",
        "rom_sm.bin": "b0b5f865a403f7308b4be8d0ed3ba2ed1c22db881b8a8326769dea222f6431d8",
        "waverom1.bin": "c6429e21b9b3a02fbd68ef0b2053668433bee0bccd537a71841bc70b8874243b",
        "waverom2.bin": "5b753f6cef4cfc7fcafe1430fecbb94a739b874e55356246a46abe24097ee491",
    },
}


def absolute(path) -> Path:
    """An absolute path that keeps a mapped drive letter: on Windows,
    Path.resolve() rewrites Z:\\... to \\\\server\\share\\..., which the
    renderer and its ROM loader can't always open."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve(repo: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo / path


def _display_path(repo: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(path.resolve())


def _run(command: list[str]) -> str:
    result = subprocess.run(
        command, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        details = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise RenderError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{details}"
        )
    return result.stdout.strip()


def _wav_info(path: Path) -> dict[str, int]:
    with path.open("rb") as source:
        header = source.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise RenderError(f"renderer did not produce a RIFF/WAVE file: {path}")
        fmt: tuple[int, int, int, int, int] | None = None
        data_size: int | None = None
        while True:
            chunk = source.read(8)
            if not chunk:
                break
            if len(chunk) != 8:
                raise RenderError(f"truncated WAVE chunk header: {path}")
            kind, size = struct.unpack("<4sI", chunk)
            if kind == b"fmt " and size >= 16:
                payload = source.read(size)
                if len(payload) != size:
                    raise RenderError(f"truncated WAVE fmt chunk: {path}")
                encoding, channels, rate, _, block_align, bits = struct.unpack(
                    "<HHIIHH", payload[:16]
                )
                fmt = (encoding, channels, rate, block_align, bits)
            elif kind == b"data":
                data_size = size
                source.seek(size, 1)
            else:
                source.seek(size, 1)
            if size & 1:
                source.seek(1, 1)
        if fmt is None or data_size is None:
            raise RenderError(f"WAVE file lacks fmt/data chunks: {path}")
    encoding, channels, rate, block_align, bits = fmt
    if not block_align or data_size % block_align:
        raise RenderError(f"misaligned WAVE data: {path}")
    return {
        "encoding": encoding,
        "channels": channels,
        "sample_rate": rate,
        "bits_per_sample": bits,
        "frames": data_size // block_align,
    }


def _firmware(rom_dir: Path, romset: str) -> tuple[dict[str, str], str]:
    required = ROM_FILES[romset]
    missing = [name for name in required if not (rom_dir / name).is_file()]
    if missing:
        raise RenderError(
            f"{romset} firmware is incomplete in {rom_dir}; missing: "
            + ", ".join(missing)
        )
    hashes = {name: _sha256(rom_dir / name) for name in required}
    revision = next(
        (name for name, expected in KNOWN_ROMSETS.items() if hashes == expected),
        "unrecognized firmware revision",
    )
    return hashes, revision


def _steady_ticks(prepared_loop: dict[str, object]) -> tuple[int, int]:
    period = int(prepared_loop["period_ticks"])
    start = int(prepared_loop["start_tick"]) + SETTLE_LOOP_PASSES * period
    return start, start + period


def _loop_candidate(
    loop: dict[str, object] | None, rate: int, midi_path: Path
) -> dict[str, object] | None:
    if loop is None:
        return None
    prepared_audit = audit(midi_path)
    prepared_loop = prepared_audit["global_loop"]
    if prepared_loop is None:
        raise RenderError(f"prepared MIDI lost its source loop: {midi_path}")
    midi = parse_midi(midi_path.read_bytes())
    start_tick, end_tick = _steady_ticks(prepared_loop)
    start = tick_to_seconds(midi, start_tick)
    end = tick_to_seconds(midi, end_tick)
    source_start = float(loop["start_seconds"])
    preroll = float(prepared_loop["start_seconds"]) - source_start
    return {
        "strategy": "second rendered pass after one effects-settling pass",
        "preroll_seconds": round(preroll, 9),
        "settled_passes": SETTLE_LOOP_PASSES,
        "start_seconds": start,
        "end_seconds": end,
        "period_seconds": end - start,
        "start_tick": start_tick,
        "end_tick": end_tick,
        "start_sample": round(start * rate),
        "end_sample": round(end * rate),
        "prepared_first_pass_loop": prepared_loop,
        "source_loop": loop,
    }


def _link_firmware(source: Path, link: Path) -> None:
    # Symlinks need Developer Mode or elevation on Windows; a hard link (same
    # volume) or a copy of the few MB of firmware isolates the song as well.
    try:
        link.symlink_to(source)
    except OSError:
        try:
            os.link(source, link)
        except OSError:
            shutil.copy2(source, link)


def _renderer_command(
    args: argparse.Namespace, renderer: Path, rom_dir: Path,
    output: Path, source: Path,
) -> list[str]:
    return [
        str(renderer),
        "--romset", args.romset,
        "--rom-directory", str(absolute(rom_dir)),
        "--reset", "none",
        "--format", args.format,
        "--end", args.end,
        "-o", str(absolute(output)),
        str(absolute(source)),
    ]


def render(args: argparse.Namespace, repo: Path) -> Path:
    catalog_path = absolute(args.catalog)
    if not catalog_path.is_file():
        raise RenderError(f"extraction catalog is missing: {catalog_path}")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("schema") != "cpsplus-x68000-audio-v1":
        raise RenderError(f"unsupported extraction catalog: {catalog_path}")
    renderer = absolute(args.renderer)
    if not renderer.is_file():
        raise RenderError(
            f"SC-55 renderer is missing: {renderer}; build it with "
            "setup_x68k_capture_tools.py"
        )
    version = _run([str(renderer), "--version"])
    firmware_hashes, firmware_revision = _firmware(absolute(args.rom_dir), args.romset)

    games = [
        game for game in catalog["games"]
        if not args.game or game["game"] in args.game
    ]
    if args.game:
        found = {game["game"] for game in games}
        missing = sorted(set(args.game) - found)
        if missing:
            raise RenderError("games not in catalog: " + ", ".join(missing))
    selected: list[tuple[dict[str, object], dict[str, object]]] = []
    for game in games:
        for row in game.get("loop_points", {}).get("midi", []):
            if args.song and row["song"] not in args.song:
                continue
            selected.append((game, row))
    if args.song:
        found_songs = {row["song"] for _, row in selected}
        missing = sorted(set(args.song) - found_songs)
        if missing:
            raise RenderError("songs not in selection: " + ", ".join(missing))
    if args.limit is not None:
        selected = selected[:args.limit]
    if not selected:
        raise RenderError("selection contains no SC-55 songs")
    if int(catalog.get("finite_loop_passes", 0)) < 3 and any(
        row.get("loop") is not None for _, row in selected
    ):
        raise RenderError(
            "looped MIDI was exported with fewer than three passes; rerun "
            "extract_x68k_music.py --loops 3 before capture"
        )

    args.output = absolute(args.output)
    args.output.mkdir(parents=True, exist_ok=True)
    output_catalog = args.output / "catalog.json"
    captures_by_song: dict[tuple[str, str], dict[str, object]] = {}
    if output_catalog.is_file():
        previous = json.loads(output_catalog.read_text(encoding="utf-8"))
        if (
            previous.get("schema") != "cpsplus-x68000-sc55-capture-v1"
            or previous.get("romset") != args.romset
            or previous.get("firmware_sha256") != firmware_hashes
            or previous.get("renderer_version") != version
        ):
            raise RenderError(
                f"existing capture catalog uses a different renderer/firmware: "
                f"{output_catalog}; choose another --output directory"
            )
        # Keep incremental captures only while their prepared MIDI still
        # matches the current extraction catalog.  A changed reset/sound-map
        # wrapper must not leave an obsolete render advertised as current.
        current_inputs = {
            (str(game["game"]), str(row["song"])): row["sc55"]
            for game in catalog["games"]
            for row in game.get("loop_points", {}).get("midi", [])
        }
        for record in previous.get("captures", []):
            key = (str(record["game"]), str(record["song"]))
            current = current_inputs.get(key)
            if current is None:
                continue
            if record.get("input_sha256") != current.get("sha256"):
                continue
            if record.get("sound_map", "gs") != current.get("sound_map", "gs"):
                continue
            captures_by_song[key] = record
    jobs: list[tuple[dict[str, object], dict[str, object], Path, str, Path]] = []
    for game, row in selected:
        source = _resolve(repo, row["sc55"]["path"])
        if not source.is_file():
            raise RenderError(f"prepared MIDI is missing: {source}")
        input_hash = _sha256(source)
        if input_hash != row["sc55"]["sha256"]:
            raise RenderError(f"prepared MIDI hash mismatch: {source}")
        game_dir = args.output / str(game["game"])
        game_dir.mkdir(parents=True, exist_ok=True)
        output = game_dir / f"{row['song']}.wav"
        sidecar = output.with_suffix(".json")
        command = _renderer_command(
            args, renderer, args.rom_dir, output, source
        )
        if args.dry_run:
            print(" ".join(command))
            continue
        if output.exists() or sidecar.exists():
            if args.overwrite:
                pass
            elif not output.is_file() or not sidecar.is_file():
                # An interrupted run leaves a WAV without its sidecar (or the
                # reverse).  Neither half is trustworthy alone: render again.
                print(f"redoing interrupted capture {game['game']}/{row['song']}",
                      flush=True)
                output.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
            else:
                existing = json.loads(sidecar.read_text(encoding="utf-8"))
                reusable = (
                    existing.get("input_sha256") == input_hash
                    and existing.get("renderer_version") == version
                    and existing.get("romset") == args.romset
                    and existing.get("firmware_sha256") == firmware_hashes
                    and existing.get("format") == args.format
                    and existing.get("end_behavior") == args.end
                    and existing.get("output_sha256") == _sha256(output)
                )
                if not reusable:
                    raise RenderError(
                        f"prior capture does not match this run: {output}; "
                        "use --overwrite"
                    )
                print(f"reusing {game['game']}/{row['song']}", flush=True)
                captures_by_song[(str(game["game"]), str(row["song"]))] = existing
                continue
        jobs.append((game, row, source, input_hash, output))
    if args.dry_run:
        return args.output / "catalog.json"

    def render_one(job) -> dict[str, object]:
        game, row, source, input_hash, output = job
        print(f"rendering {game['game']}/{row['song']} ...", flush=True)
        # Nuked-SC55 persists module SRAM beside its ROMs.  Give every song a
        # private writable directory containing the firmware (links where the
        # OS allows, else copies) so a preceding (or concurrent) capture can
        # never change another song's initial state.
        with tempfile.TemporaryDirectory(prefix="cpsplus-sc55-") as temporary:
            isolated_roms = Path(temporary)
            for name in ROM_FILES[args.romset]:
                _link_firmware(absolute(args.rom_dir) / name, isolated_roms / name)
            _run(_renderer_command(
                args, renderer, isolated_roms, output, source
            ))
        wav = _wav_info(output)
        loop = _loop_candidate(row.get("loop"), wav["sample_rate"], source)
        if loop is not None and int(loop["end_sample"]) > wav["frames"]:
            raise RenderError(
                f"render ended before steady loop boundary for {output}: "
                f"need {loop['end_sample']} frames, got {wav['frames']}"
            )
        record: dict[str, object] = {
            "game": game["game"],
            "song": row["song"],
            "input": _display_path(repo, source),
            "input_sha256": input_hash,
            "sound_map": row["sc55"].get("sound_map", "gs"),
            "output": _display_path(repo, output),
            "output_sha256": _sha256(output),
            "wav": wav,
            "loop_status": row["loop_status"],
            "loop_candidate": loop,
            "renderer_version": version,
            "romset": args.romset,
            "firmware_revision": firmware_revision,
            "firmware_sha256": firmware_hashes,
            "format": args.format,
            "end_behavior": args.end,
        }
        output.with_suffix(".json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
        return record

    # Every render is independent (own MIDI, own firmware directory, own
    # output), so a pool of renderer processes is safe; the output is
    # identical whatever the order or degree of parallelism.
    workers = max(1, int(args.jobs))
    if workers == 1 or len(jobs) <= 1:
        records = [render_one(job) for job in jobs]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            records = list(pool.map(render_one, jobs))
    for record in records:
        captures_by_song[(str(record["game"]), str(record["song"]))] = record
    captures = [captures_by_song[key] for key in sorted(captures_by_song)]
    output_catalog.write_text(json.dumps({
        "schema": "cpsplus-x68000-sc55-capture-v1",
        "source_catalog": _display_path(repo, catalog_path),
        "renderer_version": version,
        "romset": args.romset,
        "firmware_revision": firmware_revision,
        "firmware_sha256": firmware_hashes,
        "captures": captures,
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"captured {len(captures)} songs; catalog: {output_catalog}")
    return output_catalog


def self_test() -> None:
    assert _steady_ticks({"start_tick": 48, "period_ticks": 9600}) == (
        9648, 19248
    )
    assert set(ROM_FILES) == {"mk1", "mk2"}
    with tempfile.TemporaryDirectory() as temporary:
        # Without symlink or hard-link permission the firmware is copied.
        rom = Path(temporary) / "rom.bin"
        rom.write_bytes(b"sc55")
        def refuse(*_):
            raise OSError(1314, "A required privilege is not held by the client")
        symlink_to, link = Path.symlink_to, os.link
        Path.symlink_to = os.link = refuse
        try:
            _link_firmware(rom, Path(temporary) / "copy.bin")
        finally:
            Path.symlink_to, os.link = symlink_to, link
        assert (Path(temporary) / "copy.bin").read_bytes() == b"sc55"
        assert not (Path(temporary) / "copy.bin").is_symlink()
    print("render_x68k_sc55 self-test: OK")


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[2]
    upstream = repo / "cpsplus" / "work" / "x68000" / "upstream"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--catalog", type=Path,
        default=repo / "cpsplus" / "work" / "x68000" / "export" / "catalog.json",
    )
    parser.add_argument(
        "--renderer", type=Path,
        default=upstream / "Nuked-SC55-GUI-Float" / "build" / f"nuked-sc55-render{EXE}",
    )
    parser.add_argument(
        "--rom-dir", type=Path, default=repo / "roms" / "sc55",
        help="directory containing a user-supplied SC-55 ROM dump",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--romset", choices=sorted(ROM_FILES), default="mk1")
    parser.add_argument("--format", choices=("s16", "s32", "f32"), default="f32")
    parser.add_argument("--end", choices=("cut", "release"), default="cut")
    parser.add_argument("--game", action="append")
    parser.add_argument("--song", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--jobs", type=int, default=1,
                        help="render this many songs at once (default: 1)")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least one")
    if args.jobs < 1:
        parser.error("--jobs must be at least one")
    if args.output is None:
        args.output = (
            repo / "cpsplus" / "work" / "x68000" / "capture" / "sc55"
            / args.romset
        )
    render(args, repo)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, json.JSONDecodeError, RenderError) as exc:
        print(f"render_x68k_sc55: error: {exc}", file=sys.stderr)
        raise SystemExit(1)
