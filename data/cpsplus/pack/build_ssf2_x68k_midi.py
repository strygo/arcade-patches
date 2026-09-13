"""Build the SSF2 X68000 external-MIDI pack for the CPS2 QSound latch.

The inputs are the 63 stereo 48 kHz SC-55 FLAC renders produced by
``tools/export_x68k_midi_flac.py``.  Every command join must have passed the
two-file X68000/arcade listening gate in
``manifests/x68k_midi_arcade_map.tsv``.  The sole source-only row, song3c,
must not claim arcade command 0x3d: that command is the native QSound-logo
sequence and deliberately fails open.

These particular Nuked-SC55 renders carry a stable DC pedestal followed by
capture pre-roll.  ``manifests/ssf2_x68k_midi_audio.tsv`` pins both the source
shape and the deterministic cleanup: validate and subtract the per-channel DC
value, find sustained AC onset, retain 50 ms of pre-roll, then rebase the MIDI
loop tags.  Looped songs retain one real second-pass tail for CPS+ crossfade;
rows without loop coordinates are one-shots.

Run:
  python3 build_pack.py ssf2-x68k-midi --flac-dir <flac-dir>
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import adxcodec, protocols
from .adxencode import encode_adx_track
from .build_common import MANIFESTS, PACKS_DIR, PKG_ROOT
from .format import (CODEC_ADX, VERB_NONE, VERB_PLAY, VERB_STOP, PackReader,
                     PackWriter, TrackMeta, TriggerRow)


SOURCE_DIR = PKG_ROOT / "work" / "x68000" / "flac" / "midi" / "ssf2"
MAP_TSV = MANIFESTS / "x68k_midi_arcade_map.tsv"
AUDIO_TSV = MANIFESTS / "ssf2_x68k_midi_audio.tsv"
PACK_NAME = "ssf2_x68k_midi.cpk"
TITLE = "Super Street Fighter II: The New Challengers (X68000 MIDI Soundtrack)"
RATE = 48_000
CHANNELS = 2
TRIGGER_ROWS = 0x1200
XFADE_SAMPLES = RATE

# Measured (EBU R128 loudness match) against the isolated native ssf2
# QSound renders, 2026-08-31.  The canonical equal mono-downmix comparison
# puts the encoded pack a median 12.9 LU above the board (IQR 2.2 LU), giving
# 0x1d under the CPS+ linear gain law.  It independently agrees with the
# existing HSF2-based ssf2_arrange pack's measured gain.
TRIG_GAIN = 0x1d
NATIVE_QSOUND_LOGO_CMD = 0x3D


@dataclass(frozen=True)
class MusicRow:
    song: str
    role: str
    command: int
    in_game: bool


@dataclass(frozen=True)
class AudioRow:
    source_frames: int
    dc_left: int
    dc_right: int
    trim_samples: int
    source_ls: int | None
    source_le: int | None
    pack_ls: int | None
    pack_le: int | None

    @property
    def loops(self) -> bool:
        return self.source_ls is not None


def _dict_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as f:
        yield from csv.DictReader(
            (line for line in f if line.strip() and not line.startswith("#")),
            delimiter="\t")


def load_music_map(path: Path = MAP_TSV) -> list[MusicRow]:
    rows: list[MusicRow] = []
    claimed: set[int] = set()
    for raw in _dict_rows(path):
        if raw["game"] != "ssf2":
            continue
        song = raw["song"]
        if raw["arcade_set"] != "ssf2":
            raise ValueError(f"{song}: reviewed arcade set is not ssf2")
        if raw["match_status"] != "verified":
            raise ValueError(
                f"{song}: listening gate is {raw['match_status']!r}, not "
                "'verified'")
        if raw["in_game"] not in {"yes", "no"}:
            raise ValueError(f"{song}: invalid in_game value")
        if (raw.get("arcade_aliases") or "").strip():
            raise ValueError(f"{song}: SSF2 map unexpectedly has aliases")
        command = int(raw["arcade_cmd"], 0)
        if command in claimed:
            raise ValueError(f"duplicate command 0x{command:04x}")
        claimed.add(command)
        rows.append(MusicRow(song, raw["role"], command,
                             raw["in_game"] == "yes"))

    expected_songs = {f"song{i:02x}" for i in range(0x3f)}
    got_songs = {r.song for r in rows}
    if got_songs != expected_songs:
        raise ValueError(
            "SSF2 map must contain exactly song00..song3e; "
            f"missing={sorted(expected_songs - got_songs)}, "
            f"extra={sorted(got_songs - expected_songs)}")
    expected_commands = set(range(1, 0x40))
    if claimed != expected_commands:
        raise ValueError("SSF2 command vocabulary must be exactly 0x01..0x3f")
    unused = [r.song for r in rows if not r.in_game]
    if unused != ["song3c"]:
        raise ValueError(f"expected only song3c to be unused, got {unused}")
    return sorted(rows, key=lambda r: int(r.song[4:], 16))


def _optional_int(raw: dict[str, str], key: str) -> int | None:
    value = raw[key].strip()
    return int(value) if value and value != "-" else None


def load_audio_manifest(path: Path = AUDIO_TSV) -> dict[str, AudioRow]:
    out: dict[str, AudioRow] = {}
    for raw in _dict_rows(path):
        song = raw["song"]
        if song in out:
            raise ValueError(f"duplicate audio row for {song}")
        row = AudioRow(
            source_frames=int(raw["source_frames"]),
            dc_left=int(raw["dc_left"]), dc_right=int(raw["dc_right"]),
            trim_samples=int(raw["trim_samples"]),
            source_ls=_optional_int(raw, "source_ls"),
            source_le=_optional_int(raw, "source_le"),
            pack_ls=_optional_int(raw, "pack_ls"),
            pack_le=_optional_int(raw, "pack_le"))
        loop_values = (row.source_ls, row.source_le, row.pack_ls, row.pack_le)
        if any(v is None for v in loop_values) != all(v is None for v in loop_values):
            raise ValueError(f"{song}: loop row is only partially populated")
        if row.trim_samples < 0 or row.trim_samples >= row.source_frames:
            raise ValueError(f"{song}: invalid trim_samples")
        if row.loops:
            assert row.source_ls is not None and row.source_le is not None
            assert row.pack_ls is not None and row.pack_le is not None
            if row.source_le <= row.source_ls or row.pack_le <= row.pack_ls:
                raise ValueError(f"{song}: invalid loop interval")
            if row.pack_ls % 32 or row.pack_le % 32:
                raise ValueError(f"{song}: pack loop is not on the ADX grid")
            expected_ls = (row.source_ls - row.trim_samples - XFADE_SAMPLES) // 32 * 32
            expected_le = (row.source_le - row.trim_samples - XFADE_SAMPLES) // 32 * 32
            if (row.pack_ls, row.pack_le) != (expected_ls, expected_le):
                raise ValueError(
                    f"{song}: pack loop is not the trimmed, one-second-shifted "
                    "source loop")
            if abs((row.source_le - row.source_ls) -
                   (row.pack_le - row.pack_ls)) >= 32:
                raise ValueError(f"{song}: pack loop changes the source period")
            if row.pack_le + XFADE_SAMPLES > row.source_frames - row.trim_samples:
                raise ValueError(f"{song}: crossfade tail exceeds trimmed source")
        out[song] = row

    expected = {f"song{i:02x}" for i in range(0x3f)}
    if set(out) != expected:
        raise ValueError("audio manifest must contain exactly song00..song3e")
    loop_count = sum(row.loops for row in out.values())
    if loop_count != 46:
        raise ValueError(f"expected 46 looped songs, got {loop_count}")
    return out


def probe_flac(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=sample_rate,channels:format_tags", "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    info = json.loads(proc.stdout)
    if len(info.get("streams", [])) != 1:
        raise ValueError(f"{path.name}: expected exactly one audio stream")
    stream = info["streams"][0]
    tags = {k.upper(): v
            for k, v in info.get("format", {}).get("tags", {}).items()}
    return {"rate": int(stream["sample_rate"]),
            "channels": int(stream["channels"]), "tags": tags}


def decode_flac(path: Path) -> np.ndarray:
    """The FLAC's samples as s16, taken exactly as the published packs were.

    ffmpeg decodes FLAC losslessly to s32; the reduction to s16 is done here
    as ffmpeg's s32 -> s16 conversion does it, an arithmetic shift right by
    16 (a floor, no dither), so the result cannot depend on the CPU."""
    proc = subprocess.run(
        [adxcodec.FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0",
         "-f", "s32le", "-acodec", "pcm_s32le", "-"],
        capture_output=True, check=True)
    s32 = np.frombuffer(proc.stdout, dtype="<i4")
    if s32.size % CHANNELS:
        raise ValueError(f"{path.name}: decoded PCM ends mid-frame")
    return (s32 >> 16).astype("<i2").reshape(-1, CHANNELS)


def measure_cleanup(pcm: np.ndarray) -> tuple[tuple[int, int], int]:
    """Return deterministic (per-channel DC baseline, musical-onset trim)."""
    if len(pcm) < RATE // 2:
        raise ValueError("source is too short to measure its capture pre-roll")
    head = pcm[:RATE // 2].astype(np.float64)
    dc_array = np.rint(np.median(head, axis=0)).astype(np.int64)
    centered = pcm[:min(len(pcm), RATE * 10)].astype(np.float64) - dc_array
    mono = centered.mean(axis=1)
    window = RATE // 50                         # 20 ms
    count = len(mono) // window
    ac_rms = mono[:count * window].reshape(count, window).std(axis=1)
    peak_ac = float(np.max(ac_rms))
    if peak_ac <= 0:
        raise ValueError("no AC signal found in first ten seconds")
    active = ac_rms >= max(peak_ac * 1e-3, 1.0)
    sustained = np.convolve(active.astype(np.int8), np.ones(3, dtype=np.int8),
                            mode="same") >= 2
    audible = np.flatnonzero(sustained)
    if not audible.size:
        raise ValueError("no sustained musical onset found in first ten seconds")
    trim = max(int(audible[0]) * window - RATE // 20, 0)  # 50 ms pre-roll
    return (int(dc_array[0]), int(dc_array[1])), trim


def clean_pcm(pcm: np.ndarray, row: AudioRow, name: str) -> np.ndarray:
    if len(pcm) != row.source_frames:
        raise ValueError(
            f"{name}: decoded {len(pcm)} frames, expected {row.source_frames}")
    dc, trim = measure_cleanup(pcm)
    if dc != (row.dc_left, row.dc_right):
        raise ValueError(
            f"{name}: measured DC {dc}, expected "
            f"{(row.dc_left, row.dc_right)}")
    if trim != row.trim_samples:
        raise ValueError(
            f"{name}: measured trim {trim}, expected {row.trim_samples}")
    centered = pcm.astype(np.int32)
    centered -= np.array(dc, dtype=np.int32)
    centered = np.clip(centered, -32768, 32767).astype("<i2")
    return centered[trim:]


def build(*, out: Path | None = None, flac_dir: Path = SOURCE_DIR,
          map_path: Path = MAP_TSV, audio_path: Path = AUDIO_TSV,
          measure_snr: bool = False) -> dict:
    music = load_music_map(map_path)
    audio = load_audio_manifest(audio_path)
    proto = protocols.PROTOCOLS["ssf2"]
    if proto.latch_page != 0x618000 or proto.off_arg_byte != 0:
        raise ValueError("ssf2 protocol no longer matches the measured CPS2 latch")
    if proto.control_verbs != {0xff00: VERB_STOP}:
        raise ValueError("ssf2 protocol control map changed unexpectedly")

    writer = PackWriter(
        proto, title=TITLE, trigger_rows=TRIGGER_ROWS,
        default_rate=RATE, xfade_samples=XFADE_SAMPLES)
    track_of: dict[str, int] = {}
    snrs: list[tuple[str, float]] = []

    print(f"[ssf2] === {TITLE} ===")
    for music_row in music:
        src = flac_dir / f"{music_row.song}.flac"
        if not src.is_file():
            raise FileNotFoundError(f"missing rendered MIDI source: {src}")
        probe = probe_flac(src)
        if (probe["rate"], probe["channels"]) != (RATE, CHANNELS):
            raise ValueError(
                f"{src.name}: expected {RATE} Hz stereo, got "
                f"{probe['rate']} Hz/{probe['channels']}ch")
        tags = probe["tags"]
        spec = audio[music_row.song]
        pcm = clean_pcm(decode_flac(src), spec, src.name)

        if spec.loops:
            assert spec.source_ls is not None and spec.source_le is not None
            assert spec.pack_ls is not None and spec.pack_le is not None
            for tag, want in (("LOOPSTART", spec.source_ls),
                              ("LOOPEND", spec.source_le)):
                if tag not in tags or int(tags[tag]) != want:
                    raise ValueError(
                        f"{src.name}: {tag}={tags.get(tag)!r}, expected {want}")
            keep = spec.pack_le + XFADE_SAMPLES
            pcm = pcm[:keep]
            ls, le, xfade = spec.pack_ls, spec.pack_le, 1
            kind = "xfade loop"
        else:
            if "LOOPSTART" in tags or "LOOPEND" in tags:
                raise ValueError(f"{src.name}: untracked embedded loop tags")
            keep, ls, le, xfade = len(pcm), 0, 0, 0
            kind = "one-shot"

        pcm_bytes = pcm.tobytes()
        data, coef1, coef2, snr = encode_adx_track(
            pcm_bytes, RATE, CHANNELS, keep, music_row.song, measure_snr)
        if snr is not None:
            snrs.append((music_row.song, snr))
        meta = TrackMeta(
            sample_rate=RATE, channels=CHANNELS, codec=CODEC_ADX, gain=0x7f,
            loop_start_sample=ls,
            loop_start_byte=(adxcodec.samples_to_stream_byte(ls, CHANNELS)
                             if spec.loops else 0),
            loop_end_sample=le,
            loop_end_byte=(adxcodec.samples_to_stream_byte(le, CHANNELS)
                           if spec.loops else 0),
            coef1=coef1, coef2=coef2, xfade_enable=xfade,
            name=f"{music_row.song} {music_row.role} ({kind})",
            source=f"ssf2/{src.name}; dc={spec.dc_left},{spec.dc_right}; "
                   f"trim={spec.trim_samples}")
        ti = writer.add_track(data, meta)
        track_of[music_row.song] = ti
        print(f"[ssf2] t{ti:02d} {music_row.song} {keep / RATE:7.2f}s  "
              f"{kind:<11} {len(data) / 1e6:6.2f} MB  {music_row.role}")

    for row in music:
        if not row.in_game:
            continue
        writer.set_trigger(
            row.command,
            TriggerRow(verb=VERB_PLAY, track=track_of[row.song],
                       gain=TRIG_GAIN, suppress=1))

    out_path = out or (PACKS_DIR / PACK_NAME)
    writer.write(out_path)

    reader = PackReader(out_path)
    try:
        if reader.header.game_id != "ssf2":
            raise ValueError("readback: wrong game id")
        if reader.header.trigger_rows != TRIGGER_ROWS:
            raise ValueError("readback: wrong QSound trigger table size")
        if reader.header.proto.off_arg_byte != 0:
            raise ValueError("readback: wrong QSound record layout")
        if reader.header.proto.control_verbs != proto.control_verbs:
            raise ValueError("readback: wrong control-verb map")
        mapped = {row.command for row in music if row.in_game}
        for row in music:
            if not row.in_game:
                continue
            got = reader.triggers[row.command]
            if (got.verb, got.track, got.gain, got.suppress) != (
                    VERB_PLAY, track_of[row.song], TRIG_GAIN, 1):
                raise ValueError(
                    f"readback: command 0x{row.command:04x} does not map to "
                    f"{row.song}")
        for command, got in enumerate(reader.triggers):
            if command in mapped:
                continue
            if got.verb != VERB_NONE or got.suppress:
                raise ValueError(
                    f"readback: unmapped command 0x{command:04x} does not fail open")
        logo = reader.triggers[NATIVE_QSOUND_LOGO_CMD]
        if logo.verb != VERB_NONE or logo.suppress:
            raise ValueError(
                "readback: command 0x3d must pass through to native QSound")
    finally:
        reader.close()

    size = out_path.stat().st_size
    if snrs:
        mean = sum(value for _, value in snrs) / len(snrs)
        worst = min(snrs, key=lambda value: value[1])
        print(f"[ssf2] ADX SNR mean {mean:.2f} dB, worst "
              f"{worst[0]} {worst[1]:.2f} dB")
    play_count = sum(row.in_game for row in music)
    print(f"[ssf2] {out_path}: {len(music)} tracks, {play_count} play "
          f"commands, {sum(row.loops for row in audio.values())} loops, "
          f"{size / 1e6:.1f} MB")
    return {"path": out_path, "size": size, "tracks": len(music),
            "commands": play_count, "loops": 46, "snrs": snrs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--flac-dir", type=Path, default=SOURCE_DIR)
    ap.add_argument("--map", type=Path, default=MAP_TSV)
    ap.add_argument("--audio", type=Path, default=AUDIO_TSV)
    ap.add_argument("--measure-snr", action="store_true")
    args = ap.parse_args(argv)
    build(out=args.out, flac_dir=args.flac_dir, map_path=args.map,
          audio_path=args.audio, measure_snr=args.measure_snr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
