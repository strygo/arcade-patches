#!/usr/bin/env python3
"""Export rendered X68000 external-MIDI arrangements as loop-tagged FLAC.

Final Fight is taken from a native MT-32/Munt capture.  The remaining games
are taken from SC-55 captures.  SC-55's playback-only GS setup window is
removed, every file is converted to stereo 24-bit/48-kHz FLAC, and steady
second-pass loop candidates are written as Vorbis comments and JSON sidecars.

The optional Final Fight mastering profile is the fixed, album-relative chain
approved while comparing song00 against the X68000 MIDI OSV reference.  It is
not a per-track normalizer, so the source soundtrack's relative levels remain
unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


class ExportError(RuntimeError):
    pass


OUTPUT_SAMPLE_RATE = 48_000
OUTPUT_BITS = 24
SC55_SETUP_SECONDS = 0.5
SC55_GAMES = ("daimakaimura", "sf2ce", "ssf2")
MT32_GAMES = ("ffight",)
GAME_TITLES = {
    "daimakaimura": "Daimakaimura",
    "ffight": "Final Fight",
    "sf2ce": "Street Fighter II' Champion Edition",
    "ssf2": "Super Street Fighter II",
}
EXPECTED_SONG_COUNTS = {
    "daimakaimura": 27,
    "ffight": 20,
    "sf2ce": 46,
    "ssf2": 63,
}

# song00 was first raised by 8.2 dB to the OSV reference level, then passed
# through this broad EQ/width chain.  Keeping the gain fixed across Final
# Fight preserves its original inter-song level relationships.
FFIGHT_OSV_FILTER = (
    "volume=8.2dB,"
    "bass=g=-2:f=200:w=0.7,"
    "equalizer=f=900:t=q:w=0.8:g=1,"
    "equalizer=f=4000:t=q:w=1.3:g=1.7,"
    "treble=g=-3.3:f=8000:w=0.7,"
    "stereotools=slev=1.028,"
    "volume=0.95dB"
)
FFIGHT_PROFILES = {
    "none": None,
    "song00-osv": FFIGHT_OSV_FILTER,
}


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
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        details = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise ExportError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{details}"
        )
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def _load_catalog(path: Path, schema: str) -> dict[str, object]:
    if not path.is_file():
        raise ExportError(f"capture catalog is missing: {path}")
    catalog = json.loads(path.read_text())
    if catalog.get("schema") != schema:
        raise ExportError(f"unsupported capture catalog: {path}")
    return catalog


def _loop_tags(
    candidate: dict[str, object] | None, trim_seconds: float,
    sample_rate: int = OUTPUT_SAMPLE_RATE,
) -> dict[str, int]:
    if candidate is None:
        return {}
    start_seconds = float(candidate["start_seconds"]) - trim_seconds
    end_seconds = float(candidate["end_seconds"]) - trim_seconds
    start = round(start_seconds * sample_rate)
    end = round(end_seconds * sample_rate)
    if start < 0 or end <= start:
        raise ExportError(
            "loop candidate does not survive the requested setup trim: "
            f"start={start}, end={end}"
        )
    return {
        "LOOPSTART": start,
        "LOOPEND": end,
        "LOOPLENGTH": end - start,
    }


def _probe_flac(ffprobe: Path, path: Path) -> dict[str, object]:
    output = _run([
        str(ffprobe), "-v", "error", "-select_streams", "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,bits_per_raw_sample,duration_ts,time_base:format_tags",
        "-of", "json", str(path),
    ])
    probe = json.loads(output)
    streams = probe.get("streams", [])
    if len(streams) != 1:
        raise ExportError(f"expected one audio stream in {path}")
    stream = streams[0]
    if stream.get("codec_name") != "flac":
        raise ExportError(f"output is not FLAC: {path}")
    if int(stream.get("sample_rate", 0)) != OUTPUT_SAMPLE_RATE:
        raise ExportError(f"wrong output sample rate: {path}")
    if int(stream.get("channels", 0)) != 2:
        raise ExportError(f"wrong output channel count: {path}")
    if int(stream.get("bits_per_raw_sample", 0)) != OUTPUT_BITS:
        raise ExportError(f"wrong output bit depth: {path}")
    if stream.get("time_base") != f"1/{OUTPUT_SAMPLE_RATE}":
        raise ExportError(f"unexpected FLAC time base: {path}")
    frames = int(stream.get("duration_ts", 0))
    if frames <= 0:
        raise ExportError(f"FLAC has no audio frames: {path}")
    tags = {
        str(key).upper(): str(value)
        for key, value in probe.get("format", {}).get("tags", {}).items()
    }
    return {
        "codec": "flac",
        "channels": 2,
        "sample_rate": OUTPUT_SAMPLE_RATE,
        "bits_per_sample": OUTPUT_BITS,
        "frames": frames,
        "tags": tags,
    }


def _capture_index(
    catalog: dict[str, object], games: tuple[str, ...]
) -> dict[tuple[str, str], dict[str, object]]:
    captures: dict[tuple[str, str], dict[str, object]] = {}
    for capture in catalog["captures"]:
        game = str(capture["game"])
        song = str(capture["song"])
        if game not in games:
            continue
        key = (game, song)
        if key in captures:
            raise ExportError(f"duplicate capture: {game}/{song}")
        captures[key] = capture
    return captures


def _processing(
    module: str, ffight_profile: str,
    loop_candidate: dict[str, object] | None = None,
    compact_loop: bool = True,
) -> tuple[float, str, str]:
    if module == "SC-55":
        exact_end = ""
        note = "SC-55 GS setup window removed; no gain or tonal processing"
        if compact_loop and loop_candidate is not None:
            output_end = round(
                (float(loop_candidate["end_seconds"]) - SC55_SETUP_SECONDS)
                * OUTPUT_SAMPLE_RATE
            )
            # The end trim happens AFTER the sample-rate converter, in the
            # output sample domain.  Trimming the converter's input instead
            # would make it flush against a truncated tail, and the last few
            # dozen frames before LOOPEND would differ from a full-length
            # conversion -- which is how the published packs were made.
            # The converter can also finish one frame short depending on
            # phase, so pad before the exact trim: LOOPEND is always a valid
            # exclusive stream boundary.
            exact_end = f",apad,atrim=end_sample={output_end}"
            note += "; stream ends at LOOPEND"
        return (
            SC55_SETUP_SECONDS,
            (
                f"atrim=start={SC55_SETUP_SECONDS},"
                "asetpts=PTS-STARTPTS,"
                f"aresample={OUTPUT_SAMPLE_RATE}:filter_size=64:"
                f"phase_shift=10:exact_rational=1{exact_end}"
            ),
            note,
        )
    audio_filter = FFIGHT_PROFILES[ffight_profile]
    if audio_filter is None:
        return 0.0, "anull", "native MT-32 render; no mastering"
    return (
        0.0,
        audio_filter,
        "fixed Final Fight song00 OSV comparison mastering; no normalization",
    )


def _reuse_record(
    repo: Path, sidecar: Path, output: Path, source_hash: str,
    module: str, audio_filter: str, loop_tags: dict[str, int],
    compatible_audio_filter: str | None = None,
    processing_note: str | None = None,
) -> dict[str, object] | None:
    if not sidecar.is_file() or not output.is_file():
        return None
    record = json.loads(sidecar.read_text())
    if (
        record.get("schema") != "cpsplus-x68000-midi-flac-v1"
        or record.get("source_sha256") != source_hash
        or record.get("sound_module") != module
        or record.get("loop_tags") != loop_tags
        or record.get("output_sha256") != _sha256(output)
    ):
        return None
    if record.get("output") != _display_path(repo, output):
        return None
    if record.get("audio_filter") != audio_filter:
        if (
            record.get("audio_filter") != compatible_audio_filter
            or not loop_tags
            or int(record.get("flac", {}).get("frames", 0))
            != int(loop_tags["LOOPEND"])
        ):
            return None
        record["audio_filter"] = audio_filter
        if processing_note is not None:
            record["processing_note"] = processing_note
        sidecar.write_text(json.dumps(record, indent=2) + "\n")
    return record


def _compact_prior_flac(
    args: argparse.Namespace, repo: Path, sidecar: Path, output: Path,
    source_hash: str, module: str, loop_tags: dict[str, int],
    old_audio_filter: str, audio_filter: str, processing_note: str,
) -> dict[str, object] | None:
    if module != "SC-55" or not loop_tags:
        return None
    if not sidecar.is_file() or not output.is_file():
        return None
    prior = json.loads(sidecar.read_text())
    if (
        prior.get("schema") != "cpsplus-x68000-midi-flac-v1"
        or prior.get("source_sha256") != source_hash
        or prior.get("sound_module") != module
        or prior.get("audio_filter") != old_audio_filter
        or prior.get("loop_tags") != loop_tags
        or prior.get("output_sha256") != _sha256(output)
    ):
        return None

    prior_hash = str(prior["output_sha256"])
    temporary = output.with_name(f".{output.name}.{os.getpid()}.compact.tmp.flac")
    if temporary.exists():
        temporary.unlink()
    command = [
        str(args.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(output), "-map", "0:a:0", "-map_metadata", "0",
        "-af", f"atrim=end_sample={loop_tags['LOOPEND']},asetpts=PTS-STARTPTS",
        "-ar", str(OUTPUT_SAMPLE_RATE), "-ac", "2",
        "-sample_fmt", "s32", "-bits_per_raw_sample", str(OUTPUT_BITS),
        "-c:a", "flac", "-compression_level", "12", str(temporary),
    ]
    print(
        f"compacting {prior['game']}/{prior['song']} at LOOPEND ...",
        flush=True,
    )
    try:
        _run(command)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    flac = _probe_flac(args.ffprobe, output)
    if int(flac["frames"]) != int(loop_tags["LOOPEND"]):
        raise ExportError(f"compacted FLAC does not end at LOOPEND: {output}")
    for key, value in loop_tags.items():
        if flac["tags"].get(key) != str(value):
            raise ExportError(f"compacted FLAC lost {key} metadata: {output}")

    prior["audio_filter"] = audio_filter
    prior["processing_note"] = processing_note
    prior["compacted_from_output_sha256"] = prior_hash
    prior["output_sha256"] = _sha256(output)
    prior["flac"] = flac
    sidecar.write_text(json.dumps(prior, indent=2) + "\n")
    return prior


def _export_one(
    args: argparse.Namespace, repo: Path, capture: dict[str, object],
    module: str, capture_catalog: Path,
) -> dict[str, object]:
    game = str(capture["game"])
    song = str(capture["song"])
    source = _resolve(repo, str(capture["output"])).resolve()
    source_hash = str(capture["output_sha256"])
    loop_candidate = capture.get("loop_candidate")
    trim_seconds, audio_filter, processing_note = _processing(
        module, args.ffight_profile, loop_candidate
    )
    loop_tags = _loop_tags(loop_candidate, trim_seconds)
    output = args.output / game / f"{song}.flac"
    sidecar = output.with_suffix(".json")
    output.parent.mkdir(parents=True, exist_ok=True)

    if not args.overwrite:
        compatible_audio_filter = None
        if module == "SC-55" and loop_tags and ",apad,atrim=end_sample=" in audio_filter:
            compatible_audio_filter = audio_filter.rsplit(
                ",apad,atrim=end_sample=", 1
            )[0]
        reused = _reuse_record(
            repo, sidecar, output, source_hash, module, audio_filter, loop_tags,
            compatible_audio_filter, processing_note,
        )
        if reused is not None:
            print(f"reusing {game}/{song}", flush=True)
            return reused

        _, old_audio_filter, _ = _processing(
            module, args.ffight_profile, loop_candidate, compact_loop=False
        )
        compacted = _compact_prior_flac(
            args, repo, sidecar, output, source_hash, module, loop_tags,
            old_audio_filter, audio_filter, processing_note,
        )
        if compacted is not None:
            return compacted

    if not source.is_file():
        raise ExportError(f"capture WAV is missing: {source}")
    if _sha256(source) != source_hash:
        raise ExportError(f"capture WAV hash changed: {source}")

    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp.flac")
    if temporary.exists():
        temporary.unlink()
    command = [
        str(args.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-af", audio_filter,
        "-ar", str(OUTPUT_SAMPLE_RATE), "-ac", "2",
        "-sample_fmt", "s32", "-bits_per_raw_sample", str(OUTPUT_BITS),
        "-c:a", "flac", "-compression_level", "12",
        "-metadata", f"TITLE={song}",
        "-metadata", f"ALBUM={GAME_TITLES.get(game, game)} (X68000 MIDI)",
        "-metadata", f"GAME={game}",
        "-metadata", f"SOUND_MODULE={module}",
        "-metadata", f"LOOP_STATUS={capture['loop_status']}",
    ]
    for key, value in loop_tags.items():
        command.extend(("-metadata", f"{key}={value}"))
    command.append(str(temporary))
    print(f"exporting {game}/{song} ({module}) ...", flush=True)
    try:
        _run(command)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    flac = _probe_flac(args.ffprobe, output)
    for key, value in loop_tags.items():
        if flac["tags"].get(key) != str(value):
            raise ExportError(f"FLAC lost {key} metadata: {output}")
    frames = int(flac["frames"])
    if loop_tags and int(loop_tags["LOOPEND"]) > frames:
        raise ExportError(f"loop end exceeds FLAC duration: {output}")

    record = {
        "schema": "cpsplus-x68000-midi-flac-v1",
        "game": game,
        "song": song,
        "sound_module": module,
        "source_capture_catalog": _display_path(repo, capture_catalog),
        "source": _display_path(repo, source),
        "source_sha256": source_hash,
        "source_capture": {
            "input": capture["input"],
            "input_sha256": capture["input_sha256"],
            "loop_status": capture["loop_status"],
            "loop_candidate": capture.get("loop_candidate"),
            "renderer_version": capture["renderer_version"],
        },
        "setup_trim_seconds": trim_seconds,
        "ffight_mastering_profile": (
            args.ffight_profile if module == "MT-32" else None
        ),
        "audio_filter": audio_filter,
        "processing_note": processing_note,
        "loop_tags": loop_tags,
        "output": _display_path(repo, output),
        "output_sha256": _sha256(output),
        "flac": flac,
    }
    sidecar.write_text(json.dumps(record, indent=2) + "\n")
    return record


def export(args: argparse.Namespace, repo: Path) -> Path:
    sc55_catalogs = [path.resolve() for path in args.sc55_catalog]
    sc55 = [
        _load_catalog(path, "cpsplus-x68000-sc55-capture-v1")
        for path in sc55_catalogs
    ]
    if any(
        catalog.get("firmware_revision") != "SC-55 v1.21"
        for catalog in sc55
    ):
        raise ExportError(
            "SC-55 export requires the approved original SC-55 v1.21 firmware"
        )

    selected_games = set(args.game or (*SC55_GAMES, *MT32_GAMES))
    unknown = selected_games - set((*SC55_GAMES, *MT32_GAMES))
    if unknown:
        raise ExportError("unsupported games: " + ", ".join(sorted(unknown)))
    captures: list[tuple[dict[str, object], str, Path]] = []
    if selected_games.intersection(SC55_GAMES):
        indexed: dict[
            tuple[str, str], tuple[dict[str, object], Path]
        ] = {}
        for catalog, catalog_path in zip(sc55, sc55_catalogs):
            for key, capture in _capture_index(catalog, SC55_GAMES).items():
                if key in indexed:
                    raise ExportError(
                        f"capture appears in multiple SC-55 catalogs: "
                        f"{key[0]}/{key[1]}"
                    )
                indexed[key] = (capture, catalog_path)
        captures.extend(
            (capture, "SC-55", catalog_path)
            for key, (capture, catalog_path) in sorted(indexed.items())
            if key[0] in selected_games
        )
    mt32_used = bool(selected_games.intersection(MT32_GAMES))
    if mt32_used:
        # The MT-32 catalog is only needed for Final Fight; an SC-55-only
        # selection must not require a Munt render to exist.
        mt32 = _load_catalog(
            args.mt32_catalog.resolve(), "cpsplus-x68000-mt32-capture-v1"
        )
        if mt32.get("machine_id") != "mt32_2_07":
            raise ExportError(
                "Final Fight export requires the approved mt32_2_07 capture "
                "catalog"
            )
        index = _capture_index(mt32, MT32_GAMES)
        captures.extend(
            (capture, "MT-32", args.mt32_catalog.resolve())
            for key, capture in sorted(index.items())
            if key[0] in selected_games
        )
    captures.sort(key=lambda row: (str(row[0]["game"]), str(row[0]["song"])))
    if not captures:
        raise ExportError("selection contains no captures")
    actual_counts = {
        game: sum(str(row[0]["game"]) == game for row in captures)
        for game in selected_games
    }
    incomplete = {
        game: (actual_counts[game], EXPECTED_SONG_COUNTS[game])
        for game in sorted(selected_games)
        if actual_counts[game] != EXPECTED_SONG_COUNTS[game]
    }
    if incomplete and not args.allow_partial:
        details = ", ".join(
            f"{game}={actual}/{expected}"
            for game, (actual, expected) in incomplete.items()
        )
        raise ExportError(f"capture catalogs are incomplete: {details}")

    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    records = [
        _export_one(args, repo, capture, module, catalog)
        for capture, module, catalog in captures
    ]
    counts: dict[str, int] = {}
    looped = 0
    for record in records:
        game = str(record["game"])
        counts[game] = counts.get(game, 0) + 1
        looped += bool(record["loop_tags"])
    output_catalog = args.output / "catalog.json"
    output_catalog.write_text(json.dumps({
        "schema": "cpsplus-x68000-midi-flac-catalog-v1",
        "sample_rate": OUTPUT_SAMPLE_RATE,
        "bits_per_sample": OUTPUT_BITS,
        "ffight_mastering_profile": args.ffight_profile,
        "source_catalogs": {
            "SC-55": [
                _display_path(repo, path) for path in sc55_catalogs
            ],
            "MT-32": (_display_path(repo, args.mt32_catalog)
                      if mt32_used else None),
        },
        "counts_by_game": counts,
        "loop_tagged_files": looped,
        "files": records,
    }, indent=2) + "\n")
    print(
        f"exported {len(records)} FLACs ({looped} loop-tagged); "
        f"catalog: {output_catalog}"
    )
    return output_catalog


def self_test() -> None:
    candidate = {"start_seconds": 12.5, "end_seconds": 15.0}
    assert _loop_tags(candidate, 0.5) == {
        "LOOPSTART": 576_000,
        "LOOPEND": 696_000,
        "LOOPLENGTH": 120_000,
    }
    assert _loop_tags(None, 0.5) == {}
    trim, audio_filter, _ = _processing("SC-55", "song00-osv")
    assert trim == SC55_SETUP_SECONDS
    assert audio_filter.startswith("atrim=start=0.5")
    _, compact_filter, compact_note = _processing(
        "SC-55", "song00-osv", candidate
    )
    assert ":end=" not in compact_filter          # trim after the resampler
    assert "apad,atrim=end_sample=696000" in compact_filter
    assert compact_note.endswith("stream ends at LOOPEND")
    trim, audio_filter, _ = _processing("MT-32", "song00-osv")
    assert trim == 0.0
    assert audio_filter == FFIGHT_OSV_FILTER
    assert FFIGHT_OSV_FILTER.startswith("volume=8.2dB")
    assert sum(EXPECTED_SONG_COUNTS.values()) == 156
    print("export_x68k_midi_flac self-test: OK")


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[2]
    base = repo / "cpsplus" / "work" / "x68000"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--sc55-catalog", type=Path, action="append",
        help=(
            "SC-55 capture catalog (repeat for disjoint render shards; "
            "default: capture/sc55/mk1/catalog.json)"
        ),
    )
    parser.add_argument(
        "--mt32-catalog", type=Path,
        default=base / "capture" / "mt32" / "mt32_2_07" / "catalog.json",
    )
    parser.add_argument(
        "--output", type=Path, default=base / "flac" / "midi",
    )
    parser.add_argument(
        "--ffmpeg", type=Path, default=Path("ffmpeg"),
        help="ffmpeg executable",
    )
    parser.add_argument(
        "--ffprobe", type=Path, default=Path("ffprobe"),
        help="ffprobe executable",
    )
    parser.add_argument(
        "--ffight-profile", choices=sorted(FFIGHT_PROFILES),
        default="song00-osv",
    )
    parser.add_argument("--game", action="append")
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="permit a deliberately incomplete game selection",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if args.sc55_catalog is None:
        args.sc55_catalog = [
            base / "capture" / "sc55" / "mk1" / "catalog.json"
        ]
    export(args, repo)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, ExportError) as exc:
        print(f"export_x68k_midi_flac: error: {exc}", file=sys.stderr)
        raise SystemExit(1)
