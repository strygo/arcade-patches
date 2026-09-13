"""`build_mbomber_arrange.py` — Muscle Bomber / Saturday Night Slam Masters ARRANGE pack.

Builds `mbomber_arrange.cpk`: the FM Towns "Muscle Bomber: The Body Explosion"
(1994, Capcom) CD-DA arrangement re-keyed onto the CPS1.5 `slammast` QSound
stage commands, for the jtcps15_cpsplus core.

IDENTITY (why this source is eligible).  The FM Towns audio is a genuine
distinct arrangement, NOT a render of the arcade QSound master — so it does not
hit the render exclusion that dropped SFA3, Pocket Fighter, Darkstalkers and
Warriors of Fate.  Drift-immune windowed waveform test (250 ms windows aligned
independently over +-3 s, so no clock offset can suppress a real match):
same-master control r_med 0.90/0.67, different-theme control 0.22/0.19,
arcade-vs-FM-Towns probes 0.25/0.23/0.22/0.18 — at the negative floor.
Ear-confirmed by the user on all five A/B pairs.  See
internal campaign notes.

TRIGGER JOIN.  The arcade command -> CD track law is `0x0021 + N <-> tr15 + N`,
nine arena themes to nine long CD tracks.  Five points were ear-confirmed
(0x0021-0x0024, 0x0027); the remaining four follow by exhaustion.  The builder
reads the tracked map (manifests/mbomber_arrange_trigger_map.tsv) and accepts
ONLY rows whose confidence is `confirmed` or `by_exhaustion`.  That filter is
the point, not an accident: it drops the two duration+disc-order rows
(0x0039->tr27, 0x0058->tr24), which are the exact evidence class the user
refuted on Final Fight.  Those cues stay unmapped, so the arcade QSound simply
plays them — correct fail-open behaviour.

AUDIO.  CODEC_ADX, sliced out of the extracted CD-DA WAVs.  This pack is the
one deliberate exception to the byte-exact-PCM rule the rest of the library
follows (spf2t is the only remaining PCM pack): covering all 29 cues instead
of 10 needed roughly 3.5x the audio, and ADX buys that back at a measured
~33 dB SNR cost the user auditioned and accepted.  The result is 79.6 MB with
29 tracks, against 96 MB for the old 10-track PCM build.

SLICING.  Only ONE loop period is stored per track, not the whole track: the
body [loop_start, loop_end), plus XFADE_SAMPLES of natural continuation past
loop_end for the crossfade tracks (the tail the FPGA player blends against the
loop head — pack/xfade.py, rtl/cpsplus_player.v).  The stored stream is a single
contiguous slice `wav[loop_start : loop_end (+ tail)]`, and the track loops that
body whole (loop_start_byte = 0).  This is audibly identical to storing
[0, loop_end) — the seam is (end of pass 2) -> (start of pass 2) either way —
but it roughly halves the pack.

Loop points and per-track hard-cut/crossfade modes come from the tracked
manifests/mbomber_arrange_loops.tsv.

Run it through the uniform entry point:
  build_pack.py mbomber
"""
from __future__ import annotations

import argparse
import csv
import sys
import wave
from pathlib import Path

from . import protocols
from .build_common import MANIFESTS, PACKS_DIR, PKG_ROOT
from . import adxcodec
from .format import (PackWriter, TrackMeta, TriggerRow, CODEC_ADX, VERB_PLAY,

                     DDR_BUDGET_BYTES)

# Measured loudness match (EBU R128 vs the native chip music,
# tools/loudness_match.py).  Shipped packs carried this in their trigger rows
# via a post-build set_gain.py patch, which a from-source rebuild silently
# discarded; baked here so rebuilds reproduce the shipped packs byte-exactly.
TRIG_GAIN = 0x2c


XFADE_SAMPLES = 7200                 # ~163 ms at 44.1 kHz; format v1 global
CD_DIR = PKG_ROOT / "work" / "intermediate" / "mbomber" / "cd_full"
LOOPS_TSV = MANIFESTS / "mbomber_arrange_loops.tsv"
INPUT_PINS = MANIFESTS / "mbomber_arrange_inputs.json"
PINS_SOURCE = {
    "release": "Muscle Bomber (Japan), FM Towns, Alcohol 120% .mds/.mdf image",
    "rip": "each audio track extracted from its start offset to the next "
           "track's, less the 150 trailing pregap sectors; crc32 is the CRC32 "
           "of that PCM",
}
TRIGGER_TSV = MANIFESTS / "mbomber_arrange_trigger_map.tsv"

# Only these confidence tiers are built.  Anything weaker stays unmapped and
# falls through to the arcade QSound.
BUILDABLE = {"confirmed", "by_exhaustion"}
ARENA_CMDS = range(0x0021, 0x002A)   # the nine arena themes


def _rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.reader(f, delimiter="\t")
                if r and not r[0].startswith("#")]


