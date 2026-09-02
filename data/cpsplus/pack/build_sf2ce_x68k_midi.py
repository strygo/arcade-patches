"""Build the SF2CE X68000 external-MIDI pack for the CPS1 byte latch.

The audio input is the 46 stereo 48 kHz FLAC files produced by
``tools/export_x68k_midi_flac.py`` from the SF2CE X68000 disks (pass the
directory holding them with --flac-dir).  The command join is read directly from
``manifests/x68k_midi_arcade_map.tsv`` and must have passed the two-file
X68000/arcade listening gate before this builder accepts it.

Loop coordinates come from ``manifests/sf2ce_x68k_midi_loops.tsv``.  They
retain the extracted MIDI's exact period while shifting the phase one second
earlier, which leaves a real second-pass tail for the CPS+ crossfade.  Songs
absent from the loop table are one-shots.

Run:
  python3 build_pack.py sf2ce-x68k-midi --flac-dir <flac-dir>
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import adxcodec, protocols
from .adxencode import encode_adx_track, subtract_s16_dc
from .build_common import MANIFESTS, PACKS_DIR, PKG_ROOT
from .format import (CODEC_ADX, VERB_NONE, VERB_PLAY, VERB_STOP, PackReader,
                     PackWriter, TrackMeta, TriggerRow)


SOURCE_DIR = PKG_ROOT / "work" / "x68000" / "flac" / "midi" / "sf2ce"
MAP_TSV = MANIFESTS / "x68k_midi_arcade_map.tsv"
LOOPS_TSV = MANIFESTS / "sf2ce_x68k_midi_loops.tsv"
PACK_NAME = "sf2ce_x68k_midi.cpk"
TITLE = "Street Fighter II' Champion Edition (X68000 MIDI Soundtrack)"
RATE = 48_000
CHANNELS = 2
XFADE_SAMPLES = RATE                         # one second, 32-sample aligned

# Nuked-SC55 v1.21's float output sits at this signed-s16 value while silent.
# All 46 exported sources retain at least 381 identical initial frames.  ADX's
# predictive frame scaler turns the otherwise inaudible non-zero pedestal into
# a periodic tone, so validate 256 source frames and center every track without
# changing its duration or any loop coordinate.
SC55_DC = (1298, 1298)
DC_EVIDENCE_FRAMES = 256

# Measured (EBU R128 loudness match) against isolated native sf2ce board
# renders, remeasured after DC cleanup 2026-09-01.  The canonical equal
# mono-downmix comparison over all 46 user-approved pairs puts the encoded pack
# a median 1.1 LU BELOW the board (IQR 1.9 LU).  The format cannot boost above
# unity, so 0x7f is the closest reproducible match and avoids attenuating it.
TRIG_GAIN = 0x7f

# These verified cues have no counterpart in the older HSF2 arrange pack and
# therefore are not members of protocols.SF2_MUSIC_COMMANDS.  The X68000 port
# supplies all five: match-end, unused, game-over, challenger, and the
# four-boss ending.
EXTRA_MUSIC_COMMANDS = {0x10, 0x12, 0x13, 0x15, 0x8c}


@dataclass(frozen=True)
class MusicRow:
    song: str
    role: str
    commands: tuple[int, ...]
    in_game: bool


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
        if raw["game"] != "sf2ce":
            continue
        if raw["arcade_set"] != "sf2ce":
            raise ValueError(
                f"{raw['song']}: expected reviewed arcade set sf2ce, "
                f"got {raw['arcade_set']!r}")
        if raw["match_status"] != "verified":
            raise ValueError(
                f"{raw['song']}: listening gate is {raw['match_status']!r}, "
                "not 'verified'")
        if raw["in_game"] not in {"yes", "no"}:
            raise ValueError(f"{raw['song']}: invalid in_game value")
        commands = [int(raw["arcade_cmd"], 0)]
        commands.extend(int(v, 0) for v in (raw.get("arcade_aliases") or "").split(",")
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
        rows.append(MusicRow(raw["song"], raw["role"], tuple(commands),
                             raw["in_game"] == "yes"))

    expected_songs = {f"song{i:02x}" for i in range(0x2e)}
    got_songs = {r.song for r in rows}
    if got_songs != expected_songs:
        raise ValueError(
            "SF2CE map must contain exactly song00..song2d; "
            f"missing={sorted(expected_songs - got_songs)}, "
            f"extra={sorted(got_songs - expected_songs)}")
    expected_commands = protocols.SF2_MUSIC_COMMANDS | EXTRA_MUSIC_COMMANDS
    if set(claimed) != expected_commands:
        raise ValueError(
            "SF2CE map command vocabulary changed; "
            f"missing={sorted(expected_commands - set(claimed))}, "
            f"extra={sorted(set(claimed) - expected_commands)}")
    unused = [r.song for r in rows if not r.in_game]
    if unused != ["song11"]:
        raise ValueError(f"expected only song11 to be unused, got {unused}")
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
        source_period = loop.source_le - loop.source_ls
        pack_period = loop.pack_le - loop.pack_ls
        if abs(source_period - pack_period) >= 32:
            raise ValueError(f"{song}: pack loop changes the source period")
        if loop.pack_le + XFADE_SAMPLES > loop.source_le:
            raise ValueError(f"{song}: no source tail remains for crossfade")
        out[song] = loop
    if len(out) != 37:
        raise ValueError(f"expected 37 looped songs, got {len(out)}")
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
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le",
         "-acodec", "pcm_s16le", "-ac", str(CHANNELS), "-ar", str(RATE), "-"],
        capture_output=True, check=True)
    frame_bytes = CHANNELS * 2
    if len(proc.stdout) % frame_bytes:
        raise ValueError(f"{path.name}: decoded PCM ends mid-frame")
    return proc.stdout, len(proc.stdout) // frame_bytes


def build(*, out: Path | None = None, flac_dir: Path = SOURCE_DIR,
          map_path: Path = MAP_TSV, loops_path: Path = LOOPS_TSV,
          measure_snr: bool = False) -> dict:
    music = load_music_map(map_path)
    loops = load_loops(loops_path)
    proto = protocols.PROTOCOLS["sf2"]
    if proto.latch_page != 0x800180 or proto.handshake_ready != 0xf7:
        raise ValueError("sf2 protocol lacks the measured CPS1 suppression byte")
    if proto.control_verbs != {0xf7: VERB_STOP}:
        raise ValueError("sf2 protocol control map changed unexpectedly")

    writer = PackWriter(
        proto, title=TITLE, trigger_rows=protocols.SF2_TRIGGER_ROWS,
        default_rate=RATE, xfade_samples=XFADE_SAMPLES)
    track_of: dict[str, int] = {}
    snrs: list[tuple[str, float]] = []

    print(f"[sf2ce] === {TITLE} ===")
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
        pcm = subtract_s16_dc(
            pcm, CHANNELS, SC55_DC, evidence_frames=DC_EVIDENCE_FRAMES,
            name=src.name)
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
            source=f"sf2ce/{src.name}; dc={SC55_DC[0]},{SC55_DC[1]}")
        ti = writer.add_track(data, meta)
        track_of[row.song] = ti
        print(f"[sf2ce] t{ti:02d} {row.song} {keep / RATE:7.2f}s  "
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
        if reader.header.game_id != "sf2":
            raise ValueError("readback: wrong game id")
        if reader.header.proto.handshake_ready != 0xf7:
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
                if cmd != 0xf7 or got.verb != VERB_STOP or got.suppress:
                    raise ValueError(
                        f"readback: unmapped command 0x{cmd:02x} does not "
                        "fail open")
    finally:
        reader.close()

    size = out_path.stat().st_size
    if snrs:
        mean = sum(v for _, v in snrs) / len(snrs)
        worst = min(snrs, key=lambda v: v[1])
        print(f"[sf2ce] ADX SNR mean {mean:.2f} dB, worst "
              f"{worst[0]} {worst[1]:.2f} dB")
    print(f"[sf2ce] {out_path}: {len(music)} tracks, {command_count} play "
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
