"""Audition (pack -> WAV) and verify (structural + loop-seam + budget).

Audition is THE loop-joint acceptance check: it renders intro + N loop
passes exactly as the FPGA player will (linear decode; wrap = jump from the
loop-end byte back to the loop-start byte, which for a linearly-decoded
stream is a PCM splice at the corresponding samples).  Listen to the seams.
"""
from __future__ import annotations

import array
import wave
from pathlib import Path

from . import adxcodec, xfade
from .format import (PackReader, TrackMeta, CODEC_ADX, CODEC_PCM, VERB_PLAY,
                     VERB_NAMES, DDR_BUDGET_BYTES, TRACK_ALIGN)


# ------------------------------------------------------------- audition ----
def _decode_track(rd: PackReader, i: int) -> bytes:
    m = rd.tracks[i]
    data = rd.read_track(i)
    if m.codec == CODEC_ADX:
        n = m.data_length // (18 * m.channels) * 32
        return adxcodec.decode(data, m.channels, m.sample_rate,
                               total_samples=n)
    return data


def _loop_points_samples(m: TrackMeta) -> tuple[int, int]:
    """(loop_start, loop_end) in samples, derived from the byte fields
    (the byte fields are the authoritative wrap points)."""
    if m.codec == CODEC_ADX:
        unit = 18 * m.channels
        return (m.loop_start_byte // unit * 32, m.loop_end_byte // unit * 32)
    unit = 2 * m.channels
    return m.loop_start_byte // unit, m.loop_end_byte // unit


def render(rd: PackReader, track: int, loops: int = 2) -> tuple[bytes, TrackMeta]:
    """PCM render of a track: intro + `loops` wrapped loop passes.

    Crossfade tracks (xfade_enable) render exactly as the FPGA player: the tail
    past loop_end is decoded and equal-power blended against the loop head
    (pack/xfade.py, byte-for-byte identical to rtl/cpsplus_player.v)."""
    m = rd.tracks[track]
    pcm = _decode_track(rd, track)
    if not m.loops or (loops <= 0 and not m.loop_count):
        return pcm, m
    start, end = _loop_points_samples(m)
    fb = 2 * m.channels
    if m.loop_count:
        # finite-count track: the structure is the TRACK's, not the caller's --
        # intro, loop_count wraps, then play THROUGH loop_end to the stream end
        # (the source's own outro/fade), exactly as the player emits it.
        loops = m.loop_count
    if m.xfade_enable:
        n = rd.header.xfade_samples
        a = array.array("h")
        a.frombytes(pcm)
        ch = m.channels
        pairs = [(a[i * ch], a[i * ch + (ch - 1)]) for i in range(len(a) // ch)]
        # xfade.render(loops=L) emits L+1 blends (intro seam + L body seams);
        # a finite-count track wraps loop_count times = loop_count blends
        model_loops = (m.loop_count - 1) if m.loop_count else loops
        out_pairs = xfade.render(pairs, start, end, n, model_loops)
        o = array.array("h")
        for l, r in out_pairs:
            o.append(l)
            if ch == 2:
                o.append(r)
        rendered = o.tobytes()
        if m.loop_count:
            # final pass: from loop_start+n straight through to the stream end,
            # raw -- the blend material past loop_end is the natural
            # continuation, delivered unblended on the last pass
            rendered += pcm[(start + n) * fb:]
        return rendered, m
    intro = pcm[:end * fb]
    body = pcm[start * fb:end * fb]
    rendered = intro + body * loops
    if m.loop_count:
        rendered += pcm[end * fb:]           # play-through outro after last wrap
    return rendered, m


def audition(pack_path: str, track: int | None = None, cmd: int | None = None,
             loops: int = 2, out: str | None = None) -> Path:
    rd = PackReader(pack_path)
    try:
        if cmd is not None:
            row = rd.triggers[cmd]
            if row.verb != VERB_PLAY:
                raise ValueError(
                    f"cmd 0x{cmd:04x} verb is {VERB_NAMES[row.verb]!r}, "
                    f"not a play trigger")
            track = row.track
        if track is None:
            raise ValueError("need --track or --cmd")
        pcm, m = render(rd, track, loops)
        label = m.name or f"track{track:03d}"
        stem = Path(pack_path).stem
        suffix = f"_cmd{cmd:02x}" if cmd is not None else ""
        default_dir = Path(__file__).resolve().parent.parent / "work" / \
            "listening"
        out_path = Path(out) if out and not Path(out).is_dir() else \
            (Path(out) if out else default_dir) / \
            f"{stem}{suffix}_t{track:03d}_{label}_x{loops}.wav"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(m.channels)
            wf.setsampwidth(2)
            wf.setframerate(m.sample_rate)
            wf.writeframes(pcm)
        dur = len(pcm) / (2 * m.channels) / m.sample_rate
        print(f"[audition] track {track} ({label}) "
              f"{'looped x' + str(loops) if m.loops else 'one-shot'} -> "
              f"{out_path}  ({dur:.1f} s)")
        return out_path
    finally:
        rd.close()


# --------------------------------------------------------------- verify ----
def _seam_metric(pcm: bytes, m: TrackMeta) -> tuple[float, int]:
    """(ratio, joint_step): discontinuity of the loop wrap vs the local
    per-sample step scale.  ~1 means the seam moves like the music around
    it; large values suggest a bad loop point."""
    start, end = _loop_points_samples(m)
    a = array.array("h")
    a.frombytes(pcm)
    ch = m.channels
    n = len(a) // ch
    end = min(end, n)
    joint = 0
    for c in range(ch):
        joint = max(joint, abs(a[start * ch + c] - a[(end - 1) * ch + c]))
    win = 1024
    steps = total = 0
    for lo, hi in ((max(end - win, 1), end), (start, min(start + win, n))):
        for i in range(lo, hi - 1):
            for c in range(ch):
                steps += abs(a[(i + 1) * ch + c] - a[i * ch + c])
                total += 1
    scale = steps / max(total, 1)
    return joint / (scale + 1.0), joint


def verify(pack_path: str, quick: bool = False) -> bool:
    rd = PackReader(pack_path)
    h = rd.header
    problems: list[str] = []
    warns: list[str] = []

    actual_size = Path(pack_path).stat().st_size
    if actual_size != h.file_size:
        problems.append(f"file size {actual_size} != header {h.file_size}")
    ok_t, ok_d = rd.crc_check()
    if not ok_t:
        problems.append("trigger/index CRC mismatch")
    if not ok_d:
        problems.append("track data CRC mismatch")

    n_play = n_stop = 0
    for cmd, r in enumerate(rd.triggers):
        if r.verb == 0:
            continue
        if r.verb not in (1, 2):
            problems.append(f"cmd 0x{cmd:04x}: bad verb {r.verb}")
        elif r.verb == VERB_PLAY:
            n_play += 1
            if r.track >= h.track_count:
                problems.append(f"cmd 0x{cmd:04x}: track {r.track} "
                                f"out of range")
        else:
            n_stop += 1

    for i, m in enumerate(rd.tracks):
        tag = f"track {i} ({m.name})" if m.name else f"track {i}"
        if m.data_offset % TRACK_ALIGN:
            problems.append(f"{tag}: unaligned data offset")
        if m.data_offset + m.data_length > h.data_size:
            problems.append(f"{tag}: data beyond section")
            continue
        if m.channels not in (1, 2) or m.codec not in (CODEC_ADX, CODEC_PCM):
            problems.append(f"{tag}: bad channels/codec")
            continue
        unit = 18 * m.channels if m.codec == CODEC_ADX else 2 * m.channels
        if m.data_length % unit:
            problems.append(f"{tag}: length not {unit}-aligned")
        if m.loops:
            if m.loop_end_byte > m.data_length or \
                    m.loop_start_byte >= m.loop_end_byte:
                problems.append(f"{tag}: bad loop bytes")
            if m.loop_start_byte % unit or m.loop_end_byte % unit:
                problems.append(f"{tag}: loop bytes not frame-aligned")
            if m.xfade_enable:
                n = h.xfade_samples
                tail = (n // 32 * 18 if m.codec == CODEC_ADX else n * 2) \
                    * m.channels
                if not n:
                    problems.append(f"{tag}: xfade_enable but header "
                                    f"xfade_samples == 0")
                elif m.loop_count:
                    # finite loop_count stores the WHOLE stream: after the
                    # last wrap the player plays THROUGH loop_end to the end,
                    # so the tail is the source's own continuation -- any
                    # length >= one blend window is valid
                    if m.data_length < m.loop_end_byte + tail:
                        problems.append(f"{tag}: finite-loop tail "
                                        f"{m.data_length - m.loop_end_byte} "
                                        f"< blend window {tail}")
                elif m.data_length != m.loop_end_byte + tail:
                    problems.append(f"{tag}: xfade tail bytes "
                                    f"{m.data_length - m.loop_end_byte} != "
                                    f"expected {tail}")
                elif m.loop_end_sample - m.loop_start_sample <= n:
                    problems.append(f"{tag}: loop shorter than crossfade")
            start_s, end_s = _loop_points_samples(m)
            if m.loop_start_sample != start_s:
                problems.append(f"{tag}: loop_start_sample "
                                f"{m.loop_start_sample} != byte-derived "
                                f"{start_s}")
            if abs(m.loop_end_sample - end_s) >= 32:
                problems.append(f"{tag}: loop_end_sample {m.loop_end_sample}"
                                f" vs byte-derived {end_s} (>=32 apart)")
        if m.codec == CODEC_ADX:
            c1, c2 = adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF,
                                          m.sample_rate)
            if (m.coef1, m.coef2) != (c1, c2):
                warns.append(f"{tag}: coefs {m.coef1}/{m.coef2} differ from "
                             f"500 Hz-cutoff recompute {c1}/{c2} "
                             f"(non-500 Hz source cutoff?)")
            head = rd.read_track(i)[:2]
            if head and head[0] & 0x80:
                problems.append(f"{tag}: stream starts with EOF/dummy frame")

    seam_report = []
    if not quick:
        for i, m in enumerate(rd.tracks):
            if not m.loops:
                continue
            pcm = _decode_track(rd, i)
            ratio, joint = _seam_metric(pcm, m)
            seam_report.append((i, m.name, ratio, joint))
            # crossfade tracks smooth the raw hard-cut joint at playback, so a
            # large raw joint is expected — the crossfade is what handles it.
            if not m.xfade_enable and ratio > 8.0 and joint > 2500:
                warns.append(f"track {i} ({m.name}): rough loop seam "
                             f"(ratio {ratio:.1f}, step {joint}) — listen!")

    n_xfade = sum(1 for m in rd.tracks if m.xfade_enable)
    print(f"== verify {pack_path}")
    print(f"   game={h.game_id!r} title={h.title!r} tracks={h.track_count} "
          f"triggers: {n_play} play + {n_stop} stop "
          f"(table {h.trigger_rows} rows)")
    print(f"   format v{h.format_version}; crossfade: xfade_samples="
          f"{h.xfade_samples} ({n_xfade} track(s) enabled)")
    print(f"   fade law {h.proto.fade_law} consts 0x{h.proto.fade_const1:x}/"
          f"{h.proto.fade_const2}; {len(h.proto.control_verbs)} control "
          f"verbs, default {VERB_NAMES[h.proto.control_default_verb]}")
    pct = 100.0 * actual_size / DDR_BUDGET_BYTES
    print(f"   size {actual_size / 1e6:.1f} MB = {pct:.0f}% of the "
          f"~{DDR_BUDGET_BYTES // (1 << 20)} MB DDR budget"
          + ("  ** OVER BUDGET **" if actual_size > DDR_BUDGET_BYTES else ""))
    print(f"   BRAM at boot: trigger {h.trigger_rows * 4 // 1024} KB + "
          f"index {h.track_count * 32} B")
    if seam_report:
        worst = sorted(seam_report, key=lambda r: -r[2])[:5]
        print(f"   loop seams: {len(seam_report)} checked; worst ratios: "
              + ", ".join(f"{i}:{n} {r:.1f}" for i, n, r, _ in worst))
    for wmsg in warns:
        print(f"   [warn] {wmsg}")
    for p in problems:
        print(f"   [FAIL] {p}")
    print(f"   {'FAIL' if problems else 'OK'}")
    rd.close()
    return not problems
