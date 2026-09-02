"""`build_ffightae_cps2_arrange.py` — Final Fight 30th Anniversary CPS2 Edition ARRANGE pack.

Builds the Final Fight CD (Sega CD, USA) CD-DA arrangement re-keyed onto the
CPS2 QSound commands of grego2d's Final Fight 30th Anniversary CPS2 Edition
(MiSTer set `ffightae_cps2`, jtcps2 core; MAME oracle: hbmame `ffightaec2`),
for the jtcps2_cpsplus core.

This is a RE-KEY of the CPS1 `ffight_arrange` pack, not a new arrangement:
same 19 US-disc CD tracks, same ear-adjudicated stage loops (AUTHORED_LOOPS is
IMPORTED from build_ffight_arrange so the two packs cannot drift), same ending
treatment.  What changes:

  * TRIGGER KEYING — the hack pages its CPS1-derived cue space at 0x01xx
    (low-byte-identical to CPS1 ffight; measured, see
    manifests/ffightae_cps2_arrange_trigger_map.tsv).  16-bit commands, the
    standard 0x1200-row CPS2 trigger table, CPS2 handshake-gating suppression.
  * PROTOCOL — protocols.PROTOCOLS["ffightae_cps2"]: the SFA3 (sz3) 1.71 driver's
    record layout (sfa3ud shape, +0x05 arg byte present) with controls
    measured on this driver (0xff00 stop; ff06/ff07 alpha2-style FADE_KEEP;
    ff05 deliberately unmapped).
  * OPENING — entry on the SONG cue 0x0152 with the JP authored one-bar wrap
    VERBATIM (OPENING_LOOP below; US-disc sample coordinates, no loop_shift).
    The hack's ring fires only on the FIRST attract cycle after boot, so a
    US-style ring entry would leave every later cycle silent; entering on the
    song covers all cycles, and the measured timeline (song 23.0 s, title
    73.1-73.5 s) lands the master's outro on the title exactly as the JP CPS1
    pack authored (end 73.4 s).  Evidence and frame numbers: the trigger
    map's header.
  * NO voiced cutscene cues, NO regions — one World set, one pack.

DELIBERATELY ITS OWN PACK.  A shared CPS1+CPS2 `ffight_arrange.cpk` was built,
fully gated in emulation and committed (6be153cb), then reverted on review:
it re-opened the hardware validation of the shipped CPS1 pack, routed CPS1
cores through the never-hardware-exercised loader clamp (0x1200-row table on
a 256-row tap), exposed the CPS1 byte rows to the hack's own page-0 SFX bank
(any unobserved 0x00xx at a mapped byte would misfire arranged music), and
leaned on hs_ready being unused by the v0 CPS2 RTL.  A separate pack costs
~81 MB on the SD card and buys all four of those back.  The measurement work
(map, protocol, opening sizing, gain) is identical either way.

TRIGGER GAIN.  Measured EBU R128 match vs this hack's OWN native QSound music
(EBU R128 match against the menu-idle injection renders), baked as
TRIG_GAIN so rebuilds stay
byte-identical.  The CPS1 ffight packs ship unity because their YM2151/OKI
reference measured level-matched; the hack's QSound driver is its own
carrier and gets its own measurement.

Run (uniform entry point):
  build_pack.py ffightae-cps2 [--us-disc <rip>]
INPUT: the Final Fight CD USA rip -- shared extraction cache with the CPS1
packs (work/intermediate/ffightcd/cd_full), so a tree that has built
ffight_arrange builds this with no disc argument.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import adxcodec, protocols
from .adxencode import encode_adx_track, read_wav
from .audition import verify as audition_verify
from .build_common import MANIFESTS, PACKS_DIR, MapRow, VERBS, _rows
from .build_ffight_arrange import (AUTHORED_LOOPS, CD_DIR, LOOPING, ONE_SHOT,
                                   XFADE_SAMPLES, ensure_disc_audio)
from .format import (DEFAULT_TRIGGER_ROWS, PackWriter, PackReader, TrackMeta,
                     TriggerRow, CODEC_ADX, VERB_NONE, VERB_PLAY, VERB_NAMES)

TRIGGER_TSV = MANIFESTS / "ffightae_cps2_arrange_trigger_map.tsv"

# jotego "Total 0x2C42014 bytes" accounting comment in the Arcade_Offset base
# MRA (mra/base_offset/ffightae_cps2.mra).
FFIGHTAE_ROM_BYTES = 0x2C42014
DDR_HARD_CAP_BYTES = 256 * 1024 * 1024

# Opening: tr25 with the JP authored wrap, verbatim (see module docstring and
# the trigger map's OPENING section).  US-disc sample coordinates.
OPENING_TRACK = "tr25"
OPENING_LOOP = (1056928, 1183904, 1, 1)

# Measured loudness match (EBU R128) vs the hack's
# own QSound music (menu-idle injection renders, 18 command pairs): median
# arranged-over-native delta +8.9 LU (IQR 3.2) -> gain 0x2d
# (0x7f * 10^(-8.9/20)).  In family: sfa2/sfz2al measured 0x25, ssf2 0x1d.
TRIG_GAIN = 0x2d


def load_triggers(path: Path = TRIGGER_TSV) -> list[MapRow]:
    """Parse the tracked 16-bit trigger map (same row shapes as the CPS1
    ffight map; commands are CPS2 QSound words inside the 0x1200-row table)."""
    out: list[MapRow] = []
    for r in _rows(path):
        if len(r) < 5:
            raise ValueError(f"short row in {path.name}: {r!r}")
        cmd_s, verb_s, sup_s, track_s, cue = r[0], r[1], r[2], r[3], r[4]
        if verb_s not in VERBS:
            raise ValueError(f"cmd {cmd_s}: unknown verb {verb_s!r}")
        verb = VERBS[verb_s]
        cmd = int(cmd_s, 16)
        if not 0 <= cmd < DEFAULT_TRIGGER_ROWS:
            raise ValueError(f"cmd {cmd_s} outside the CPS2 trigger table")
        track = None if track_s.strip() in ("-", "") else track_s.strip()
        if verb == VERB_PLAY and track is None:
            raise ValueError(f"cmd {cmd_s}: verb=play with no track")
        if verb == VERB_NONE and track is not None:
            raise ValueError(f"cmd {cmd_s}: verb=none must not name a track")
        out.append(MapRow(cmd=cmd, verb=verb, suppress=int(sup_s),
                          track=track, cue=cue))
    seen = set()
    for row in out:
        if row.cmd in seen:
            raise ValueError(f"duplicate command 0x{row.cmd:04x} in {path.name}")
        seen.add(row.cmd)
    return out


def build(out: str | None = None, triggers_path: str | None = None,
          cd_dir: str | None = None, measure_snr: bool = True,
          us_disc: str | None = None) -> dict:
    rows = load_triggers(Path(triggers_path) if triggers_path else TRIGGER_TSV)
    cd = Path(cd_dir) if cd_dir else CD_DIR
    needed = {r.track for r in rows if r.track}
    ensure_disc_audio(cd, needed, us_disc, "--us-disc")

    tracks = [r.track for r in rows if r.track]
    unknown = sorted(set(tracks) - LOOPING - ONE_SHOT)
    if unknown:
        raise ValueError(f"tracks with no loop treatment decided: {unknown}")

    proto = protocols.PROTOCOLS["ffightae_cps2"]
    if proto.latch_page != 0x618000:
        raise ValueError("ffightae_cps2 descriptor is not the CPS2 QSound latch")
    w = PackWriter(proto,
                   title="Final Fight 30th Anniversary CPS2 Edition (Arrange)",
                   trigger_rows=DEFAULT_TRIGGER_ROWS,
                   default_rate=44100, xfade_samples=XFADE_SAMPLES)

    ti_of: dict[str, int] = {}
    snrs: list[tuple[str, float]] = []
    print("[ffightae_cps2] === adx ===")
    for row in rows:
        if row.verb == VERB_NONE:
            if row.suppress == 0:
                # Documented FAIL-OPEN: identical bytes to an unmapped command,
                # so not written -- the native SE must reach the Z80.
                print(f"[ffightae_cps2] 0x{row.cmd:04x} -> (fail open, not written)"
                      f"  {row.cue}")
                continue
            w.set_trigger(row.cmd, TriggerRow(verb=VERB_NONE, track=0,
                                              gain=0x7f, suppress=row.suppress))
            print(f"[ffightae_cps2] 0x{row.cmd:04x} -> (silence, no restart)"
                  f"  {row.cue}")
            continue

        track = row.track
        if track not in ti_of:
            src = cd / f"{track}.wav"
            pcm, rate, ch, n = read_wav(src)
            loops = track in LOOPING
            authored = OPENING_LOOP if track == OPENING_TRACK \
                else AUTHORED_LOOPS.get(track)
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
            if authored:
                als, ale, axf, acnt = authored
                loop_start_sample, loop_start_byte = \
                    als, adxcodec.samples_to_stream_byte(als, ch)
                loop_end_sample, loop_end_byte = \
                    ale, adxcodec.samples_to_stream_byte(ale, ch)
                if axf and acnt == 0:
                    # infinite crossfade stores exactly loop_end + blend tail
                    tail = XFADE_SAMPLES // 32 * 18 * ch
                    data = data[:loop_end_byte + tail]
            else:
                loop_start_sample = loop_start_byte = 0
            kind = (("inner loop" if authored[3] == 0 else "finite loop")
                    if authored else
                    "whole-track loop" if loops else "one_shot")
            meta = TrackMeta(
                sample_rate=rate, channels=ch, codec=CODEC_ADX, gain=0x7f,
                loop_start_sample=loop_start_sample,
                loop_start_byte=loop_start_byte,
                loop_end_sample=loop_end_sample, loop_end_byte=loop_end_byte,
                coef1=coef1, coef2=coef2,
                xfade_enable=(authored[2] if authored else 0),
                loop_count=(authored[3] if authored else 0),
                name=f"{track} ({kind})",
                source=f"ffightcd/cd_full/{track}.wav[0:{n}]")
            ti_of[track] = w.add_track(data, meta)
            print(f"[ffightae_cps2] 0x{row.cmd:04x} -> {track} track{ti_of[track]:<3} "
                  f"{n / rate:7.2f}s  {'loop ' if loops else 'once '}  "
                  f"{len(data) / 1e6:7.2f} MB  {row.cue}")
        else:
            print(f"[ffightae_cps2] 0x{row.cmd:04x} -> {track} track{ti_of[track]:<3} "
                  f"{'(shared)':>22}  {row.cue}")
        w.set_trigger(row.cmd, TriggerRow(verb=VERB_PLAY, track=ti_of[track],
                                          gain=TRIG_GAIN, suppress=row.suppress))

    out_path = Path(out) if out else PACKS_DIR / "ffightae_cps2_arrange.cpk"
    w.write(out_path)
    size = out_path.stat().st_size

    rd = PackReader(out_path)
    try:
        for row in rows:
            if row.verb == VERB_NONE and row.suppress == 0:
                got = rd.triggers[row.cmd]
                if got.verb != VERB_NONE or got.suppress != 0:
                    raise ValueError(f"readback: fail-open 0x{row.cmd:04x} "
                                     f"has bytes written")
                continue
            got = rd.triggers[row.cmd]
            if got.verb != row.verb or got.suppress != row.suppress:
                raise ValueError(
                    f"readback: cmd 0x{row.cmd:04x} is verb "
                    f"{VERB_NAMES[got.verb]}/suppress {got.suppress}, "
                    f"expected {VERB_NAMES[row.verb]}/{row.suppress}")
            if row.verb == VERB_PLAY and got.track != ti_of[row.track]:
                raise ValueError(f"readback: cmd 0x{row.cmd:04x} wrong track")
        # A CPS2-only pack must stay OFF the byte rows: rows 0x00-0xff belong
        # to the CPS1 dialect, and the hack owns page 0 as its own SFX bank
        # (the docstring's page-0 exposure -- structurally excluded here).
        for cmd in range(0x100):
            got = rd.triggers[cmd]
            if got.verb != VERB_NONE or got.suppress != 0:
                raise ValueError(f"readback: byte row 0x{cmd:02x} is not "
                                 f"empty in a CPS2-only pack")
        if rd.header.proto.latch_page != 0x618000:
            raise ValueError("readback: latch page is not the CPS2 dialect")
        if rd.header.trigger_rows != DEFAULT_TRIGGER_ROWS:
            raise ValueError("readback: trigger table is not 0x1200 rows")
    finally:
        rd.close()

    image = size + FFIGHTAE_ROM_BYTES
    n_play = sum(1 for r in rows if r.verb == VERB_PLAY)
    print(f"[ffightae_cps2] {out_path}")
    print(f"[ffightae_cps2]   pack        {size:,} B ({size / 1e6:.1f} MB), "
          f"{len(w.tracks)} tracks, {n_play} play triggers, gain 0x{TRIG_GAIN:02x}")
    print(f"[ffightae_cps2]   + ROM       {FFIGHTAE_ROM_BYTES:,} B "
          f"= image {image:,} B ({image / 1e6:.1f} MB)")
    over = image - DDR_HARD_CAP_BYTES
    verdict = (f"OVER by {over:,} B" if over > 0
               else f"fits, {-over:,} B ({-over / 1e6:.1f} MB) headroom")
    print(f"[ffightae_cps2]   256 MiB cap -> "
          f"{100.0 * image / DDR_HARD_CAP_BYTES:.1f}% — {verdict}")
    if image > DDR_HARD_CAP_BYTES:
        raise SystemExit("[ffightae_cps2] image exceeds the DDR cap")

    if snrs:
        worst = min(snrs, key=lambda s: s[1])
        mean = sum(s for _, s in snrs) / len(snrs)
        print(f"[ffightae_cps2]   ADX SNR: mean {mean:.2f} dB, worst "
              f"{worst[0]} {worst[1]:.2f} dB")

    return {"path": out_path, "size": size, "image": image,
            "tracks": len(w.tracks), "snrs": snrs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m cpsplus.pack.build_ffightae_cps2_arrange")
    ap.add_argument("--out")
    ap.add_argument("--triggers")
    ap.add_argument("--cd-dir")
    ap.add_argument("--us-disc", help="US Final Fight CD rip: a .cue, or a "
                    ".zip/.7z containing one (needed when the extraction "
                    "cache is empty)")
    ap.add_argument("--no-snr", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args(argv)
    res = build(out=a.out, triggers_path=a.triggers, cd_dir=a.cd_dir,
                measure_snr=not a.no_snr, us_disc=a.us_disc)
    if not a.no_verify and not audition_verify(str(res["path"])):
        raise SystemExit(f"verification FAILED for {res['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
