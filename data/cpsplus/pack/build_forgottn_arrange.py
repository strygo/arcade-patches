"""Build `forgottn_arrange.cpk` — Forgotten Worlds with the PC Engine CD-ROM2
CD-DA soundtrack (Japan disc).

Direct sibling of build_mtwins_arrange.py (same CPS1 byte-latch protocol, same
whole-track-repeat loop law from internal research notes).  What is
different here, and why:

  * The trigger map (manifests/forgottn_arrange_trigger_map.tsv) is MACHINE-
    NAMED AT BOTH ENDS: every arcade command NCC-identified against the
    reference arcade gamerip (same carrier), every CD track duration-matched
    to the reference PCE rip (all <=0.5 s error), cross-checked against the
    cue table recovered from the disc driver.  In-game anchors: attract=0x01,
    stage-1 start=0x02.
  * ONE_SHOT is non-empty: opening (tr18), round clear (tr08) and game over
    (tr19) are jingles the PCE plays with CD_PLAY mode 2 (play once).
  * 0x0e (Last Round Boss) is the disc's TWO-PHASE boss music: tr06 is the
    22.7 s Bios intro, tr07 the 180.7 s main theme.  The pack track is the
    concatenation `tr06+tr07`, loop_start at the tr06/tr07 boundary — the
    intro plays once, the main theme repeats.  This is the one authored loop
    point in the pack; everything else is a whole-track loop.

INPUTS ARE PINNED (manifests/forgottn_arrange_inputs.json; see
build_mtwins_arrange and pack/inputpins.py): every build checks the
extracted tracks first, one row per track, and stops if one differs from
the verified disc unless --allow-input-mismatch.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from . import adxcodec, protocols
from .adxencode import encode_adx_track, read_wav
from .build_common import MapRow, VERBS, _rows
from .build_common import MANIFESTS, PACKS_DIR, PKG_ROOT
from .format import (PackWriter, PackReader, TrackMeta, TriggerRow, CODEC_ADX,

                     VERB_PLAY, VERB_NONE)

# Measured loudness match (EBU R128 vs the native chip music,
# tools/loudness_match.py).  Shipped packs carried this in their trigger rows
# via a post-build set_gain.py patch, which a from-source rebuild silently
# discarded; baked here so rebuilds reproduce the shipped packs byte-exactly.
TRIG_GAIN = 0x63

TRIGGER_TSV = MANIFESTS / "forgottn_arrange_trigger_map.tsv"
INPUT_PINS = MANIFESTS / "forgottn_arrange_inputs.json"
PINS_SOURCE = {
    "release": "Forgotten Worlds (Japan), PC Engine Super CD-ROM2",
    "rip": "cue sheet with one .bin per track (redump layout); every audio "
           "track extracted whole, its INDEX 00 pregap included, so crc32 is "
           "that track's .bin CRC32",
}
CD_DIR = PKG_ROOT / "work" / "intermediate" / "forgottn" / "cd_full"

ONE_SHOT = {"tr18", "tr08", "tr19"}


def load_triggers(path: Path = TRIGGER_TSV) -> list[MapRow]:
    out: list[MapRow] = []
    for r in _rows(path):
        if len(r) < 5:
            raise ValueError(f"short row in {path.name}: {r!r}")
        cmd_s, verb_s, sup_s, track_s, cue = r[0], r[1], r[2], r[3], r[4]
        if cmd_s == "cmd" and verb_s == "verb":
            continue
        if verb_s not in VERBS:
            raise ValueError(f"cmd {cmd_s}: unknown verb {verb_s!r}")
        verb = VERBS[verb_s]
        cmd = int(cmd_s, 16)
        if not 0 <= cmd < protocols.SF2_TRIGGER_ROWS:
            raise ValueError(f"cmd {cmd_s} outside the CPS1 byte-command range")
        track = None if track_s.strip() in ("-", "") else track_s.strip()
        if verb == VERB_PLAY and track is None:
            raise ValueError(f"cmd {cmd_s}: verb=play with no track")
        if verb == VERB_NONE and track is not None:
            raise ValueError(f"cmd {cmd_s}: verb=none must not name a track")
        out.append(MapRow(cmd=cmd, verb=verb, suppress=int(sup_s),
                          track=track, cue=cue))
    seen: set[int] = set()
    for row in out:
        if row.cmd in seen:
            raise ValueError(f"duplicate command 0x{row.cmd:02x}")
        seen.add(row.cmd)
    return out


def _load_track_pcm(cd: Path, track: str):
    """-> (pcm, rate, ch, frames, loop_start_sample).  `a+b` concatenates two
    CD tracks; loop_start lands on the boundary so part a plays once."""
    if "+" in track:
        a, b = track.split("+")
        pa, rate, ch, na = read_wav(cd / f"{a}.wav")
        pb, rb, cb, nb = read_wav(cd / f"{b}.wav")
        if (rb, cb) != (rate, ch):
            raise ValueError(f"{track}: mismatched formats")
        return pa + pb, rate, ch, na + nb, na
    pcm, rate, ch, n = read_wav(cd / f"{track}.wav")
    return pcm, rate, ch, n, 0


def build(out: str | None = None, triggers_path: str | None = None,
          cd_dir: str | None = None, measure_snr: bool = True,
          disc: str | None = None, check_inputs: bool = False,
          allow_input_mismatch: bool = False, write_pins: bool = False) -> dict:
    rows = load_triggers(Path(triggers_path) if triggers_path else TRIGGER_TSV)
    cd = Path(cd_dir) if cd_dir else CD_DIR
    # PCE rips keep each track's own pregap (whole bin verbatim); `a+b`
    # concat rows need both parts.  Convention byte-verified.
    from . import inputpins
    from .discsrc import check_audio_cache, ensure_audio_cache
    needed = {part for r in rows if r.track for part in r.track.split("+")}
    ensure_audio_cache(cd, needed, disc, "--disc", pregap="keep")
    out_path = Path(out) if out else PACKS_DIR / "forgottn_arrange.cpk"
    pins = None if write_pins else inputpins.load_pins(INPUT_PINS)
    checks = {}
    if pins:
        checks = check_audio_cache(cd, needed, INPUT_PINS,
                                   "Forgotten Worlds (PC Engine CD)", "[forgottn]")
        inputpins.gate("forgottn_arrange", checks, pins, out_path,
                       allow_input_mismatch, check_inputs, "[forgottn]")
    elif check_inputs:
        print(f"[forgottn] {INPUT_PINS.name} not found: nothing to check against")
        raise SystemExit(0)
    audit = inputpins.Audit("forgottn_arrange", pins, checks)
    pin_tracks = ({t: inputpins.fingerprint(read_wav(cd / f"{t}.wav")[0])
                   for t in sorted(needed)} if write_pins else {})
    play_rows = [r for r in rows if r.verb == VERB_PLAY]
    silence_rows = [r for r in rows if r.verb == VERB_NONE and r.suppress == 1]
    if silence_rows:
        raise ValueError("build_forgottn_arrange has no silence-only handling; got "
                         f"{[hex(r.cmd) for r in silence_rows]}")

    # NOT cps1_protocol(): it demands an empty control-verb map, but sf2's
    # descriptor carries 0xf7=stop and forgottn wants it too (the game brackets every song change with
    # 0xf7: see the natural-play latch log, '0x01 0xf7 0x02' at game start).
    proto = protocols.get_protocol("forgottn")
    if proto.latch_page != 0x800180:
        raise ValueError("sf2 descriptor is not the CPS1 byte latch")
    w = PackWriter(proto, title="Forgotten Worlds (Arrange)",
                   trigger_rows=protocols.SF2_TRIGGER_ROWS, default_rate=44100)

    ti_of: dict[str, int] = {}
    snrs = []
    print("[forgottn] === PCE CD-DA arrange pack ===")
    for row in play_rows:
        track = row.track
        if track not in ti_of:
            pcm, rate, ch, n, loop_start = _load_track_pcm(cd, track)
            loops = track not in ONE_SHOT
            data, coef1, coef2, snr = encode_adx_track(
                pcm, rate, ch, n, track, measure_snr)
            if snr is not None:
                snrs.append((track, snr))
            # ADX frames are 32 samples; a concat boundary is generally not
            # frame-aligned, so round the loop start DOWN to the frame below
            # (<=0.7 ms early re-entry, inaudible against a 22.7 s intro).
            audit.track(track, track.split("+"), pcm, data,
                        (loop_start // 32 * 32 if loops else 0,
                         n if loops else 0, 0))
            fb = adxcodec.FRAME_BYTES * ch
            ls_sample = loop_start // 32 * 32
            ls_byte = adxcodec.samples_to_stream_byte(ls_sample, ch)
            meta = TrackMeta(
                sample_rate=rate, channels=ch, codec=CODEC_ADX, gain=0x7f,
                loop_start_sample=ls_sample if loops else 0,
                loop_start_byte=ls_byte if loops else 0,
                loop_end_sample=n if loops else 0,
                loop_end_byte=len(data) if loops else 0,
                coef1=coef1, coef2=coef2, xfade_enable=0,
                name=f"{track} ({'loop' if loops else 'one_shot'})",
                source=f"forgottn/cd_full/{track}.wav")
            ti_of[track] = w.add_track(data, meta)
            print(f"[forgottn] 0x{row.cmd:02x} -> {track:<10} track{ti_of[track]:<3}"
                  f" {n / rate:7.2f}s  {'loop' if loops else 'once'}  {row.cue}")
        else:
            print(f"[forgottn] 0x{row.cmd:02x} -> {track:<10} track{ti_of[track]:<3}"
                  f" {'(shared)':>10}  {row.cue}")
        w.set_trigger(row.cmd, TriggerRow(verb=VERB_PLAY, track=ti_of[track],
                                          gain=TRIG_GAIN, suppress=row.suppress))

    w.write(out_path)

    rd = PackReader(out_path)
    for row in play_rows:
        got = rd.triggers[row.cmd]
        if got.verb != VERB_PLAY or got.track != ti_of[row.track]:
            raise ValueError(f"readback mismatch at 0x{row.cmd:02x}")
    for row in rows:
        if row.verb == VERB_NONE:
            got = rd.triggers[row.cmd]
            if got.verb != VERB_NONE:
                raise ValueError(f"fail-open row 0x{row.cmd:02x} got written")
    size = out_path.stat().st_size
    rec = audit.write(inputpins.audit_path(out_path), out_path)
    if pins:
        print(f"[forgottn] {out_path.name}: {rec['diagnosis']}")
    if write_pins:
        inputpins.write_pins(INPUT_PINS, PINS_SOURCE, pin_tracks,
                             {"forgottn_arrange": audit.pins_entry(out_path)})
    print(f"[forgottn] {out_path}  {size / 1e6:.1f} MB, "
          f"{len(ti_of)} tracks, {len(play_rows)} play triggers")
    if snrs:
        worst = sorted(snrs, key=lambda kv: kv[1])[:3]
        print("[forgottn] worst encode SNRs: " +
              ", ".join(f"{t} {s:.1f} dB" for t, s in worst))
    return {"path": str(out_path), "size": size, "tracks": len(ti_of)}


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--out"); ap.add_argument("--map"); ap.add_argument("--cd")
    ap.add_argument("--disc", help="Forgotten Worlds (Japan) PCE CD rip: a "
                    ".cue, or a .zip/.7z containing one (needed when the "
                    "extraction cache is empty)")
    ap.add_argument("--no-snr", action="store_true")
    ap.add_argument("--check-inputs", action="store_true",
                    help="only check the disc's tracks against the pinned "
                         "inputs (one row per track) and exit: 0 all match, 3 not")
    ap.add_argument("--allow-input-mismatch", action="store_true",
                    help="build even when tracks differ from the verified disc")
    ap.add_argument("--write-pins", action="store_true",
                    help="maintainer: build from the verified disc and write "
                         + INPUT_PINS.name)
    a = ap.parse_args(argv)
    build(a.out, a.map, a.cd, measure_snr=not a.no_snr, disc=a.disc,
          check_inputs=a.check_inputs,
          allow_input_mismatch=a.allow_input_mismatch, write_pins=a.write_pins)


if __name__ == "__main__":
    main()
