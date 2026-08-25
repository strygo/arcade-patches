"""`build_mtwins_arrange.py` — Mega Twins / Chiki Chiki Boys (CPS1 `mtwins`) ARRANGE pack.

Re-keys the PC Engine CD-ROM2 (Chiki Chiki Boys, Japan, 1994) CD-DA arrangement
onto the CPS1 `mtwins` Z80 byte-latch stage commands, for jtcps1_cpsplus.

This builder is deliberately a thin specialisation of build_ffight_arrange.py — same
PackWriter, same CPS1 byte-latch protocol, same whole-track-repeat loop
treatment, same ADX encode path — because Mega Twins uses the identical CPS1
sound dialect (latch 0x800180, single command byte, no handshake) and the PCE
port, like the Sega CD in Final Fight, drives CD-DA with hardware WHOLE-TRACK
infinite repeat (CD_PLAY mode 1; see the internal research notes).
So no inner loop points and the crossfade bit stays OFF for every track.

TRIGGER MAP.  manifests/mtwins_arrange_trigger_map.tsv — 22 play rows, all
user ear-confirmed, every one a stage/ending theme mapped to a distinct CD
track.  Its header documents the code analysis behind the counts.  Two groups
of commands are deliberately NOT mapped and FAIL OPEN to arcade FM:

  * FANFARES cmd13/16/1c/5e.  A purpose-match to tr03 does not survive
    measurement: cmd16/1c/5e are one-shot fanfares (play once ~8-15 s, then
    silence — measured from deterministic arcade renders), cmd13 is continuous
    looping BGM (181/191 s active), and none matches tr03 melodically.  A single 69 s track cannot be both one-shot and
    loop, and matches none of them, so all four fall back to arcade FM.  tr03 is
    left PCE-only / unused.

  * SECRET cues 0x1b (Hidden Secret Door) and 0x1e (Typhoon Kid).  The PCE port
    omits them entirely.

All six fail open the same way: an unmapped (or verb=none/suppress=0) command
leaves suppress=0, the tap holds `sub` low, and the real byte reaches the Z80 ->
authentic arcade FM.  Verified in rtl/cpsplus_cps1_tap.v (sub = ... && cls_sup,
cls_sup = row_q[24]) and format.py (unmapped rows are TriggerRow(), suppress=0).

ADX ONLY.  Every mtwins CD track fits the 256 MiB DDR cap comfortably as ADX
(unlike ffight, whose PCM variant was built over-cap on purpose), so this
builder ships one ADX pack and does not build a PCM variant.

Run:
  python3 -m cpsplus.pack.build_mtwins_arrange
"""
from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

from . import adxcodec, protocols
from .audition import verify as audition_verify
from .build_common import MANIFESTS, PACKS_DIR, PKG_ROOT
from .adxencode import encode_adx_track, read_wav
from .build_common import MapRow, VERBS, _rows
from .protocols import cps1_protocol
from .format import (PackWriter, PackReader, TrackMeta, TriggerRow, CODEC_ADX,
                     VERB_NONE, VERB_PLAY, VERB_NAMES)

CD_DIR = PKG_ROOT / "work" / "intermediate" / "mtwins" / "cd_full"
TRIGGER_TSV = MANIFESTS / "mtwins_arrange_trigger_map.tsv"

DDR_HARD_CAP_BYTES = 256 * 1024 * 1024          # 268,435,456
# Assembled length of base/mtwins.mra <rom index="0"> — sum of all part files
# (che_30/31/35/36 + ck-32m + ch_09/18/19 + ck-1m/3m/5m/7m), measured, already
# 1 kB-aligned so the MRA pad is 0.  This is only the budget-report addend.
MTWINS_ROM_BYTES = 0x350000

# Loop treatment.  The PCE plays every CD cue with CD_PLAY mode 1 (infinite
# whole-track repeat), and every mapped track is a stage/ending theme, so ALL
# tracks are whole-track loops.  (tr03 was the only one-shot candidate, as the
# fanfare target, but the fanfares now fail open to arcade and tr03 is unused.)
ONE_SHOT: set[str] = set()


