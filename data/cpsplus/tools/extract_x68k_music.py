#!/usr/bin/env python3
"""Extract CPS+ music assets from X68000 disk-image archives you own.

Each game's archive is a .zip (or, with a 7zz/7z binary on PATH, a .7z)
holding that game's DIM/XDF floppy images.  Pass it explicitly with
``--archive SLUG=PATH``; without that, the archive is looked up by name under
``--rom-root``.  The output contains game data and must never be shared.

Four S.P.S. M2/M3 games yield separate FM and external-MIDI sequence banks.
MIDI-bank songs are normalized for Roland SC-55 playback with the
appropriate GS or MT-32-compatible setup, while preserving native loop
points in a JSON catalog.  Strider uses a custom FM driver, so its native
sound bank and driver are preserved but are not mislabeled as Standard MIDI.

Final Fight's external sequence bank targets an MT-32.  Its SC-55 playback
copies therefore select the SC-55's compatible MT-32 tone map instead of
misreading those patch numbers as GS instruments.  This script calls Valley
Bell's ``x68k_sps_dec`` and ``m2seq22mid`` tools, built at pinned revisions
by ``setup_x68k_music_tools.py``.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

from x68k_disk import DiskError, Human68kDisk
from x68k_midi import MidiError, audit, expand_m3_loops, prepare_sc55


class ExtractionError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Game:
    slug: str
    title: str
    archive_stem: str
    engine: str
    fm_bank: str
    midi_bank: str | None
    fm_driver_files: tuple[str, ...]
    midi_driver_files: tuple[str, ...]
    support_files: tuple[str, ...] = ()
    fm_format: str | None = None
    midi_format: str | None = None
    # sha256 of the MIDI bank the published packs were rendered from.  A rip
    # with a different bank fails here, by name, instead of hours later at
    # the pack hash.
    midi_bank_sha256: str | None = None


GAMES = (
    Game(
        "daimakaimura", "Daimakaimura", "Daimakaimura", "M3",
        "TEXTDAT2.SLD", "TEXTDAT4.SLD",
        ("M3.X", "OPMDMK.X"), ("M3.X", "SEQSTD.X"),
        (), "SLD-DM", "SLD-DM",
        "e379bcaa14630c5c2ec94725a16807737599648663e45af4619e14a1d06c6961",
    ),
    Game(
        "ffight", "Final Fight", "Final Fight", "M2 sequencer-2",
        "BGM.SLD", "BGM_MIDI.SLD",
        ("M2.X", "OPM3FF.X", "M2MOP.X", "M2MOPC.X", "MOPV.X"),
        ("M2.X", "SEQ2.X"), ("DRUMS.MOP",), "SLD-FF", "SLD-FF",
    ),
    Game(
        "sf2ce", "Street Fighter II: Champion Edition",
        "Street Fighter II Champion Edition", "M3",
        "FM.BLK", "GM.BLK",
        ("M3.X", "OPMSF2.X", "ADPCM1.X", "ADPCM4.X"),
        ("M3.X", "SEQSTD.X"), ("SF2.DRM",), "BLK-SF2", "BLK-SF2",
        "e65bda985bedaee606a895a39ef44566cf4121dd08743669e35f23602289b995",
    ),
    Game(
        "ssf2", "Super Street Fighter II: The New Challengers",
        "Super Street Fighter II", "M3",
        "FM.BLK", "GM.BLK",
        ("M3.X", "OPMSP2.X", "ADP1SP2.X", "ADP4SP2.X"),
        ("M3.X", "SEQSP2.X"), ("SSF2_X68.DRM",), "BLK-SF2", "BLK-SF2",
        "7d0165a36a6ffd9435b93602940a4b32e2a843885f22895873852ffddf2d8c74",
    ),
    Game(
        "strider", "Strider Hiryu", "Strider Hiryuu", "custom",
        "SHSOUND.SLD", None,
        ("STR.X", "ADPCM2.X", "SHSOUND.SLD"), (), (), None, None,
    ),
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        details = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise ExtractionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{details}"
        )


def _find_archive(root: Path, stem: str) -> Path:
    candidates = sorted(
        path for path in root.iterdir()
        if path.is_file()
        and path.suffix.lower() in (".zip", ".7z")
        and path.name.casefold().startswith(stem.casefold())
    )
    if len(candidates) != 1:
        raise ExtractionError(
            f"expected exactly one {stem!r} archive in {root}, found "
            f"{len(candidates)}"
        )
    return candidates[0]


def _seven_zip_members(archive: Path, seven_zip: str) -> list[str]:
    result = subprocess.run(
        [seven_zip, "l", "-slt", str(archive)], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise ExtractionError(result.stderr.strip() or f"cannot list {archive}")
    members = [
        line[7:] for line in result.stdout.splitlines() if line.startswith("Path = ")
    ]
    return [name for name in members if Path(name).suffix.lower() in (".dim", ".xdf")]


def _image_members(archive: Path, seven_zip: str | None) -> list[tuple[str, bytes]]:
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as container:
            return [
                (info.filename, container.read(info))
                for info in container.infolist()
                if not info.is_dir()
                and PurePosixPath(info.filename).suffix.lower() in (".dim", ".xdf")
            ]
    executable = seven_zip or shutil.which("7zz") or shutil.which("7z")
    if executable is None:
        raise ExtractionError(
            f"{archive.name} is a .7z archive, which needs a 7zz/7z binary on "
            "PATH; a .zip of the same disk images needs nothing extra"
        )
    output: list[tuple[str, bytes]] = []
    for member in _seven_zip_members(archive, executable):
        result = subprocess.run(
            [executable, "x", "-so", str(archive), member], capture_output=True
        )
        if result.returncode:
            raise ExtractionError(
                result.stderr.decode(errors="replace").strip()
                or f"cannot extract {member} from {archive}"
            )
        output.append((member, result.stdout))
    return output


def _collect_files(
    archive: Path, seven_zip: str | None
) -> tuple[dict[str, tuple[bytes, str]], list[dict[str, object]]]:
    files: dict[str, tuple[bytes, str]] = {}
    images: list[dict[str, object]] = []
    for member, image_data in _image_members(archive, seven_zip):
        try:
            disk = Human68kDisk(image_data, f"{archive}:{member}")
        except DiskError as exc:
            raise ExtractionError(f"cannot read {archive}:{member}: {exc}") from exc
        image_row: dict[str, object] = {
            "member": member,
            "size": len(image_data),
            "sha256": _sha256(image_data),
            "filesystem_offset": disk.geometry.image_offset,
        }
        count = 0
        for entry in disk.entries():
            if entry.is_dir:
                continue
            count += 1
            key = entry.path.name.upper()
            # Driver/data basenames are unique within each game archive.  A
            # duplicate with different bytes is an ambiguity, never a guess.
            content = disk.read(entry)
            if key in files and files[key][0] != content:
                raise ExtractionError(
                    f"conflicting {key} files in {archive}: "
                    f"{files[key][1]} and {member}:{entry.path}"
                )
            files[key] = (content, f"{member}:{entry.path.as_posix()}")
        image_row["file_count"] = count
        images.append(image_row)
    if not images:
        raise ExtractionError(f"no DIM/XDF images found in {archive}")
    return files, images


def _require(files: dict[str, tuple[bytes, str]], name: str, archive: Path) -> bytes:
    row = files.get(name.upper())
    if row is None:
        raise ExtractionError(f"{archive} does not contain required file {name}")
    return row[0]


def _extract_sps_bank(
    tool: Path, game_dir: Path, bank_name: str, content: bytes, fmt: str,
    extension: str,
) -> list[Path]:
    native = game_dir / "native"
    native.mkdir(parents=True, exist_ok=True)
    input_path = native / bank_name
    input_path.write_bytes(content)
    output_dir = game_dir / "sequences"
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob(f"song*.{extension}"):
        if stale.is_file():
            stale.unlink()
    prefix = output_dir / f"song.{extension}"
    _run([str(tool), "-f", fmt, str(input_path), str(prefix)])
    return sorted(output_dir.glob(f"song*.{extension}"))


def _convert_m2(
    converter: Path, sources: list[Path], destination: Path, loops: int
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob("song*.mid"):
        if stale.is_file():
            stale.unlink()
    output: list[Path] = []
    for source in sources:
        target = destination / f"{source.stem}.mid"
        _run([str(converter), "-Loops", str(loops), str(source), str(target)])
        output.append(target)
    return output


def _valid_midis(paths: list[Path]) -> list[Path]:
    return [path for path in paths if path.read_bytes()[:4] == b"MThd"]


def _path_catalog(paths: list[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in paths:
        content = path.read_bytes()
        rows.append({
            "name": path.name, "size": len(content), "sha256": _sha256(content)
        })
    return rows


def _loop_status(source_audit: dict[str, object]) -> str:
    if source_audit["global_loop"] is not None:
        return "embedded-global"
    if source_audit["loop_tracks"]:
        return "embedded-per-track-no-practical-global-cycle"
    return "not-embedded"


def _sequence_catalog(sources: list[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for source in sources:
        source_audit = audit(source)
        rows.append({
            "song": source.stem,
            "loop_status": _loop_status(source_audit),
            "loop": source_audit["global_loop"],
            "source": source_audit,
        })
    return rows


def _write_sc55(
    sources: list[Path], destination: Path, engine: str, loops: int
) -> tuple[list[Path], list[dict[str, object]]]:
    destination.mkdir(parents=True, exist_ok=True)
    expanded_dir = destination.parent / "expanded"
    for directory in (destination, expanded_dir):
        directory.mkdir(parents=True, exist_ok=True)
        for stale in directory.glob("song*.mid"):
            if stale.is_file():
                stale.unlink()
        for stale in directory.glob("song*.MID"):
            if stale.is_file():
                stale.unlink()
    outputs: list[Path] = []
    rows: list[dict[str, object]] = []
    for source in sources:
        source_audit = audit(source)
        loop = source_audit["global_loop"]
        prepared_source = source
        expanded_path = expanded_dir / source.name
        if engine == "M3" and loop is not None:
            expand_m3_loops(source, expanded_path, loops)
            prepared_source = expanded_path
        target = destination / source.name
        prepare_sc55(prepared_source, target)
        output_audit = audit(target)
        sound_map = (
            "mt32" if "0x16" in source_audit["roland_model_ids"] else "gs"
        )
        rows.append({
            "song": source.stem,
            "source": source_audit,
            "sc55": {
                "path": str(target),
                "sha256": output_audit["sha256"],
                "duration_seconds": output_audit["duration_seconds"],
                "has_gs_reset": output_audit["has_gs_reset"],
                "sound_map": sound_map,
            },
            "loop_status": _loop_status(source_audit),
            "loop": loop,
        })
        outputs.append(target)
    return outputs, rows


def _check_midi_bank(game: Game, files: dict[str, tuple[bytes, str]],
                     archive: Path) -> None:
    if game.midi_bank is None or game.midi_bank_sha256 is None:
        return
    content, where = files[game.midi_bank.upper()]
    digest = _sha256(content)
    if digest != game.midi_bank_sha256:
        raise ExtractionError(
            f"{game.title}: {game.midi_bank} in {archive} ({where}) has sha256 "
            f"{digest[:16]}…, but the published packs were rendered from "
            f"{game.midi_bank_sha256[:16]}….  This rip's MIDI bank differs "
            "from the disk set the packs were built from, so its render "
            "would not match the published hash."
        )


def extract_game(
    game: Game, rom_root: Path, output_root: Path, sps_tool: Path,
    m2_converter: Path, seven_zip: str | None, loops: int,
    archive: Path | None = None,
) -> dict[str, object]:
    if archive is None:
        archive = _find_archive(rom_root, game.archive_stem)
    elif not archive.is_file():
        raise ExtractionError(f"{game.title}: archive not found: {archive}")
    files, images = _collect_files(archive, seven_zip)
    for name in (game.fm_bank, game.midi_bank):
        if name:
            _require(files, name, archive)
    _check_midi_bank(game, files, archive)
    game_dir = output_root / game.slug
    game_dir.mkdir(parents=True, exist_ok=True)
    drivers = game_dir / "drivers"
    drivers.mkdir(parents=True, exist_ok=True)
    copied_drivers: list[dict[str, object]] = []
    for name in dict.fromkeys((*game.fm_driver_files, *game.midi_driver_files)):
        content = _require(files, name, archive)
        target = drivers / name
        target.write_bytes(content)
        copied_drivers.append({
            "name": name, "size": len(content), "sha256": _sha256(content)
        })
    support = game_dir / "support"
    support.mkdir(parents=True, exist_ok=True)
    copied_support: list[dict[str, object]] = []
    for name in game.support_files:
        content = _require(files, name, archive)
        (support / name).write_bytes(content)
        copied_support.append({
            "name": name, "size": len(content), "sha256": _sha256(content)
        })

    common: dict[str, object] = {
        "game": game.slug,
        "title": game.title,
        "engine": game.engine,
        "archive": str(archive),
        "archive_sha256": _sha256(archive.read_bytes()),
        "images": images,
        "drivers": copied_drivers,
        "support_files": copied_support,
    }
    if game.engine == "custom":
        # Preserve the custom Strider bank and every SH*.ADP sample.  There is
        # no native external-MIDI bank on these disks.
        native = game_dir / "fm" / "native"
        native.mkdir(parents=True, exist_ok=True)
        preserved: list[dict[str, object]] = []
        names = [game.fm_bank] + sorted(name for name in files if name.endswith(".ADP"))
        for name in dict.fromkeys(names):
            content = _require(files, name, archive)
            target = native / name
            target.write_bytes(content)
            preserved.append({
                "name": name, "size": len(content), "sha256": _sha256(content)
            })
        common.update({
            "fm": {
                "status": "native-custom-bank-preserved",
                "files": preserved,
                "render_status": "requires custom driver capture",
            },
            "midi": {"status": "no-native-midi-bank"},
            "loops": {"status": "requires driver capture"},
        })
        (game_dir / "catalog.json").write_text(json.dumps(common, indent=2) + "\n")
        return common

    if not sps_tool.is_file():
        raise ExtractionError(f"x68k_sps_dec not found: {sps_tool}")
    fm_dir = game_dir / "fm"
    midi_dir = game_dir / "midi"
    fm_extension = "SQ2" if game.engine.startswith("M2") else "MID"
    fm_native = _extract_sps_bank(
        sps_tool, fm_dir, game.fm_bank, _require(files, game.fm_bank, archive),
        game.fm_format or "auto", fm_extension,
    )
    midi_native = _extract_sps_bank(
        sps_tool, midi_dir, game.midi_bank or "", _require(files, game.midi_bank or "", archive),
        game.midi_format or "auto", fm_extension,
    )
    if game.engine.startswith("M2"):
        if not m2_converter.is_file():
            raise ExtractionError(f"m2seq22mid not found: {m2_converter}")
        fm_smf = _convert_m2(m2_converter, fm_native, fm_dir / "smf", loops)
        midi_smf = _convert_m2(m2_converter, midi_native, midi_dir / "smf", loops)
    else:
        fm_smf = _valid_midis(fm_native)
        midi_smf = _valid_midis(midi_native)
    if not fm_smf or not midi_smf:
        raise ExtractionError(f"{game.title}: an extracted sequence bank is empty")
    sc55_files, loop_rows = _write_sc55(
        midi_smf, midi_dir / "sc55", game.engine, loops
    )
    fm_loop_rows = _sequence_catalog(fm_smf)
    common.update({
        "fm": {
            "status": "source-sequences-extracted",
            "bank": game.fm_bank,
            "bank_sha256": _sha256(_require(files, game.fm_bank, archive)),
            "native_count": len(fm_native),
            "native_files": _path_catalog(fm_native),
            "smf_count": len(fm_smf),
            "playback": "requires bundled original FM drivers",
            "songs": [str(path) for path in fm_smf],
        },
        "midi": {
            "status": "sc55-ready",
            "bank": game.midi_bank,
            "bank_sha256": _sha256(_require(files, game.midi_bank or "", archive)),
            "native_count": len(midi_native),
            "native_files": _path_catalog(midi_native),
            "smf_count": len(midi_smf),
            "sc55_count": len(sc55_files),
            "loops_expanded": loops,
            "songs": [str(path) for path in sc55_files],
        },
        "loop_points": {"fm": fm_loop_rows, "midi": loop_rows},
    })
    (game_dir / "catalog.json").write_text(json.dumps(common, indent=2) + "\n")
    return common


def _write_loop_table(catalogs: list[dict[str, object]], path: Path) -> None:
    fields = (
        "game", "mode", "song", "status", "start_tick", "end_tick",
        "period_ticks", "start_seconds", "end_seconds", "period_seconds",
        "native_lcm_ticks",
    )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        for game in catalogs:
            loop_points = game.get("loop_points")
            if not isinstance(loop_points, dict):
                writer.writerow({
                    "game": game["game"], "mode": "fm", "song": "*",
                    "status": game["loops"]["status"],
                })
                continue
            for mode in ("fm", "midi"):
                for row in loop_points[mode]:
                    loop = row["loop"] or {}
                    writer.writerow({
                        "game": game["game"],
                        "mode": mode,
                        "song": row["song"],
                        "status": row["loop_status"],
                        "start_tick": loop.get("start_tick", ""),
                        "end_tick": loop.get("end_tick", ""),
                        "period_ticks": loop.get("period_ticks", ""),
                        "start_seconds": loop.get("start_seconds", ""),
                        "end_seconds": loop.get("end_seconds", ""),
                        "period_seconds": loop.get("period_seconds", ""),
                        "native_lcm_ticks": row["source"].get(
                            "native_loop_lcm_ticks", ""
                        ),
                    })


def self_test() -> None:
    assert len(GAMES) == 5
    assert {game.slug for game in GAMES} == {
        "daimakaimura", "ffight", "sf2ce", "ssf2", "strider"
    }
    assert all(game.fm_bank for game in GAMES)
    assert sum(game.midi_bank is not None for game in GAMES) == 4
    print("extract_x68k_music self-test: OK")


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[2]
    default_upstream = repo / "cpsplus" / "work" / "x68000" / "upstream"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--archive", action="append", default=[], metavar="SLUG=PATH",
        help="disk-image archive for one game, e.g. "
             "--archive ssf2=/disks/ssf2_x68000.zip (repeatable; selects "
             "that game unless --game says otherwise)",
    )
    parser.add_argument("--rom-root", type=Path, default=repo / "roms" / "x68000",
                        help="directory searched by archive name for games "
                             "without an explicit --archive")
    parser.add_argument(
        "--output", type=Path,
        default=repo / "cpsplus" / "work" / "x68000" / "export",
    )
    parser.add_argument(
        "--sps-tool", type=Path,
        default=default_upstream / "ExtractorsDecoders" / "build" / "x68k_sps_dec",
    )
    parser.add_argument(
        "--m2-converter", type=Path,
        default=default_upstream / "MidiConverters" / "build" / "m2seq22mid",
    )
    parser.add_argument("--seven-zip", help="7zz/7z executable override")
    parser.add_argument("--loops", type=int, default=3,
                        help="finite loop passes in playback MIDI (default: 3)")
    parser.add_argument(
        "--game", action="append", choices=[game.slug for game in GAMES],
        help="extract only this game (repeatable)",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if args.loops < 1:
        parser.error("--loops must be at least one")
    archives: dict[str, Path] = {}
    slugs = {game.slug for game in GAMES}
    for item in args.archive:
        slug, _, value = item.partition("=")
        if slug not in slugs or not value:
            parser.error(f"--archive wants SLUG=PATH with SLUG one of "
                         f"{', '.join(sorted(slugs))}; got {item!r}")
        archives[slug] = Path(value).expanduser()
    wanted = set(args.game or archives or slugs)
    selected = [game for game in GAMES if game.slug in wanted]
    args.output.mkdir(parents=True, exist_ok=True)
    catalogs = [
        extract_game(
            game, args.rom_root, args.output, args.sps_tool,
            args.m2_converter, args.seven_zip, args.loops,
            archive=archives.get(game.slug),
        )
        for game in selected
    ]
    summary = {
        "schema": "cpsplus-x68000-audio-v1",
        "finite_loop_passes": args.loops,
        "games": catalogs,
    }
    catalog_path = args.output / "catalog.json"
    catalog_path.write_text(json.dumps(summary, indent=2) + "\n")
    loop_path = args.output / "loop_points.tsv"
    _write_loop_table(catalogs, loop_path)
    print(
        f"extracted {len(catalogs)} games; catalog: {catalog_path}; "
        f"loops: {loop_path}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ExtractionError, MidiError, OSError, zipfile.BadZipFile) as exc:
        print(f"extract_x68k_music: error: {exc}", file=sys.stderr)
        raise SystemExit(1)