def load_loops(path: Path = LOOPS_TSV) -> dict[str, dict]:
    """track -> {cmd, loop_start, loop_end, mode} from the tracked manifest."""
    out = {}
    for r in _rows(path):
        if r[0] == "track":          # header row
            continue
        track, cmd, ls, le, _len_s, mode = r[0], r[1], r[2], r[3], r[4], r[5]
        if mode not in ("hard_cut", "crossfade", "one_shot"):
            raise ValueError(f"{track}: unknown mode {mode!r}")
        out[track] = dict(cmd=int(cmd, 16), loop_start=int(ls),
                          loop_end=int(le), mode=mode)
    return out


def load_triggers(path: Path = TRIGGER_TSV) -> dict[int, str]:
    """arcade cmd -> cd track, for buildable rows only."""
    out, skipped = {}, []
    for r in _rows(path):
        cmd_s, track, _label, conf = r[0], r[1], r[2], r[3]
        cmd = int(cmd_s, 16)
        if conf not in BUILDABLE:
            skipped.append((cmd, track, conf))
            continue
        if track.upper() == "UNKNOWN":
            continue
        out[cmd] = track
    for cmd, track, conf in skipped:
        print(f"[mbomber] cmd 0x{cmd:04x} -> {track}: confidence {conf!r} "
              f"below the build threshold — left unmapped (arcade QSound "
              f"plays this cue)")
    return out


def read_wav_slice(path: Path, first: int, count: int | None
                   ) -> tuple[bytes, int, int]:
    """Byte-exact PCM frames [first, first+count) from a 16-bit WAV.

    count=None reads to the end of the file (one-shot tracks)."""
    with wave.open(str(path), "rb") as w:
        rate, ch, sw, total = (w.getframerate(), w.getnchannels(),
                               w.getsampwidth(), w.getnframes())
        if sw != 2:
            raise ValueError(f"{path.name}: expected 16-bit, got {sw*8}")
        if count is None:
            count = total - first
        if first + count > total:
            raise ValueError(
                f"{path.name}: slice [{first}, {first+count}) runs past the "
                f"end of the track ({total} frames)")
        w.setpos(first)
        data = w.readframes(count)
    if len(data) != count * ch * 2:
        raise ValueError(f"{path.name}: short read")
    return data, rate, ch


