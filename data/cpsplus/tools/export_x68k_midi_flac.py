#!/usr/bin/env python3
"""Export rendered X68000 external-MIDI arrangements as loop-tagged FLAC.

Final Fight is taken from a native MT-32/Munt capture.  The remaining games
are taken from SC-55 captures.  SC-55's playback-only GS setup window is
removed, every file is converted to stereo 24-bit/48-kHz FLAC, and steady
second-pass loop candidates are written as Vorbis comments and JSON sidecars.

The SC-55 exports feed packs that are rebuilt on users' machines against
pinned hashes, so their audio is computed here rather than by an ffmpeg filter
graph: ffmpeg's resampler runs in float and rounds differently per CPU, and a
Windows export differed from the Mac in every file.  The in-repo chain is the
one those pins were made with -- the setup trim, swresample's filter_size=64
64 -> 48 kHz conversion, llrint to s32 and the FLAC encoder's shift to 24 bits
-- reproduced bit for bit (pack/resample.py), so the decoded FLAC audio is the
same on every CPU.  ffmpeg only encodes the finished integer PCM, which FLAC
stores losslessly; the file bytes may still vary with the ffmpeg version.

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
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pack import resample  # noqa: E402


class ExportError(RuntimeError):
    pass


OUTPUT_SAMPLE_RATE = 48_000
OUTPUT_BITS = 24
SC55_SETUP_SECONDS = 0.5
# swresample settings the published SC-55 exports were converted with
SC55_FILTER_SIZE = 64
SC55_TAPS_SHA256 = "15974aee5370021799860f3ddc83da62e0fa915998cf7f4a4f8ff6a46c1066f8"
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
    result = subprocess.run(
        command, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
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
            # The end cut happens AFTER the sample-rate converter, in the
            # output sample domain: the samples before LOOPEND are those of
            # a full-length conversion, which is how the published packs were
            # made.  A conversion that ends short of LOOPEND is padded with
            # silence, as ffmpeg's apad did.
            exact_end = f"; end at sample {output_end}"
            note += "; stream ends at LOOPEND"
        return (
            SC55_SETUP_SECONDS,
            (
                f"trim {SC55_SETUP_SECONDS} s; swresample-exact "
                f"{OUTPUT_SAMPLE_RATE} Hz filter_size={SC55_FILTER_SIZE} "
                "phase_shift=10 exact_rational=1 (float32, arm64 order); "
                f"llrint to s32, 24-bit{exact_end}"
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
) -> dict[str, object] | None:
    if not sidecar.is_file() or not output.is_file():
        return None
    record = json.loads(sidecar.read_text())
    if (
        record.get("schema") != "cpsplus-x68000-midi-flac-v1"
        or record.get("source_sha256") != source_hash
        or record.get("sound_module") != module
        or record.get("audio_filter") != audio_filter
        or record.get("loop_tags") != loop_tags
        or record.get("output_sha256") != _sha256(output)
    ):
        return None
    if record.get("output") != _display_path(repo, output):
        return None
    return record


def _float_wav(path: Path) -> tuple[int, np.ndarray]:
    """(rate, samples) of a 32-bit float WAV, samples as a float32 view of
    shape (frames, channels) -- the file is mapped, not read."""
    raw = np.memmap(path, dtype=np.uint8, mode="r")
    if bytes(raw[:4]) != b"RIFF" or bytes(raw[8:12]) != b"WAVE":
        raise ExportError(f"not a WAV file: {path}")
    pos, fmt = 12, None
    while pos + 8 <= len(raw):
        chunk = bytes(raw[pos:pos + 4])
        size = struct.unpack("<I", bytes(raw[pos + 4:pos + 8]))[0]
        if chunk == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack(
                "<HHIIHH", bytes(raw[pos + 8:pos + 24]))
            if tag == 0xFFFE:                   # WAVE_FORMAT_EXTENSIBLE
                tag = struct.unpack("<H", bytes(raw[pos + 32:pos + 34]))[0]
            fmt = tag, channels, rate, bits
        elif chunk == b"data":
            if fmt is None or fmt[0] != 3 or fmt[3] != 32 or fmt[1] != 2:
                raise ExportError(f"expected stereo 32-bit float WAV: {path}")
            size = min(size, len(raw) - pos - 8) // 8 * 8
            samples = np.frombuffer(raw, "<f4", count=size // 4, offset=pos + 8)
            return fmt[2], samples.reshape(-1, 2)
        pos += 8 + size + (size & 1)
    raise ExportError(f"WAV has no audio data: {path}")


def _sc55_pcm(source: Path, loop_tags: dict[str, int]) -> np.ndarray:
    """The SC-55 capture as the published 24-bit export, in s32 form (the low
    byte zero), shape (frames, 2).

    ffmpeg's chain, step by step: atrim=start drops round(0.5 s * rate)
    input frames; aresample converts in float (swresample's fltp), which
    resample.convolve_f32 reproduces exactly; the float result goes to s32 by
    llrint (round to nearest, ties to even, of v * 2**31; saturating); the
    FLAC encoder keeps the top 24 bits (an arithmetic shift, so a floor);
    apad + atrim=end_sample pad with silence and cut at LOOPEND."""
    rate, samples = _float_wav(source)
    start = round(SC55_SETUP_SECONDS * rate)
    samples = samples[start:]
    if not np.all(np.isfinite(samples)):
        raise ExportError(f"capture holds non-finite samples: {source}")
    end = loop_tags.get("LOOPEND")

    def to_s32(v: np.ndarray) -> np.ndarray:
        q31 = np.clip(np.rint(v.astype(np.float64) * 2.0**31),
                      -2.0**31, 2.0**31 - 1).astype(np.int64)
        return ((q31 >> 8) << 8).astype("<i4")

    pcm = resample.convolve_f32(
        lambda k: np.ascontiguousarray(samples[:, k]), len(samples), 2,
        rate, OUTPUT_SAMPLE_RATE, to_s32, "<i4", SC55_FILTER_SIZE, limit=end)
    if end is not None and len(pcm) < end:
        pcm = np.concatenate((pcm, np.zeros((end - len(pcm), 2), "<i4")))
    return pcm


def _decode_s32(ffmpeg: Path, path: Path) -> bytes:
    """A FLAC file's samples as s32le: lossless, no conversion in ffmpeg."""
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-map", "0:a:0", "-f", "s32le", "-c:a", "pcm_s32le", "-"],
        capture_output=True)
    if result.returncode:
        raise ExportError(
            f"could not decode {path}: "
            f"{result.stderr.decode('utf-8', 'replace')[:500]}")
    return result.stdout


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
        reused = _reuse_record(
            repo, sidecar, output, source_hash, module, audio_filter, loop_tags,
        )
        if reused is not None:
            print(f"reusing {game}/{song}", flush=True)
            return reused

    if not source.is_file():
        raise ExportError(f"capture WAV is missing: {source}")
    if _sha256(source) != source_hash:
        raise ExportError(f"capture WAV hash changed: {source}")

    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp.flac")
    if temporary.exists():
        temporary.unlink()
    pcm = None
    if module == "SC-55":
        print(f"converting {game}/{song} ({module}) ...", flush=True)
        pcm = _sc55_pcm(source, loop_tags).tobytes()
        # already the finished s32 samples: nothing for ffmpeg to convert
        audio_input = [
            "-f", "s32le", "-ar", str(OUTPUT_SAMPLE_RATE), "-ac", "2",
            "-i", "pipe:0",
        ]
    else:
        audio_input = [
            "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-af", audio_filter,
        ]
    command = [
        str(args.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        *audio_input,
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
        if pcm is None:
            _run(command)
        else:
            result = subprocess.run(command, input=pcm, capture_output=True)
            if result.returncode:
                raise ExportError(
                    f"command failed ({result.returncode}): "
                    f"{' '.join(command)}\n"
                    f"{result.stderr.decode('utf-8', 'replace')}")
            # FLAC is lossless: hold the encoder to that before publishing
            if _decode_s32(args.ffmpeg, temporary) != pcm:
                raise ExportError(f"FLAC does not decode to its PCM: {output}")
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
        # the file bytes carry the encoder's version; the audio does not
        "pcm_s32le_sha256": (hashlib.sha256(pcm).hexdigest()
                             if pcm is not None else None),
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
    assert audio_filter.startswith("trim 0.5 s; swresample-exact")
    _, compact_filter, compact_note = _processing(
        "SC-55", "song00-osv", candidate
    )
    assert compact_filter.endswith("; end at sample 696000")
    assert compact_note.endswith("stream ends at LOOPEND")
    # the SC-55 conversion's filter bank has one possible content
    bank = resample.taps32(64_000, OUTPUT_SAMPLE_RATE, SC55_FILTER_SIZE)
    assert bank.shape == (3, 88)
    assert hashlib.sha256(bank.tobytes()).hexdigest() == SC55_TAPS_SHA256
    # a fused multiply-add that lands on a float32 midpoint in float64 is
    # settled by the part float64 rounded away, not by ties-to-even
    one, tail = np.array([1.0]), 2.0**-24
    assert resample.fma32(np.array([tail + 2.0**-76]), one)[0] == 1 + 2.0**-23
    assert resample.fma32(np.array([-tail - 2.0**-76]), -one)[0] == -1 - 2.0**-23
    assert resample.fma32(np.array([tail - 2.0**-76]), one)[0] == 1.0
    assert resample.fma32(np.array([tail]), one)[0] == 1.0
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
        "--ffmpeg", type=Path,
        default=Path(os.environ.get("CPSPLUS_FFMPEG") or "ffmpeg"),
        help="ffmpeg executable (default: $CPSPLUS_FFMPEG, then PATH)",
    )
    parser.add_argument(
        "--ffprobe", type=Path,
        default=Path(os.environ.get("CPSPLUS_FFPROBE") or "ffprobe"),
        help="ffprobe executable (default: $CPSPLUS_FFPROBE, then PATH)",
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