def load_triggers(path: Path = TRIGGER_TSV) -> list[MapRow]:
    """Parse the mtwins map.  Same 5-column shape as ffight (cmd, verb,
    suppress, track, cue).  Row shapes here:

      verb=play                the 26 arranged rows -> a CD track plays.
      verb=none, suppress=0    an explicit FAIL-OPEN marker: it documents in the
                               map that the cue was considered and left to the
                               arcade original.  Produces bytes identical to an
                               unmapped command (TriggerRow() default), so it is
                               NOT written to the table -- skipped, so the real
                               byte reaches the Z80.  The two secret cues 0x1b
                               and 0x1e are exactly these.

    A verb=none row with suppress=1 (silence-only, as ffight uses for its
    opening stitch) would be written; mtwins has none, but the shape is kept."""
    out: list[MapRow] = []
    for r in _rows(path):
        if len(r) < 5:
            raise ValueError(f"short row in {path.name}: {r!r}")
        cmd_s, verb_s, sup_s, track_s, cue = r[0], r[1], r[2], r[3], r[4]
        if cmd_s == "cmd" and verb_s == "verb":
            continue                          # column-header row

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
    seen: dict[int, MapRow] = {}
    for row in out:
        if row.cmd in seen:
            raise ValueError(f"duplicate command 0x{row.cmd:02x} in {path.name}")
        seen[row.cmd] = row
    # The two secret cues may appear ONLY as fail-open (verb=none, suppress=0);
    # a play/suppress row for them would wrongly mute the arcade FM, which is the
    # only form these cues exist in.
    for secret in (0x1b, 0x1e):
        if secret in seen and not (seen[secret].verb == VERB_NONE
                                   and seen[secret].suppress == 0):
            raise ValueError(
                f"cmd 0x{secret:02x} is a SECRET arcade cue that must fail open "
                f"(verb=none, suppress=0); got a suppressing row in {path.name}")
    return out


