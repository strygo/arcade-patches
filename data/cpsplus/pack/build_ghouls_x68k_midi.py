"""Build the Ghouls'n Ghosts X68000 external-MIDI pack for `ghoulsu`.

The audio input is the 27 stereo 48 kHz FLAC files produced by
``tools/export_x68k_midi_flac.py`` from the Daimakaimura X68000 disks
(pass the directory holding them with --flac-dir).  The reviewed command join is read
directly from ``manifests/x68k_midi_arcade_map.tsv``; aliases are expanded
into explicit CPS1 trigger rows and every mapped cue suppresses the native
YM2151/OKI music.

Loop coordinates come from ``manifests/ghouls_x68k_midi_loops.tsv``.  They
retain the extracted MIDI's exact period while shifting the phase one second
earlier, which leaves a real second-pass tail for the CPS+ crossfade.  Songs
absent from the loop table are one-shots.

Run:
  python3 build_pack.py ghouls-x68k-midi --flac-dir <flac-dir>
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


SOURCE_DIR = PKG_ROOT / "work" / "x68000" / "flac" / "midi" / "daimakaimura"
MAP_TSV = MANIFESTS / "x68k_midi_arcade_map.tsv"
LOOPS_TSV = MANIFESTS / "ghouls_x68k_midi_loops.tsv"
PACK_NAME = "ghouls_x68k_midi.cpk"
TITLE = "Ghouls'n Ghosts (X68000 MIDI Soundtrack)"
RATE = 48_000
CHANNELS = 2
XFADE_SAMPLES = RATE                         # one second, 32-sample aligned

# Measured EBU R128 match against the native ghoulsu board renders, 2026-08-31.
# Fourteen sustained primary-command pairs gave arranged-native deltas of
# +1.4..+10.1 LU, median +6.7 LU (IQR 2.9):
# round(0x7f * 10**(-6.7/20)) = 0x3b.  Baked into the builder so a from-source
# rebuild stays byte-identical and preserves the native music/SFX balance.
TRIG_GAIN = 0x3b


@dataclass(frozen=True)
class MusicRow:
    song: str
    role: str
    commands: tuple[int, ...]


@dataclass(frozen=True)
class LoopRow:
    source_ls: int
    source_le: int
    pack_ls: int
    pack_le: int


def _dict_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as f:
        yield from csv.DictReader(
            (line for line in f if line.strip() and not line.startswith("#")),
            delimiter="\t")


def load_music_map(path: Path = MAP_TSV) -> list[MusicRow]:
    rows: list[MusicRow] = []
    claimed: dict[int, str] = {}
    for raw in _dict_rows(path):
        if raw["game"] != "daimakaimura":
            continue
        if raw["arcade_set"] != "ghoulsu":
            raise ValueError(
                f"{raw['song']}: expected reviewed arcade set ghoulsu, "
                f"got {raw['arcade_set']!r}")
        if raw["match_status"] != "verified":
            raise ValueError(
                f"{raw['song']}: listening gate is {raw['match_status']!r}, "
                "not 'verified'")
        if raw["in_game"] != "yes":
            raise ValueError(f"{raw['song']}: reviewed pack row is not in-game")
        commands = [int(raw["arcade_cmd"], 0)]
        commands.extend(int(v, 0) for v in raw["arcade_aliases"].split(",")
                        if v.strip())
        for cmd in commands:
            if not 0 <= cmd < 0xf0:
                raise ValueError(
                    f"{raw['song']}: music command 0x{cmd:02x} overlaps the "
                    "CPS1 control region")
            if cmd in claimed:
                raise ValueError(
                    f"command 0x{cmd:02x} claimed by both {claimed[cmd]} and "
                    f"{raw['song']}")
            claimed[cmd] = raw["song"]
        rows.append(MusicRow(raw["song"], raw["role"], tuple(commands)))

    expected_songs = {f"song{i:02x}" for i in range(0x1b)}
    got_songs = {r.song for r in rows}
    if got_songs != expected_songs:
        raise ValueError(
            "Daimakaimura map must contain exactly song00..song1a; "
            f"missing={sorted(expected_songs - got_songs)}, "
            f"extra={sorted(got_songs - expected_songs)}")
    if len(claimed) != 64:
        raise ValueError(
            f"Daimakaimura map must expand to 64 primary/alias commands, "
            f"got {len(claimed)}")
    return sorted(rows, key=lambda r: int(r.song[4:], 16))


def load_loops(path: Path = LOOPS_TSV) -> dict[str, LoopRow]:
    out: dict[str, LoopRow] = {}
    for raw in _dict_rows(path):
        song = raw["song"]
        if song in out:
            raise ValueError(f"duplicate loop row for {song}")
        loop = LoopRow(*(int(raw[k]) for k in
                         ("source_ls", "source_le", "pack_ls", "pack_le")))
        if loop.pack_ls % 32 or loop.pack_le % 32:
            raise ValueError(f"{song}: pack loop is not on the ADX grid")
        if loop.source_le <= loop.source_ls or loop.pack_le <= loop.pack_ls:
            raise ValueError(f"{song}: invalid loop interval")
        expected_ls = (loop.source_ls - XFADE_SAMPLES) // 32 * 32
        expected_le = (loop.source_le - XFADE_SAMPLES) // 32 * 32
        if (loop.pack_ls, loop.pack_le) != (expected_ls, expected_le):
            raise ValueError(
                f"{song}: pack loop must be the one-second shifted source "
                "loop rounded down to the ADX grid")
        # The phase shift may lose at most 31 samples to the ADX grid.  A
        # larger period change would mean the authored musical loop changed.
        source_period = loop.source_le - loop.source_ls
        pack_period = loop.pack_le - loop.pack_ls
        if abs(source_period - pack_period) >= 32:
            raise ValueError(f"{song}: pack loop changes the source period")
        if loop.pack_le + XFADE_SAMPLES > loop.source_le:
            raise ValueError(f"{song}: no source tail remains for crossfade")
        out[song] = loop
    if len(out) != 14:
        raise ValueError(f"expected 14 looped songs, got {len(out)}")
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


def decode_flac(path: Path) -> tuple[bytes, int]:
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
    pcm = (s32 >> 16).astype("<i2").tobytes()
    return pcm, s32.size // CHANNELS


def build(*, out: Path | None = None, flac_dir: Path = SOURCE_DIR,
          map_path: Path = MAP_TSV, loops_path: Path = LOOPS_TSV,
          measure_snr: bool = False) -> dict:
    music = load_music_map(map_path)
    loops = load_loops(loops_path)
    proto = protocols.PROTOCOLS["ghouls"]
    if proto.latch_page != 0x800180 or proto.handshake_ready != 0xf0:
        raise ValueError("ghouls protocol lacks the measured CPS1 stop byte")
    if proto.control_verbs != {
            0xf0: VERB_STOP, 0xf1: VERB_STOP, 0xf2: VERB_STOP}:
        raise ValueError("ghouls protocol control map changed unexpectedly")

    writer = PackWriter(
        proto, title=TITLE, trigger_rows=protocols.SF2_TRIGGER_ROWS,
        default_rate=RATE, xfade_samples=XFADE_SAMPLES)
    track_of: dict[str, int] = {}
    snrs: list[tuple[str, float]] = []

    print(f"[ghouls] === {TITLE} ===")
    for row in music:
        src = flac_dir / f"{row.song}.flac"
        if not src.is_file():
            raise FileNotFoundError(f"missing rendered MIDI source: {src}")
        probe = probe_flac(src)
        if (probe["rate"], probe["channels"]) != (RATE, CHANNELS):
            raise ValueError(
                f"{src.name}: expected {RATE} Hz stereo, got "
                f"{probe['rate']} Hz/{probe['channels']}ch")
        tags = probe["tags"]
        pcm, frames = decode_flac(src)
        loop = loops.get(row.song)
        if loop:
            for tag, want in (("LOOPSTART", loop.source_ls),
                              ("LOOPEND", loop.source_le)):
                if tag not in tags or int(tags[tag]) != want:
                    raise ValueError(
                        f"{src.name}: {tag}={tags.get(tag)!r}, expected {want}")
            keep = loop.pack_le + XFADE_SAMPLES
            if keep > frames:
                raise ValueError(f"{src.name}: crossfade tail exceeds source")
            pcm = pcm[:keep * CHANNELS * 2]
            ls, le, xfade = loop.pack_ls, loop.pack_le, 1
            kind = "xfade loop"
        else:
            if "LOOPSTART" in tags or "LOOPEND" in tags:
                raise ValueError(f"{src.name}: untracked embedded loop tags")
            keep, ls, le, xfade = frames, 0, 0, 0
            kind = "one-shot"

        data, coef1, coef2, snr = encode_adx_track(
            pcm, RATE, CHANNELS, keep, row.song, measure_snr)
        if snr is not None:
            snrs.append((row.song, snr))
        meta = TrackMeta(
            sample_rate=RATE, channels=CHANNELS, codec=CODEC_ADX, gain=0x7f,
            loop_start_sample=ls,
            loop_start_byte=(adxcodec.samples_to_stream_byte(ls, CHANNELS)
                             if loop else 0),
            loop_end_sample=le,
            loop_end_byte=(adxcodec.samples_to_stream_byte(le, CHANNELS)
                           if loop else 0),
            coef1=coef1, coef2=coef2, xfade_enable=xfade,
            name=f"{row.song} {row.role} ({kind})",
            source=f"daimakaimura/{src.name}")
        ti = writer.add_track(data, meta)
        track_of[row.song] = ti
        print(f"[ghouls] t{ti:02d} {row.song} {keep / RATE:7.2f}s  "
              f"{kind:<11} {len(data) / 1e6:6.2f} MB  {row.role}")

    command_count = 0
    for row in music:
        ti = track_of[row.song]
        trigger = TriggerRow(
            verb=VERB_PLAY, track=ti, gain=TRIG_GAIN, suppress=1)
        for cmd in row.commands:
            writer.set_trigger(cmd, trigger)
            command_count += 1

    out_path = out or (PACKS_DIR / PACK_NAME)
    writer.write(out_path)

    reader = PackReader(out_path)
    try:
        if reader.header.game_id != "ghouls":
            raise ValueError("readback: wrong game id")
        if reader.header.proto.handshake_ready != 0xf0:
            raise ValueError("readback: wrong suppression substitute")
        if reader.header.proto.control_verbs != proto.control_verbs:
            raise ValueError("readback: wrong control-verb map")
        for row in music:
            for cmd in row.commands:
                got = reader.triggers[cmd]
                if (got.verb, got.track, got.gain, got.suppress) != (
                        VERB_PLAY, track_of[row.song], TRIG_GAIN, 1):
                    raise ValueError(
                        f"readback: command 0x{cmd:02x} does not map to "
                        f"{row.song}")
        mapped = {cmd for row in music for cmd in row.commands}
        for cmd, got in enumerate(reader.triggers):
            if cmd not in mapped and (got.verb != VERB_NONE or got.suppress):
                raise ValueError(
                    f"readback: unmapped command 0x{cmd:02x} does not fail open")
    finally:
        reader.close()

    size = out_path.stat().st_size
    if snrs:
        mean = sum(v for _, v in snrs) / len(snrs)
        worst = min(snrs, key=lambda v: v[1])
        print(f"[ghouls] ADX SNR mean {mean:.2f} dB, worst "
              f"{worst[0]} {worst[1]:.2f} dB")
    print(f"[ghouls] {out_path}: {len(music)} tracks, {command_count} play "
          f"commands, {len(loops)} loops, {size / 1e6:.1f} MB")
    return {"path": out_path, "size": size, "tracks": len(music),
            "commands": command_count, "loops": len(loops), "snrs": snrs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--flac-dir", type=Path, default=SOURCE_DIR)
    ap.add_argument("--map", type=Path, default=MAP_TSV)
    ap.add_argument("--loops", type=Path, default=LOOPS_TSV)
    ap.add_argument("--measure-snr", action="store_true")
    args = ap.parse_args(argv)
    build(out=args.out, flac_dir=args.flac_dir, map_path=args.map,
          loops_path=args.loops, measure_snr=args.measure_snr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