def build(out: str | None = None, loops_path: str | None = None,
          triggers_path: str | None = None, cd_dir: str | None = None,
          disc: str | None = None, check_inputs: bool = False,
          allow_input_mismatch: bool = False, write_pins: bool = False) -> Path:
    loops = load_loops(Path(loops_path) if loops_path else LOOPS_TSV)
    trig = load_triggers(Path(triggers_path) if triggers_path else TRIGGER_TSV)
    cd = Path(cd_dir) if cd_dir else CD_DIR
    # FM Towns rip is .mds/.mdf; the extractor's mds path reproduces the
    # INVENTORY-documented cut (length-150 sectors) byte-exactly (29/29,
    # verified)
    from . import inputpins
    from .discsrc import check_audio_cache, ensure_audio_cache
    needed = set(trig.values()) | set(loops)
    ensure_audio_cache(cd, needed, disc, "--disc")
    out_path = Path(out) if out else PACKS_DIR / "mbomber_arrange.cpk"
    pins = None if write_pins else inputpins.load_pins(INPUT_PINS)
    checks = {}
    if pins:
        checks = check_audio_cache(cd, set(trig.values()), INPUT_PINS,
                                   "Muscle Bomber (FM Towns)", "[mbomber]")
        inputpins.gate("mbomber_arrange", checks, pins, out_path,
                       allow_input_mismatch, check_inputs, "[mbomber]")
    elif check_inputs:
        print(f"[mbomber] {INPUT_PINS.name} not found: nothing to check against")
        raise SystemExit(0)
    audit = inputpins.Audit("mbomber_arrange", pins, checks)
    pin_tracks = ({t: inputpins.fingerprint(read_wav_slice(cd / f"{t}.wav", 0, None)[0])
                   for t in sorted(set(trig.values()))} if write_pins else {})

    # --- cross-check the two tracked manifests against each other ----------
    missing = [c for c in ARENA_CMDS if c not in trig]
    if missing:
        raise ValueError(
            f"the arena block is incomplete: {[hex(c) for c in missing]} "
            f"missing from the buildable trigger rows")
    for cmd, track in trig.items():
        if track not in loops:
            raise ValueError(f"cmd 0x{cmd:04x} -> {track}: no loop row")
        if loops[track]["cmd"] != cmd:
            raise ValueError(
                f"{track}: trigger map says cmd 0x{cmd:04x}, loops manifest "
                f"says 0x{loops[track]['cmd']:04x}")

    proto = protocols.get_protocol("slammast")
    if proto.latch_page != 0xF18000:
        raise ValueError(f"expected CPS1.5 latch page, got {proto.latch_page:#x}")
    w = PackWriter(proto, title="Saturday Night Slam Masters (Arrange)",
                   default_rate=44100, xfade_samples=XFADE_SAMPLES)

    total_audio = 0
    for cmd in sorted(trig):
        track = trig[cmd]
        L = loops[track]
        one_shot = L["mode"] == "one_shot"
        if one_shot:
            # Whole track, unlooped: loop_end_byte = 0 and no crossfade bit.
            # The cue ends in its own decay to silence and never repeats.
            pcm, rate, ch = read_wav_slice(cd / f"{track}.wav", 0, None)
            body = len(pcm) // (2 * ch)
            data = adxcodec.encode(pcm, ch, rate)
            c1, c2 = adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF, rate)
            meta = TrackMeta(
                sample_rate=rate, channels=ch, codec=CODEC_ADX, gain=0x7f,
                coef1=c1, coef2=c2,
                loop_start_sample=0, loop_start_byte=0,
                loop_end_sample=0, loop_end_byte=0, xfade_enable=0,
                name=f"{track} (one_shot)",
                source=f"fmtowns_mbomber/{track}.wav[0:{body}]")
        else:
            body = L["loop_end"] - L["loop_start"]
            if body <= 0:
                raise ValueError(f"{track}: non-positive loop body {body}")
            xf = L["mode"] == "crossfade"
            want = body + (XFADE_SAMPLES if xf else 0)
            pcm, rate, ch = read_wav_slice(cd / f"{track}.wav",
                                           L["loop_start"], want)
            if body % 32:
                raise ValueError(f"{track}: loop body {body} is not a whole "
                                 f"number of 32-sample ADX frames")
            data = adxcodec.encode(pcm, ch, rate)
            c1, c2 = adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF, rate)
            meta = TrackMeta(
                sample_rate=rate, channels=ch, codec=CODEC_ADX, gain=0x7f,
                coef1=c1, coef2=c2,
                loop_start_sample=0, loop_start_byte=0,
                loop_end_sample=body,
                loop_end_byte=adxcodec.samples_to_stream_byte(body, ch),
                xfade_enable=1 if xf else 0,
                name=f"{track} ({L['mode']})",
                source=f"fmtowns_mbomber/{track}.wav[{L['loop_start']}:"
                       f"{L['loop_start']+want}]")
        audit.track(track, [track], pcm, data,
                    (0, meta.loop_end_sample, meta.xfade_enable))
        ti = w.add_track(data, meta)
        w.set_trigger(cmd, TriggerRow(verb=VERB_PLAY, track=ti, gain=TRIG_GAIN,
                                      suppress=1))
        total_audio += len(data)
        print(f"[mbomber] 0x{cmd:04x} -> {track} track{ti}  "
              f"{body/rate:7.3f}s {'whole' if one_shot else 'body '}  "
              f"{L['mode']:9s}  {len(data)/1e6:6.2f} MB")

    w.write(out_path)
    size = out_path.stat().st_size
    rec = audit.write(inputpins.audit_path(out_path), out_path)
    if pins:
        print(f"[mbomber] {out_path.name}: {rec['diagnosis']}")
    if write_pins:
        inputpins.write_pins(INPUT_PINS, PINS_SOURCE, pin_tracks,
                             {"mbomber_arrange": audit.pins_entry(out_path)})
    print(f"[mbomber] {out_path}  {size/1e6:.1f} MB, {len(w.tracks)} tracks, "
          f"{len(trig)} play triggers, audio {total_audio/1e6:.1f} MB")
    if size > DDR_BUDGET_BYTES:
        print(f"[mbomber] WARNING: pack exceeds the {DDR_BUDGET_BYTES/1e6:.0f} "
              f"MB DDR budget")
    return out_path


def main(argv=None):
    REPO = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(REPO / "cpsplus"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--loops")
    ap.add_argument("--triggers")
    ap.add_argument("--cd-dir")
    ap.add_argument("--disc", help="FM Towns Muscle Bomber rip: .mds (with "
                    ".mdf beside it), or a .zip/.7z containing one (needed "
                    "when the extraction cache is empty)")
    ap.add_argument("--check-inputs", action="store_true",
                    help="only check the disc's tracks against the pinned "
                         "inputs (one row per track) and exit: 0 all match, 3 not")
    ap.add_argument("--allow-input-mismatch", action="store_true",
                    help="build even when tracks differ from the verified disc")
    ap.add_argument("--write-pins", action="store_true",
                    help="maintainer: build from the verified disc and write "
                         + INPUT_PINS.name)
    a = ap.parse_args(argv)
    build(out=a.out, loops_path=a.loops, triggers_path=a.triggers,
          cd_dir=a.cd_dir, disc=a.disc, check_inputs=a.check_inputs,
          allow_input_mismatch=a.allow_input_mismatch, write_pins=a.write_pins)


if __name__ == "__main__":
    main()