def build(out: str | None = None, triggers_path: str | None = None,
          cd_dir: str | None = None, measure_snr: bool = True,
          disc: str | None = None) -> dict:
    rows = load_triggers(Path(triggers_path) if triggers_path else TRIGGER_TSV)
    cd = Path(cd_dir) if cd_dir else CD_DIR
    # PCE rips keep each track's own pregap (whole bin verbatim) -- the
    # convention the shipped, ear-approved pack was built from, verified
    # byte-identical against scripted extraction
    from .discsrc import ensure_audio_cache
    ensure_audio_cache(cd, {r.track for r in rows if r.track},
                       disc, "--disc", pregap="keep")
    play_rows = [r for r in rows if r.verb == VERB_PLAY]
    # verb=none rows are all fail-open (suppress=0) in this map, so they are
    # simply not written.  A silence-only row (suppress=1) would need real
    # handling like ffight's opening stitch; assert there are none so one added
    # later cannot silently degrade to fail-open.
    silence_rows = [r for r in rows if r.verb == VERB_NONE and r.suppress == 1]
    if silence_rows:
        raise ValueError(
            f"build_mtwins_arrange has no silence-only handling but the map has "
            f"{len(silence_rows)} suppress=1 none row(s): "
            f"{[hex(r.cmd) for r in silence_rows]}")

    proto = cps1_protocol()
    proto = dataclasses.replace(proto, game_id="mtwins")
    w = PackWriter(proto, title="Mega Twins (Arrange)",
                   trigger_rows=protocols.SF2_TRIGGER_ROWS, default_rate=44100)

    ti_of: dict[str, int] = {}
    snrs: list[tuple[str, float]] = []
    total_audio = 0
    print("[mtwins] === ADX arrange pack ===")
    for row in play_rows:
        track = row.track
        if track not in ti_of:
            src = cd / f"{track}.wav"
            pcm, rate, ch, n = read_wav(src)
            loops = track not in ONE_SHOT
            data, coef1, coef2, snr = encode_adx_track(
                pcm, rate, ch, n, track, measure_snr)
            if snr is not None:
                snrs.append((track, snr))
            if adxcodec.samples_to_stream_byte(0, ch) != 0:
                raise ValueError("loop start not frame-aligned")
            loop_end_byte = len(data) if loops else 0
            loop_end_sample = n if loops else 0
            if loops and loop_end_byte % (adxcodec.FRAME_BYTES * ch):
                raise ValueError(f"{track}: loop end not frame-aligned")
            meta = TrackMeta(
                sample_rate=rate, channels=ch, codec=CODEC_ADX, gain=0x7f,
                loop_start_sample=0, loop_start_byte=0,
                loop_end_sample=loop_end_sample, loop_end_byte=loop_end_byte,
                coef1=coef1, coef2=coef2, xfade_enable=0,
                name=f"{track} ({'whole-track loop' if loops else 'one_shot'})",
                source=f"mtwins/cd_full/{track}.wav[0:{n}]")
            ti_of[track] = w.add_track(data, meta)
            total_audio += len(data)
            print(f"[mtwins] 0x{row.cmd:02x} -> {track} track{ti_of[track]:<3} "
                  f"{n / rate:7.2f}s  {'loop ' if loops else 'once '}  "
                  f"{len(data) / 1e6:7.2f} MB  {row.cue}")
        else:
            print(f"[mtwins] 0x{row.cmd:02x} -> {track} track{ti_of[track]:<3} "
                  f"{'(shared)':>22}  {row.cue}")
        w.set_trigger(row.cmd, TriggerRow(verb=VERB_PLAY, track=ti_of[track],
                                          gain=0x7f, suppress=row.suppress))

    name = "mtwins_arrange.cpk"
    out_path = Path(out) if out else PACKS_DIR / name
    w.write(out_path)
    size = out_path.stat().st_size

    # readback: every play row survived, the secret cues are genuinely unmapped
    # (fail open), and the header is the CPS1 dialect.
    rd = PackReader(out_path)
    try:
        for row in play_rows:
            got = rd.triggers[row.cmd]
            if got.verb != VERB_PLAY or got.suppress != row.suppress:
                raise ValueError(f"readback: cmd 0x{row.cmd:02x} verb/suppress "
                                 f"mismatch")
            if got.track != ti_of[row.track]:
                raise ValueError(f"readback: cmd 0x{row.cmd:02x} wrong track")
        # every verb=none row must read back as a fail-open passthrough (the
        # 4 fanfares + the 2 secret cues): unwritten default, so arcade FM plays
        failopen = [r.cmd for r in rows if r.verb == VERB_NONE]
        for cmd in failopen:
            g = rd.triggers[cmd]
            if g.verb != VERB_NONE or g.suppress != 0:
                raise ValueError(
                    f"readback: fail-open cmd 0x{cmd:02x} is not passthrough "
                    f"(verb {VERB_NAMES[g.verb]}, suppress {g.suppress})")
        if rd.header.proto.latch_page != 0x800180:
            raise ValueError("readback: latch page is not the CPS1 dialect")
        if rd.header.trigger_rows != protocols.SF2_TRIGGER_ROWS:
            raise ValueError("readback: trigger table is not 256 rows")
    finally:
        rd.close()

    image = size + MTWINS_ROM_BYTES
    print(f"[mtwins] {out_path}")
    print(f"[mtwins]   pack        {size:,} B ({size / 1e6:.1f} MB), "
          f"{len(w.tracks)} tracks, {len(play_rows)} play triggers")
    print(f"[mtwins]   + ROM       {MTWINS_ROM_BYTES:,} B "
          f"= image {image:,} B ({image / 1e6:.1f} MB)")
    over = image - DDR_HARD_CAP_BYTES
    verdict = (f"OVER by {over:,} B" if over > 0
               else f"fits, {-over:,} B ({-over / 1e6:.1f} MB) headroom")
    print(f"[mtwins]   256 MiB cap -> "
          f"{100.0 * image / DDR_HARD_CAP_BYTES:.1f}% — {verdict}")
    print("[mtwins]   secret cues 0x1b/0x1e: unmapped -> fail open to arcade FM")
    if snrs:
        mean = sum(s for _, s in snrs) / len(snrs)
        worst = min(snrs, key=lambda s: s[1])
        print(f"[mtwins]   ADX SNR: mean {mean:.2f} dB, worst "
              f"{worst[0]} {worst[1]:.2f} dB")
    return {"path": out_path, "size": size, "image": image,
            "tracks": len(w.tracks), "snrs": snrs, "audio": total_audio}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cpsplus.pack.build_mtwins_arrange")
    ap.add_argument("--out")
    ap.add_argument("--triggers")
    ap.add_argument("--cd-dir")
    ap.add_argument("--disc", help="Chiki Chiki Boys (Japan) PCE CD rip: a "
                    ".cue, or a .zip/.7z containing one (needed when the "
                    "extraction cache is empty)")
    ap.add_argument("--no-snr", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args(argv)
    res = build(out=a.out, triggers_path=a.triggers, cd_dir=a.cd_dir,
                disc=a.disc,
                measure_snr=not a.no_snr)
    if not a.no_verify:
        audition_verify(str(res["path"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
