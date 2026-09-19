#!/usr/bin/env python3
"""Audit X68000 Standard MIDI sequences and recover native loop points.

S.P.S. M2/M3 sequence conversions mark an infinite loop with MIDI controller
111: value 0 at the loop entry and value 1 at the first wrap.  This tool reads
those markers, converts their ticks through the file's tempo map, and emits a
machine-readable catalog.  It can also make an SC-55 playback copy.  Native GS
sequences receive a GS reset and short pre-roll; MT-32 sequences receive the
SC-55's MT-32-compatible tone map and the longer initialization window that
map requires.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import struct
import sys
import tempfile
from collections import Counter
from pathlib import Path


class MidiError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class MidiEvent:
    track: int
    tick: int
    status: int
    data: bytes
    meta_type: int | None = None

    @property
    def channel(self) -> int | None:
        return self.status & 0x0F if 0x80 <= self.status <= 0xEF else None


@dataclasses.dataclass(frozen=True)
class MidiFile:
    format: int
    division: int
    track_blobs: tuple[bytes, ...]
    events: tuple[MidiEvent, ...]


def _read_vlq(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise MidiError("truncated variable-length quantity")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise MidiError("variable-length quantity exceeds four bytes")


def _write_vlq(value: int) -> bytes:
    if not 0 <= value <= 0x0FFFFFFF:
        raise MidiError(f"VLQ value out of range: {value}")
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def _parse_track(blob: bytes, track: int) -> list[MidiEvent]:
    events: list[MidiEvent] = []
    pos = 0
    tick = 0
    running_status: int | None = None
    while pos < len(blob):
        delta, pos = _read_vlq(blob, pos)
        tick += delta
        if pos >= len(blob):
            raise MidiError(f"track {track}: missing event after delta")
        first = blob[pos]
        if first & 0x80:
            status = first
            pos += 1
            if 0x80 <= status <= 0xEF:
                running_status = status
        else:
            if running_status is None:
                raise MidiError(f"track {track}: data byte without running status")
            status = running_status
        if status == 0xFF:
            if pos >= len(blob):
                raise MidiError(f"track {track}: truncated meta event")
            meta_type = blob[pos]
            pos += 1
            length, pos = _read_vlq(blob, pos)
            end = pos + length
            if end > len(blob):
                raise MidiError(f"track {track}: truncated meta payload")
            events.append(MidiEvent(track, tick, status, blob[pos:end], meta_type))
            pos = end
            if meta_type == 0x2F:
                break
            continue
        if status in (0xF0, 0xF7):
            length, pos = _read_vlq(blob, pos)
            end = pos + length
            if end > len(blob):
                raise MidiError(f"track {track}: truncated SysEx payload")
            events.append(MidiEvent(track, tick, status, blob[pos:end]))
            pos = end
            running_status = None
            continue
        family = status & 0xF0
        if not 0x80 <= status <= 0xEF:
            raise MidiError(f"track {track}: unsupported status 0x{status:02x}")
        length = 1 if family in (0xC0, 0xD0) else 2
        end = pos + length
        if end > len(blob):
            raise MidiError(f"track {track}: truncated channel event")
        events.append(MidiEvent(track, tick, status, blob[pos:end]))
        pos = end
    return events


def parse_midi(data: bytes) -> MidiFile:
    if len(data) < 14 or data[:4] != b"MThd":
        raise MidiError("not a Standard MIDI file")
    header_size = struct.unpack_from(">I", data, 4)[0]
    if header_size < 6 or 8 + header_size > len(data):
        raise MidiError("invalid MIDI header size")
    fmt, track_count, division = struct.unpack_from(">HHH", data, 8)
    if division & 0x8000:
        raise MidiError("SMPTE MIDI timing is not supported")
    if not division:
        raise MidiError("zero MIDI timing division")
    pos = 8 + header_size
    blobs: list[bytes] = []
    events: list[MidiEvent] = []
    for track in range(track_count):
        if pos + 8 > len(data) or data[pos : pos + 4] != b"MTrk":
            raise MidiError(f"missing MTrk header for track {track}")
        length = struct.unpack_from(">I", data, pos + 4)[0]
        start = pos + 8
        end = start + length
        if end > len(data):
            raise MidiError(f"track {track} extends beyond the file")
        blob = data[start:end]
        blobs.append(blob)
        events.extend(_parse_track(blob, track))
        pos = end
    if pos != len(data):
        raise MidiError(f"{len(data) - pos} trailing bytes after final track")
    return MidiFile(fmt, division, tuple(blobs), tuple(events))


def _tempo_events(midi: MidiFile) -> list[tuple[int, int]]:
    tempos = [(0, 500_000)]
    for event in midi.events:
        if event.status == 0xFF and event.meta_type == 0x51 and len(event.data) == 3:
            tempos.append((event.tick, int.from_bytes(event.data, "big")))
    # At a duplicate tick, the last event in track order wins.
    by_tick: dict[int, int] = {}
    for tick, tempo in sorted(tempos, key=lambda item: item[0]):
        by_tick[tick] = tempo
    return sorted(by_tick.items())


def tick_to_seconds(midi: MidiFile, target: int) -> float:
    elapsed = 0.0
    last_tick = 0
    tempo = 500_000
    for tick, next_tempo in _tempo_events(midi):
        if tick > target:
            break
        elapsed += (tick - last_tick) * tempo / midi.division / 1_000_000
        last_tick = tick
        tempo = next_tempo
    elapsed += (target - last_tick) * tempo / midi.division / 1_000_000
    return elapsed


def _track_name(midi: MidiFile, track: int) -> str | None:
    for event in midi.events:
        if event.track == track and event.status == 0xFF and event.meta_type == 0x03:
            return event.data.decode("shift_jis", errors="replace").rstrip(" \0")
    return None


def loop_rows(midi: MidiFile) -> list[dict[str, object]]:
    """Return native M2/M3 loop rows.

    M2 conversions use CC111 value 0/1.  M3 sequences use CC60 value 0 at
    entry and either 1 (GM) or 127 (OPM) at exit.  Older SF2 M3 files instead
    put ``@P``/``@L`` MIDI marker events on their conductor track.
    """
    markers: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
    for event in midi.events:
        if event.status & 0xF0 == 0xB0 and event.data[0] in (60, 111):
            markers.setdefault(
                (event.track, event.status & 0x0F, event.data[0]), []
            ).append(
                (event.tick, event.data[1])
            )
    rows: list[dict[str, object]] = []
    for (track, channel, controller), values in sorted(markers.items()):
        start = next((tick for tick, value in values if value == 0), None)
        if start is None:
            continue
        end_row = next(
            ((tick, value) for tick, value in values if value != 0 and tick > start),
            None,
        )
        if end_row is None:
            continue
        end, end_value = end_row
        rows.append({
            "track": track,
            "track_name": _track_name(midi, track),
            "channel": channel + 1,
            "marker": f"cc{controller}",
            "end_value": end_value,
            "start_tick": start,
            "end_tick": end,
            "period_ticks": end - start,
            "start_seconds": round(tick_to_seconds(midi, start), 9),
            "end_seconds": round(tick_to_seconds(midi, end), 9),
            "period_seconds": round(
                tick_to_seconds(midi, end) - tick_to_seconds(midi, start), 9
            ),
        })
    meta_markers = [
        event for event in midi.events
        if event.status == 0xFF and event.meta_type == 0x06
    ]
    for start_tag, end_tag, marker_name in (
        (b"@P", b"@L", "meta-@P/@L"),
        (b"loopStart", b"loopEnd", "meta-loopStart/loopEnd"),
    ):
        start_event = next(
            (event for event in meta_markers if event.data == start_tag), None
        )
        end_event = next(
            (event for event in meta_markers
             if event.data == end_tag
             and start_event is not None
             and event.tick > start_event.tick),
            None,
        )
        if start_event is not None and end_event is not None:
            start = start_event.tick
            end = end_event.tick
            rows.append({
                "track": start_event.track,
                "track_name": _track_name(midi, start_event.track),
                "channel": None,
                "marker": marker_name,
                "end_value": None,
                "start_tick": start,
                "end_tick": end,
                "period_ticks": end - start,
                "start_seconds": round(tick_to_seconds(midi, start), 9),
                "end_seconds": round(tick_to_seconds(midi, end), 9),
                "period_seconds": round(
                    tick_to_seconds(midi, end) - tick_to_seconds(midi, start), 9
                ),
            })
    return rows


def _consensus(values: list[int]) -> int | None:
    if not values:
        return None
    counts = Counter(values)
    value, count = counts.most_common(1)[0]
    # A global audio loop is safe only when every looping sequence track agrees.
    return value if count == len(values) else None


def _composite_period(periods: list[int]) -> tuple[int | None, int | None]:
    """Return (usable period, exact LCM) for independently looping tracks.

    An exact LCM that is more than 16 times the longest native track cycle is
    almost always caused by tracks that differ by a few ticks (for example
    1534/1536/1538).  Such a song has no practical sample-exact short loop.
    Keep the exact value as evidence, but do not manufacture a loop point or
    try to encode hours/days of expanded MIDI.
    """
    if not periods:
        return None, None
    exact = math.lcm(*periods)
    usable = exact if exact <= max(periods) * 16 else None
    return usable, exact


def audit(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    midi = parse_midi(data)
    loops = loop_rows(midi)
    programs: set[tuple[int, int]] = set()
    banks: set[tuple[int, int, int]] = set()
    bank_state: dict[int, list[int]] = {channel: [0, 0] for channel in range(16)}
    manufacturers: set[str] = set()
    roland_models: set[str] = set()
    controllers: Counter[int] = Counter()
    meta_types: Counter[int] = Counter()
    meta_texts: list[dict[str, object]] = []
    track_end_ticks: dict[int, int] = {}
    gs_reset = False
    gm_reset = False
    max_tick = 0
    for event in sorted(midi.events, key=lambda item: (item.tick, item.track)):
        max_tick = max(max_tick, event.tick)
        family = event.status & 0xF0
        if family == 0xB0:
            controllers[event.data[0]] += 1
            channel = event.status & 0x0F
            if event.data[0] == 0:
                bank_state[channel][0] = event.data[1]
            elif event.data[0] == 32:
                bank_state[channel][1] = event.data[1]
        elif family == 0xC0:
            channel = event.status & 0x0F
            programs.add((channel + 1, event.data[0]))
            banks.add((channel + 1, *bank_state[channel]))
        elif event.status in (0xF0, 0xF7) and event.data:
            payload = event.data[:-1] if event.data[-1:] == b"\xf7" else event.data
            manufacturers.add(f"0x{payload[0]:02x}")
            if len(payload) >= 3 and payload[0] == 0x41:
                roland_models.add(f"0x{payload[2]:02x}")
            if len(payload) >= 8 and payload[0] == 0x41:
                gs_reset |= payload[2:8] == bytes.fromhex("421240007f00")
            gm_reset |= payload[:4] == bytes.fromhex("7e7f0901")
        elif event.status == 0xFF and event.meta_type is not None:
            meta_types[event.meta_type] += 1
            if 0x01 <= event.meta_type <= 0x07:
                meta_texts.append({
                    "track": event.track,
                    "tick": event.tick,
                    "type": f"0x{event.meta_type:02x}",
                    "text": event.data.decode("shift_jis", errors="replace").rstrip(" \0"),
                })
            if event.meta_type == 0x2F:
                track_end_ticks[event.track] = event.tick
    start = _consensus([int(row["start_tick"]) for row in loops])
    end = _consensus([int(row["end_tick"]) for row in loops])
    periods = [int(row["period_ticks"]) for row in loops]
    # M3 permits each track to loop independently.  The audible composite
    # repeats at the least common multiple (e.g. an 8-bar melody plus a
    # 1-bar hi-hat pattern), once every track has entered its loop.
    period, exact_period = _composite_period(periods)
    global_start = max((int(row["start_tick"]) for row in loops), default=None)
    global_end = None if global_start is None or period is None else global_start + period
    return {
        "source": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "format": midi.format,
        "tracks": len(midi.track_blobs),
        "division": midi.division,
        "duration_ticks": max_tick,
        "duration_seconds": round(tick_to_seconds(midi, max_tick), 9),
        "tempo_events": [
            {"tick": tick, "microseconds_per_quarter": tempo}
            for tick, tempo in _tempo_events(midi)
        ],
        "controllers": {
            str(controller): count for controller, count in sorted(controllers.items())
        },
        "meta_types": {
            f"0x{meta_type:02x}": count for meta_type, count in sorted(meta_types.items())
        },
        "meta_texts": meta_texts,
        "track_end_ticks": [
            {"track": track, "tick": tick}
            for track, tick in sorted(track_end_ticks.items())
        ],
        "loop_tracks": loops,
        "consensus_loop": None if start is None or end is None else {
            "start_tick": start,
            "end_tick": end,
            "start_seconds": round(tick_to_seconds(midi, start), 9),
            "end_seconds": round(tick_to_seconds(midi, end), 9),
        },
        "global_loop": None if global_start is None or global_end is None else {
            "start_tick": global_start,
            "end_tick": global_end,
            "period_ticks": period,
            "start_seconds": round(tick_to_seconds(midi, global_start), 9),
            "end_seconds": round(tick_to_seconds(midi, global_end), 9),
            "period_seconds": round(
                tick_to_seconds(midi, global_end)
                - tick_to_seconds(midi, global_start), 9
            ),
            "basis": "latest per-track entry plus LCM of native track periods",
        },
        "native_loop_lcm_ticks": exact_period,
        "loop_issue": (
            "native track periods have no practical short common cycle"
            if loops and period is None else None
        ),
        "programs": [
            {"channel": channel, "program_zero_based": program}
            for channel, program in sorted(programs)
        ],
        "banks": [
            {"channel": channel, "msb": msb, "lsb": lsb}
            for channel, msb, lsb in sorted(banks)
        ],
        "sysex_manufacturers": sorted(manufacturers),
        "roland_model_ids": sorted(roland_models),
        "has_gs_reset": gs_reset,
        "has_gm_reset": gm_reset,
    }


GS_RESET = bytes.fromhex("4110421240007f0041f7")
MT32_MODEL_ID = "0x16"
MT32_SETUP_MS = 6000

# Roland's documented SC-55 MT-32 sound arrangement.  Bank 127 selects the
# compatible melodic tones; program 128 on channel 10 selects the CM-64/32L
# drum set.  These are power-on setup values, not a rewrite of the song's own
# later program changes.
MT32_INITIAL_PROGRAMS = (0, 68, 48, 95, 78, 41, 3, 110, 122)
MT32_INITIAL_PANS = (64, 54, 54, 54, 54, 18, 91, 1, 127)


def _encode_track(events: list[MidiEvent], end_tick: int) -> bytes:
    blob = bytearray()
    previous = 0
    for event in sorted(enumerate(events), key=lambda item: (item[1].tick, item[0])):
        event = event[1]
        if event.status == 0xFF and event.meta_type == 0x2F:
            continue
        if event.tick < previous:
            raise MidiError("events are not in chronological order")
        blob += _write_vlq(event.tick - previous)
        previous = event.tick
        if event.status == 0xFF:
            if event.meta_type is None:
                raise MidiError("meta event has no type")
            blob += bytes((0xFF, event.meta_type))
            blob += _write_vlq(len(event.data)) + event.data
        elif event.status in (0xF0, 0xF7):
            blob += bytes((event.status,)) + _write_vlq(len(event.data)) + event.data
        else:
            blob += bytes((event.status,)) + event.data
    end_tick = max(end_tick, previous)
    blob += _write_vlq(end_tick - previous) + b"\xff\x2f\x00"
    return bytes(blob)


def _is_native_loop_control(event: MidiEvent) -> bool:
    if event.status & 0xF0 == 0xB0 and event.data[0] == 60:
        return True
    return (
        event.status == 0xFF
        and event.meta_type == 0x06
        and event.data in (b"@P", b"@L")
    )


def expand_m3_loops(source: Path, destination: Path, loop_count: int) -> None:
    """Expand native M3 loops so ordinary MIDI/SC-55 players repeat them."""
    if loop_count < 1:
        raise MidiError("loop count must be at least one")
    midi = parse_midi(source.read_bytes())
    rows = loop_rows(midi)
    if not rows:
        raise MidiError(f"{source} has no recognized native loop markers")
    if any(row["marker"] == "cc111" for row in rows):
        raise MidiError(
            "CC111 M2 files are already expanded by m2seq22mid; rerun that "
            "converter with the desired -Loops value"
        )
    native_rows = [
        row for row in rows
        if row["marker"] in ("cc60", "meta-@P/@L")
    ]
    if not native_rows:
        raise MidiError("only native M3 @P/@L and CC60 loops can be expanded")
    period, exact_period = _composite_period(
        [int(row["period_ticks"]) for row in native_rows]
    )
    if period is None:
        raise MidiError(
            "native track loops have no practical short common cycle "
            f"(exact LCM {exact_period} ticks)"
        )
    meta_row = next((row for row in rows if row["marker"] == "meta-@P/@L"), None)
    by_track = {
        int(row["track"]): row for row in rows if row["marker"] == "cc60"
    }
    if meta_row is None and not by_track:
        raise MidiError("only M3 @P/@L and CC60 loops can be expanded")
    source_by_track: list[list[MidiEvent]] = [
        [event for event in midi.events if event.track == track]
        for track in range(len(midi.track_blobs))
    ]
    expanded_tracks: list[bytes] = []
    source_end = max((event.tick for event in midi.events), default=0)
    global_start = max(int(row["start_tick"]) for row in native_rows)
    global_end = global_start + period
    final_end = max(source_end, global_start + loop_count * period)
    for track, track_events in enumerate(source_by_track):
        spec = meta_row if meta_row is not None else by_track.get(track)
        if spec is None:
            output = [event for event in track_events
                      if not (event.status == 0xFF and event.meta_type == 0x2F)]
        else:
            start = int(spec["start_tick"])
            end = int(spec["end_tick"])
            prefix = [event for event in track_events
                      if event.tick < start and not _is_native_loop_control(event)]
            body = [event for event in track_events
                    if start <= event.tick <= end and not _is_native_loop_control(event)
                    and not (event.status == 0xFF and event.meta_type == 0x2F)]
            output = list(prefix)
            repetitions = math.ceil((final_end - start) / (end - start))
            for repetition in range(repetitions):
                shift = repetition * (end - start)
                output.extend(
                    dataclasses.replace(event, tick=event.tick + shift)
                    for event in body if event.tick + shift <= final_end
                )
        if track == 0:
            output.append(MidiEvent(0, global_start, 0xFF, b"loopStart", 0x06))
            output.append(MidiEvent(0, global_end, 0xFF, b"loopEnd", 0x06))
        expanded_tracks.append(_encode_track(output, final_end))
    out = bytearray()
    out += b"MThd" + struct.pack(
        ">IHHH", 6, 1, len(expanded_tracks), midi.division
    )
    for blob in expanded_tracks:
        out += b"MTrk" + struct.pack(">I", len(blob)) + blob
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(out)


def _scaled_tick(division: int, reference_tick: int) -> int:
    """Scale a tick from Roland's 96 PPQN setup sequence."""
    return round(reference_tick * division / 96)


def _mt32_setup_track(midi: MidiFile, preroll_ms: int) -> bytes:
    """Build the SC-55 MT-32-compatible power-on arrangement.

    The timing and values follow Roland's SC-55 MT-32 arrangement procedure.
    Six seconds are retained before the source song starts so the hardware
    reset and serial SysEx writes complete on a physical module too.
    """
    events = [
        MidiEvent(0, 0, 0xFF, b"SC-55 MT-32 map setup", 0x03),
        MidiEvent(0, 0, 0xFF, b"\x07\xa1\x20", 0x51),  # 120 BPM
        MidiEvent(0, 0, 0xF0, GS_RESET),
    ]

    # Drum part first: CM-64/32L set (one-based program 128).
    drum_tick = _scaled_tick(midi.division, 96)
    events.extend([
        MidiEvent(0, drum_tick, 0xB9, bytes((0, 0))),
        MidiEvent(0, drum_tick, 0xC9, bytes((127,))),
        MidiEvent(0, drum_tick, 0xB9, bytes((10, 64))),
        MidiEvent(0, drum_tick, 0xB9, bytes((91, 64))),
    ])

    for channel, (program, pan) in enumerate(
        zip(MT32_INITIAL_PROGRAMS, MT32_INITIAL_PANS)
    ):
        tick = _scaled_tick(midi.division, 101 + 5 * channel)
        status = 0xB0 | channel
        events.extend([
            MidiEvent(0, tick, status, bytes((0, 127))),
            MidiEvent(0, tick, 0xC0 | channel, bytes((program,))),
            MidiEvent(0, tick, status, bytes((10, pan))),
            MidiEvent(0, tick, status, bytes((91, 64))),
        ])

    # MT-32 pitch-bend range is twelve semitones on all ten active parts.
    for channel in (9, *range(9)):
        tick = _scaled_tick(midi.division, 192 + 5 * ((channel + 1) % 10))
        status = 0xB0 | channel
        events.extend([
            MidiEvent(0, tick, status, bytes((100, 0))),
            MidiEvent(0, tick, status, bytes((101, 0))),
            MidiEvent(0, tick, status, bytes((6, 12))),
            MidiEvent(0, tick, status, bytes((38, 0))),
            MidiEvent(0, tick, status, bytes((100, 127))),
            MidiEvent(0, tick, status, bytes((101, 127))),
        ])

    # Apply Roland's documented per-part MT-32 arrangement value.
    chorus_tick = _scaled_tick(midi.division, 288)
    for part in range(10):
        address = 0x20 + part
        checksum = (-0x40 - address - 0x04 - 0x04) & 0x7F
        events.append(MidiEvent(
            0, chorus_tick, 0xF0,
            bytes((0x41, 0x10, 0x42, 0x12, 0x40, address, 0x04, 0x04,
                   checksum, 0xF7)),
        ))
    events.extend([
        MidiEvent(0, _scaled_tick(midi.division, 298), 0xB9, bytes((11, 80))),
        MidiEvent(
            0, _scaled_tick(midi.division, 308), 0xF0,
            bytes.fromhex("4110421240100e0022f7"),
        ),
        MidiEvent(
            0, _scaled_tick(midi.division, 318), 0xF0,
            bytes.fromhex("411042124001310004356a6bf7"),
        ),
    ])
    setup_ms = max(preroll_ms, MT32_SETUP_MS)
    end_tick = max(1, round(midi.division * 2 * setup_ms / 1000))
    return _encode_track(events, end_tick)


def prepare_sc55(
    source: Path, destination: Path, preroll_ms: int = 500,
    sound_map: str = "auto",
) -> None:
    data = source.read_bytes()
    midi = parse_midi(data)
    audit_row = audit(source)
    unsupported = set(audit_row["sysex_manufacturers"]) - {"0x41", "0x7e", "0x7f"}
    if unsupported:
        raise MidiError(
            f"{source} uses non-Roland SysEx ({', '.join(sorted(unsupported))}); "
            "refusing to label it SC-55-ready"
        )
    if audit_row["has_gm_reset"]:
        raise MidiError(
            f"{source} sends a GM reset after the inserted GS setup; refusing "
            "to label it SC-55-ready"
        )
    if sound_map not in ("auto", "gs", "mt32"):
        raise MidiError(f"unsupported SC-55 sound map: {sound_map}")
    source_is_mt32 = MT32_MODEL_ID in audit_row["roland_model_ids"]
    selected_map = "mt32" if sound_map == "auto" and source_is_mt32 else sound_map
    if selected_map == "auto":
        selected_map = "gs"
    if selected_map == "gs" and source_is_mt32:
        raise MidiError(
            f"{source} contains Roland MT-32 model-ID 0x16 SysEx; refusing "
            "to force its patch numbers through the GS map"
        )
    # Establish 120 BPM during the reset wait.  The original tempo events are
    # shifted with the music and take over before any source note is emitted.
    if selected_map == "mt32":
        setup = _mt32_setup_track(midi, preroll_ms)
        setup_ms = max(preroll_ms, MT32_SETUP_MS)
    else:
        setup_ms = preroll_ms
        setup_events = [
            MidiEvent(0, 0, 0xFF, b"SC-55 setup", 0x03),
            MidiEvent(0, 0, 0xFF, b"\x07\xa1\x20", 0x51),
            MidiEvent(0, 0, 0xF0, GS_RESET),
        ]
        setup_end = max(1, round(midi.division * 2 * setup_ms / 1000))
        setup = _encode_track(setup_events, setup_end)
    preroll_ticks = max(1, round(midi.division * 2 * setup_ms / 1000))
    shifted: list[bytes] = []
    for blob in midi.track_blobs:
        first_delta, pos = _read_vlq(blob, 0)
        shifted.append(_write_vlq(first_delta + preroll_ticks) + blob[pos:])
    out = bytearray()
    out += b"MThd" + struct.pack(">IHHH", 6, 1, len(shifted) + 1, midi.division)
    for blob in [setup, *shifted]:
        out += b"MTrk" + struct.pack(">I", len(blob)) + blob
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(out)


def self_test() -> None:
    track = bytearray()
    track += b"\x00\xff\x51\x03\x07\xa1\x20"  # 120 BPM
    track += b"\x00\xb0\x6f\x00"
    track += _write_vlq(96) + b"\xb0\x6f\x01"
    track += b"\x00\xff\x2f\x00"
    raw = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 48)
    raw += b"MTrk" + struct.pack(">I", len(track)) + track
    midi = parse_midi(raw)
    rows = loop_rows(midi)
    assert len(rows) == 1
    assert rows[0]["start_tick"] == 0 and rows[0]["end_tick"] == 96

    # The implicit 120 BPM default must not sort after a faster real tempo at
    # tick zero.  Python tuple sorting by both fields would incorrectly make
    # 500000 win over 272727 here.
    tempo_track = b"\x00\xff\x51\x03\x04\x29\x57\x00\xff\x2f\x00"
    tempo_raw = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 48)
    tempo_raw += b"MTrk" + struct.pack(">I", len(tempo_track)) + tempo_track
    tempo_midi = parse_midi(tempo_raw)
    assert _tempo_events(tempo_midi) == [(0, 272727)]
    assert round(tick_to_seconds(tempo_midi, 48), 6) == 0.272727
    assert rows[0]["period_seconds"] == 1.0
    assert _write_vlq(0x1FFFFF) == b"\xff\xff\x7f"
    assert _composite_period([96, 48]) == (96, 96)
    assert _composite_period([1534, 1536, 1538])[0] is None

    # Two independently looping M3 tracks: a 96-tick phrase and a nested
    # 48-tick rhythm.  Expansion must produce one 96-tick composite marker,
    # remove native CC60 controls, and survive the SC-55 reset wrapper.
    tracks = [
        _encode_track([
            MidiEvent(0, 0, 0xFF, b"\x07\xa1\x20", 0x51),
        ], 96),
        _encode_track([
            MidiEvent(1, 0, 0xB0, b"\x3c\x00"),
            MidiEvent(1, 0, 0x90, b"\x3c\x64"),
            MidiEvent(1, 24, 0x80, b"\x3c\x00"),
            MidiEvent(1, 96, 0xB0, b"\x3c\x7f"),
        ], 96),
        _encode_track([
            MidiEvent(2, 0, 0xB1, b"\x3c\x00"),
            MidiEvent(2, 0, 0x91, b"\x24\x64"),
            MidiEvent(2, 12, 0x81, b"\x24\x00"),
            MidiEvent(2, 48, 0xB1, b"\x3c\x7f"),
        ], 48),
    ]
    m3 = bytearray(b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), 48))
    for track_blob in tracks:
        m3 += b"MTrk" + struct.pack(">I", len(track_blob)) + track_blob
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.mid"
        expanded = root / "expanded.mid"
        sc55 = root / "sc55.mid"
        source.write_bytes(m3)
        expand_m3_loops(source, expanded, 2)
        expanded_audit = audit(expanded)
        assert expanded_audit["global_loop"]["period_ticks"] == 96
        assert "60" not in expanded_audit["controllers"]
        prepare_sc55(expanded, sc55)
        sc55_audit = audit(sc55)
        assert sc55_audit["has_gs_reset"]
        assert sc55_audit["global_loop"]["period_ticks"] == 96
        assert sc55_audit["roland_model_ids"] == ["0x42"]

        mt32_track = _encode_track([
            MidiEvent(0, 0, 0xF0, bytes.fromhex("4110161210000101050366f7")),
            MidiEvent(0, 0, 0xC1, bytes((68,))),
        ], 96)
        mt32_raw = bytearray(b"MThd" + struct.pack(
            ">IHHH", 6, 0, 1, 48
        ))
        mt32_raw += b"MTrk" + struct.pack(">I", len(mt32_track)) + mt32_track
        mt32_source = root / "mt32.mid"
        mt32_sc55 = root / "mt32-sc55.mid"
        mt32_source.write_bytes(mt32_raw)
        assert audit(mt32_source)["roland_model_ids"] == ["0x16"]
        prepare_sc55(mt32_source, mt32_sc55)
        mt32_audit = audit(mt32_sc55)
        assert mt32_audit["duration_seconds"] == 7.0
        mt32_setup_sysex = [
            event.data for event in parse_midi(mt32_sc55.read_bytes()).events
            if event.track == 0 and event.status == 0xF0
        ]
        assert bytes.fromhex("411042124020040418f7") in mt32_setup_sysex
        assert bytes.fromhex("41104212402904040ff7") in mt32_setup_sysex
        assert mt32_audit["banks"] == [
            {"channel": channel, "msb": 127, "lsb": 0}
            for channel in range(1, 10)
        ] + [{"channel": 10, "msb": 0, "lsb": 0}]
        try:
            prepare_sc55(mt32_source, root / "wrong-map.mid", sound_map="gs")
        except MidiError as exc:
            assert "MT-32 model-ID" in str(exc)
        else:
            raise AssertionError("MT-32 source was incorrectly accepted as GS")
    print("x68k_midi self-test: OK")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="command")
    analyze = sub.add_parser("analyze", help="audit MIDI files and loop markers")
    analyze.add_argument("midi", nargs="+", type=Path)
    analyze.add_argument("-o", "--output", type=Path)
    sc55 = sub.add_parser(
        "prepare-sc55", help="prepare GS or MT-32-map playback on an SC-55"
    )
    sc55.add_argument("source", type=Path)
    sc55.add_argument("destination", type=Path)
    sc55.add_argument("--preroll-ms", type=int, default=500)
    sc55.add_argument(
        "--sound-map", choices=("auto", "gs", "mt32"), default="auto"
    )
    expand = sub.add_parser(
        "expand-loops", help="expand M3 @P/@L or CC60 loops for ordinary players"
    )
    expand.add_argument("source", type=Path)
    expand.add_argument("destination", type=Path)
    expand.add_argument("--loops", type=int, default=2)
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if args.command == "analyze":
        rows = [audit(path) for path in args.midi]
        text = json.dumps(rows, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8", newline="\n")
        else:
            print(text, end="")
        return 0
    if args.command == "prepare-sc55":
        if args.preroll_ms < 0:
            parser.error("--preroll-ms must be non-negative")
        prepare_sc55(
            args.source, args.destination, args.preroll_ms, args.sound_map
        )
        print(f"wrote SC-55 playback MIDI: {args.destination}")
        return 0
    if args.command == "expand-loops":
        expand_m3_loops(args.source, args.destination, args.loops)
        print(f"wrote {args.loops}-pass MIDI: {args.destination}")
        return 0
    parser.error("choose analyze or prepare-sc55, or use --self-test")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MidiError, OSError) as exc:
        print(f"x68k_midi: error: {exc}", file=sys.stderr)
        raise SystemExit(1)
