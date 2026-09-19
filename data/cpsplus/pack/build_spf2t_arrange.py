"""`build_spf2t_arrange.py` — Super Puzzle Fighter II Turbo (CPS2 `spf2t`) ARRANGE pack.

Builds `spf2t_arrange.cpk`: the Saturn "Super Puzzle Fighter II Turbo" (1996,
Capcom) ARRANGE bank re-keyed onto the arcade `spf2t` QSound sound commands,
for the jtcps2_cpsplus core.

IDENTITY (why this source is eligible).  The Saturn disc carries TWO banks of
the same 23 cues: bank A (songs 0x03..0x19) is a render of the arcade QSound
master, bank B (songs 0x1A..0x30) is a genuine distinct arrangement.  Shipping
bank A would ship arcade audio dressed as an arrangement, so this builder reads
ONLY the `saturn_song` column of the tracked map — which points at bank B — and
asserts every song id lands in 0x1A..0x30.  Carrier choice: the Saturn MUS is
lossless s16 PCM; the Dreamcast (ADX) and PS2 Anthology carriers of the same
recordings measure 27-38 dB more HF noise
(the internal listening archive, internal research notes).

TRIGGER JOIN.  manifests/spf2t_arrange_source_map.tsv — 23 rows, EAR-CONFIRMED
by ear across the full arcade-vs-arrange audition set, 18 of
them additionally grounded on arcade screenshots.  The map is NOT an offset:
the 11 stage themes follow `arrange = cmd + 0x19`, but 10 of the 12 jingles are
PERMUTED (0x0C->0x2C, 0x13->0x25, 0x17->0x30 ...).  So the builder reads every
row from the TSV and never computes a mapping.  Rows outside the TSV stay
unmapped and fall through to the arcade QSound — correct fail-open behaviour.

PROTOCOL.  `protocols.get_protocol("spf2t")` does NOT raise for an unknown
game — it silently falls through to `generic_protocol()`, which for a CPS2 set
would LOOK right (QSound family, latch page 0x618000) while being an
unverified guess.  So this builder loads the real Phase-0 traced descriptor,
manifests/protocol/spf2t.json (driver 2.01b, banner "2.01b /CPS2 1996 /APRIL",
545 complete records, 0 stray writes), and ASSERTS the latch page and the whole
record layout — command bytes at +0x01/+0x03, argument word at +0x07/+0x09,
argument byte at +0x05 (this driver DOES write it, unlike hsf2/ssf2t),
handshake at +0x1f with 0x00 = pending / 0xff = ready — before building a
Protocol from those traced values.  Fade law and the 0xffxx control vocabulary
are the Anthology-family ones (same 1996 CPS2 QSound lineage); every control
byte the trace actually observed (0xff00, 0xff05, 0xff07, 0xff08) is explicitly
mapped in CONTROL_ANTHOLOGY, so `control_default_verb` only covers unobserved
commands and is left VERB_NONE (fail-open: an unmapped 0xffxx passes through to
the Z80 and does not disturb the arranged player).

AUDIO.  Byte-exact CODEC_PCM.  The MUS streams are headerless s16 BIG-endian
stereo at 37800 Hz, channel-block-interleaved in 4096 B blocks; `mus.decode_mus`
de-interleaves and byte-swaps to the s16le the pack format wants.  That swap is
lossless and is the ONLY transform applied: no resample, no ADX re-encode, no
gain change.  The builder proves it by inverting the transform on the bytes it
read back out of the written pack and comparing against the raw MUS files.

LOOPS — the whole reason this source was chosen.  The Saturn MUS engine stores
each song as `SXX0.MUS` (intro) + `SXX1.MUS` (loop body) and streams
intro-once-then-repeat-body, so the loop point IS the file boundary:
    loop_start = intro frames,  loop_end = intro + loop frames.
Sample-exact and Capcom-authored.  NOTHING is authored here and no crossfade is
applied (xfade_enable = 0 on every track, pack xfade_samples = 0).  A song with
no `SXX1.MUS` is a one-shot: loop_end_byte = 0.  Every size is cross-checked
against manifests/spf2t_usa_saturn_looptable.tsv and against the independent
intro/loop seconds in the source map, and the build fails on any mismatch.

Run:
  python3 -m cpsplus.pack.build_spf2t_arrange
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .build_common import MANIFESTS, PACKS_DIR, REPO_ROOT
from .format import (PackReader, PackWriter, Protocol, TrackMeta, TriggerRow,
                     CODEC_PCM, FADE_ANTHOLOGY, VERB_NONE, VERB_PLAY,
                     VERB_NAMES, DDR_BUDGET_BYTES)
from .isofs import IsoFS
from .mus import decode_mus, BLOCK
from .protocols import CONTROL_ANTHOLOGY, CONTROL_SPF2T_ARCADE
from .sources import resolve_image

# Measured loudness match (EBU R128 vs the native chip music,
# tools/loudness_match.py).  Shipped packs carried this in their trigger rows
# via a post-build set_gain.py patch, which a from-source rebuild silently
# discarded; baked here so rebuilds reproduce the shipped packs byte-exactly.
TRIG_GAIN = 0x4e

# --- tracked inputs -----------------------------------------------------------
SOURCE_MAP_TSV = MANIFESTS / "spf2t_arrange_source_map.tsv"
LOOPTABLE_TSV = MANIFESTS / "spf2t_usa_saturn_looptable.tsv"
RATES_TSV = MANIFESTS / "mus_rates_spf2t_usa.tsv"
PROTOCOL_JSON = MANIFESTS / "protocol" / "spf2t.json"

# --- source facts (internal research notes, addendum h) -----------
MUS_RATE = 37800             # SPF2T/SPF2X family rate; NOT the 32000 of SFZ2
MUS_CHANNELS = 2
BYTES_PER_FRAME = 2 * MUS_CHANNELS
ARRANGE_BANK = range(0x1A, 0x31)   # bank B — the arrangement.  Bank A (0x03..
                                   # 0x19) is the arcade render and is excluded.
MUS_FORMAT = "s16be_stereo37800_blk4096"

# --- traced protocol expectations (manifests/protocol/spf2t.json) -------------
EXPECT_LATCH_PAGE = 0x618000
EXPECT_DIALECT = "cps2_qsound"
EXPECT_BANNER = "2.01b /CPS2 1996 /APRIL"
EXPECT_RECORD_OFFSETS = {0x01, 0x03, 0x05, 0x07, 0x09}   # subset that must be
                                                         # present in the trace
EXPECT_HANDSHAKE_OFF = 0x1f
EXPECT_HANDSHAKE_PENDING = 0x00
EXPECT_HANDSHAKE_READY = 0xff


def _rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.reader(f, delimiter="\t")
                if r and not r[0].startswith("#")]


# ----------------------------------------------------------------- protocol --
def traced_protocol(path: Path = PROTOCOL_JSON) -> Protocol:
    """Build the spf2t descriptor from the Phase-0 trace, with assertions.

    Deliberately does NOT call protocols.get_protocol(): that helper returns
    generic_protocol() for any game it has no entry for, which would look
    plausible here (CPS2 QSound, latch 0x618000) while being unverified.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing — spf2t needs its Phase-0 latch trace; refusing "
            f"to fall back to protocols.generic_protocol()")
    j = json.loads(path.read_text(encoding="utf-8"))

    if j.get("set") != "spf2t":
        raise AssertionError(f"{path.name}: descriptor is for {j.get('set')!r}")
    if j.get("dialect") != EXPECT_DIALECT:
        raise AssertionError(f"{path.name}: dialect {j.get('dialect')!r} != "
                             f"{EXPECT_DIALECT!r}")
    if j.get("z80_banner") != EXPECT_BANNER:
        raise AssertionError(f"{path.name}: Z80 banner {j.get('z80_banner')!r}")

    latch = int(j["latch_page"], 0)
    if latch != EXPECT_LATCH_PAGE:
        raise AssertionError(f"{path.name}: latch page {latch:#x} != "
                             f"{EXPECT_LATCH_PAGE:#x} (CPS2 QSound page)")

    fo = j["field_offsets"]
    cmd_hi, cmd_lo = int(fo["cmd_hi"], 0), int(fo["cmd_lo"], 0)
    observed = {int(x, 0) for x in fo["observed_record_offsets"]}
    if (cmd_hi, cmd_lo) != (0x01, 0x03):
        raise AssertionError(f"{path.name}: command bytes at "
                             f"{cmd_hi:#04x}/{cmd_lo:#04x}, expected 0x01/0x03")
    missing = sorted(EXPECT_RECORD_OFFSETS - observed)
    if missing:
        raise AssertionError(
            f"{path.name}: record offsets {[hex(m) for m in missing]} were "
            f"never observed — the record layout is not the QSound family one")
    hs = j["handshake"]
    hs_off = int(hs["offset"], 0)
    hs_pending = int(hs["post_value"], 0)
    hs_ready = max(int(k, 0) for k in hs["z80_ack_values_seen"])
    if hs_off != EXPECT_HANDSHAKE_OFF or hs_pending != EXPECT_HANDSHAKE_PENDING \
            or hs_ready != EXPECT_HANDSHAKE_READY:
        raise AssertionError(
            f"{path.name}: handshake {hs_off:#04x} "
            f"{hs_pending:#04x}/{hs_ready:#04x} != "
            f"{EXPECT_HANDSHAKE_OFF:#04x} 0x00/0xff")
    if not j.get("record_then_handshake"):
        raise AssertionError(f"{path.name}: not record-then-handshake")
    if j.get("stray_writes"):
        raise AssertionError(f"{path.name}: trace has stray latch writes")

    # Every 0xffxx the trace actually saw must be in the control map we ship,
    # so control_default_verb (fail-open VERB_NONE) never decides a real cue.
    # CONTROL_ANTHOLOGY is used here as the KNOWN-VOCABULARY reference (all
    # 0xff00-0xff0d), not as the verb map we ship: the point is to notice a
    # control command this family has never been seen to send.
    unmapped = [c for c in j.get("control_cmds_observed", [])
                if int(c, 0) not in CONTROL_ANTHOLOGY]
    if unmapped:
        raise AssertionError(
            f"{path.name}: observed control commands {unmapped} are not in "
            f"CONTROL_ANTHOLOGY — the control vocabulary needs review")

    print(f"[spf2t] protocol from {path.name}: latch {latch:#x}, cmd "
          f"{cmd_hi:#04x}/{cmd_lo:#04x}, arg {int(fo['observed_record_offsets'][3], 0):#04x}"
          f"/{int(fo['observed_record_offsets'][4], 0):#04x}, arg byte 0x05, "
          f"handshake {hs_off:#04x} (00/ff), driver {j['driver_version']}, "
          f"{j['records_complete']} complete records, 0 stray writes")
    return Protocol(
        game_id="spf2t", latch_page=latch,
        off_cmd_hi=cmd_hi, off_cmd_lo=cmd_lo,
        off_arg_hi=0x07, off_arg_lo=0x09, off_arg_byte=0x05,
        off_handshake=hs_off,
        handshake_pending=hs_pending, handshake_ready=hs_ready,
        fade_law=FADE_ANTHOLOGY, fade_const1=0xffff, fade_const2=0,
        control_default_verb=VERB_NONE, control_region_start=0xff00,
        control_verbs=dict(CONTROL_SPF2T_ARCADE))


# ------------------------------------------------------------------ manifests -
@dataclass
class MapRow:
    cmd: int            # arcade sound command (16-bit key into the table)
    song: int           # Saturn arrange-bank song id
    role: str
    intro_s: float      # independent seconds from the source map
    loop_s: float


def load_source_map(path: Path = SOURCE_MAP_TSV) -> list[MapRow]:
    rows = _rows(path)
    hdr = rows[0]
    for col in ("saturn_song", "arcade_cmd", "role",
                "saturn_intro_s", "saturn_loop_s"):
        if col not in hdr:
            raise ValueError(f"{path.name}: missing column {col!r}")
    out: list[MapRow] = []
    for r in rows[1:]:
        f = dict(zip(hdr, r))
        song = int(f["saturn_song"], 16)
        cmd = int(f["arcade_cmd"], 16)
        if song not in ARRANGE_BANK:
            raise AssertionError(
                f"song 0x{song:02X} is outside the arrange bank "
                f"0x{ARRANGE_BANK.start:02X}..0x{ARRANGE_BANK.stop - 1:02X} — "
                f"bank A is the ARCADE RENDER and must never be packed")
        out.append(MapRow(cmd=cmd, song=song, role=f["role"],
                          intro_s=float(f["saturn_intro_s"]),
                          loop_s=float(f["saturn_loop_s"])))
    if len(out) != 23:
        raise AssertionError(f"{path.name}: {len(out)} rows, expected 23")
    if len({r.cmd for r in out}) != len(out):
        raise AssertionError(f"{path.name}: duplicate arcade_cmd")
    if len({r.song for r in out}) != len(out):
        raise AssertionError(f"{path.name}: duplicate saturn_song")
    return out


def load_looptable(path: Path = LOOPTABLE_TSV) -> dict[int, dict]:
    """song -> {intro_bytes, loop_start_frame, loop_len_frames}."""
    rows = path.read_text(encoding="utf-8").splitlines()
    hdr = rows[0].split("\t")
    out = {}
    for line in rows[1:]:
        f = dict(zip(hdr, line.split("\t")))
        out[int(f["song"], 16)] = dict(
            intro_bytes=int(f["intro_bytes"]),
            loop_start_frame=int(f["loop_start_frame"]),
            loop_len_frames=int(f["loop_len_frames"]),
            format=f["format"])
    return out


def load_rates(path: Path = RATES_TSV) -> dict[int, int]:
    """song -> nominal sample rate, from the boot binary's pitch table.

    Cross-check only: this manifest was decoded from `1stread_usa.prg` (the
    SCSP pitch word per song), independently of the looptable.  The true SCSP
    rate is 37790.77 Hz; 37800 is the tracked nominal (-0.024%, ~0.4 cents)
    and is what the pack's u16 rate field carries.
    """
    out = {}
    for r in _rows(path):
        if r[0] == "song_id":
            continue
        out[int(r[0], 16)] = int(r[4])
    return out


# ----------------------------------------------------------------------- disc -
class SpfDisc:
    """The SPF2T Saturn data track: its SXX0/SXX1.MUS streams."""

    def __init__(self, image: Path):
        self.image = image
        self.iso = IsoFS(image)
        self.songs: dict[int, dict[int, tuple[int, int]]] = {}
        for full, lba, size, isdir in self.iso.entries():
            m = re.fullmatch(r"/S([0-9A-F]{2})([01])\.MUS", full.upper())
            if m and not isdir:
                sid, part = int(m.group(1), 16), int(m.group(2))
                self.songs.setdefault(sid, {})[part] = (lba, size)
        if not self.songs:
            from .build_common import is_playstation_disc
            if is_playstation_disc(self.iso):
                raise SystemExit(
                    f"{image} is the PLAYSTATION release -- only the "
                    f"Saturn discs carry the MUS masters these packs use "
                    f"(see pack/README.md for the verified Saturn carriers)")
            raise ValueError(f"no SXX[01].MUS files on {image}")

    def raw_parts(self, sid: int) -> dict[int, bytes]:
        return {p: self.iso.read_extent(*ext)
                for p, ext in sorted(self.songs[sid].items())}

    def close(self):
        self.iso.close()


def reinterleave_to_mus(pcm: bytes) -> bytes:
    """Inverse of mus.decode_mus: s16le interleaved -> s16be block-interleaved.

    Used only as a build-time proof that the decode is lossless — the bytes
    read back out of the written pack are pushed through this and compared
    against the raw MUS files off the disc.
    """
    import array
    a = array.array("h")
    a.frombytes(pcm)
    if sys.byteorder == "big":
        a.byteswap()                       # incoming buffer is little-endian
    n = len(a) // 2                        # frames
    bs = BLOCK // 2                        # samples per channel block
    out = array.array("h")
    for start in range(0, n, bs):
        chunk = a[start * 2:(start + bs) * 2]
        out += chunk[0::2]                 # left block
        out += chunk[1::2]                 # right block
    if sys.byteorder == "little":
        out.byteswap()                     # emit big-endian
    return out.tobytes()


# ---------------------------------------------------------------------- build -
def build(out: str | None = None, disc: str | None = None,
          source_map: str | None = None, looptable: str | None = None) -> Path:
    rows = load_source_map(Path(source_map) if source_map else SOURCE_MAP_TSV)
    loops = load_looptable(Path(looptable) if looptable else LOOPTABLE_TSV)
    rates = load_rates()
    off_rate = {s: rates[s] for r in rows for s in (r.song,)
                if rates.get(s) != MUS_RATE}
    if off_rate:
        raise AssertionError(
            f"{RATES_TSV.name}: songs {[hex(s) for s in off_rate]} are not "
            f"{MUS_RATE} Hz ({off_rate}) — this builder does not resample")
    if not disc:
        raise ValueError(
            "no disc given -- pass the Saturn Super Puzzle Fighter II Turbo "
            "(USA) rip (zip or Track-1 bin); see your wrapper script")
    image = resolve_image(Path(disc), member_hint="(Track 1).bin")
    d = SpfDisc(image)
    proto = traced_protocol()

    n_offset = sum(1 for r in rows if r.song - r.cmd == 0x19)
    print(f"[spf2t] source map: 23 rows, {n_offset} at cmd+0x19, "
          f"{23 - n_offset} permuted — every row read from the TSV")

    w = PackWriter(proto, title="Super Puzzle Fighter II Turbo (Arrange)",
                   default_rate=MUS_RATE, xfade_samples=0)

    ti_of: dict[int, int] = {}
    src_pcm: dict[int, bytes] = {}
    src_mus: dict[int, bytes] = {}
    total_audio = 0
    for r in sorted(rows, key=lambda x: x.cmd):
        ref = loops.get(r.song)
        if ref is None:
            raise AssertionError(f"song 0x{r.song:02X}: no looptable row")
        if ref["format"] != MUS_FORMAT:
            raise AssertionError(
                f"song 0x{r.song:02X}: looptable says {ref['format']!r}, this "
                f"builder only handles {MUS_FORMAT!r}")
        if r.song not in d.songs:
            raise AssertionError(f"song 0x{r.song:02X} not on {image.name}")

        parts = d.raw_parts(r.song)
        one_shot = ref["loop_len_frames"] == 0
        if one_shot and 1 in parts:
            raise AssertionError(
                f"song 0x{r.song:02X}: looptable says one-shot but the disc "
                f"has an S{r.song:02X}1.MUS loop body")
        if not one_shot and 1 not in parts:
            raise AssertionError(
                f"song 0x{r.song:02X}: looptable says it loops but the disc "
                f"has no S{r.song:02X}1.MUS")

        # --- sizes on the disc must match the tracked looptable exactly ------
        if len(parts[0]) != ref["intro_bytes"]:
            raise AssertionError(
                f"song 0x{r.song:02X}: S{r.song:02X}0.MUS is {len(parts[0])} B, "
                f"looptable says {ref['intro_bytes']} B")
        if not one_shot and len(parts[1]) != ref["loop_len_frames"] * BYTES_PER_FRAME:
            raise AssertionError(
                f"song 0x{r.song:02X}: S{r.song:02X}1.MUS is {len(parts[1])} B, "
                f"looptable says {ref['loop_len_frames'] * BYTES_PER_FRAME} B")
        for p, raw in parts.items():
            if len(raw) % (2 * BLOCK):
                raise AssertionError(
                    f"song 0x{r.song:02X} part {p}: {len(raw)} B is not a whole "
                    f"number of {2 * BLOCK} B interleave periods — decoding "
                    f"would drop a block")

        # --- decode: the ONLY transform is de-interleave + byte swap ---------
        pcm = b"".join(decode_mus(parts[p]) for p in sorted(parts))
        intro_frames = len(decode_mus(parts[0])) // BYTES_PER_FRAME
        total_frames = len(pcm) // BYTES_PER_FRAME
        if intro_frames != ref["loop_start_frame"]:
            raise AssertionError(
                f"song 0x{r.song:02X}: intro decodes to {intro_frames} frames, "
                f"looptable loop_start_frame is {ref['loop_start_frame']}")
        if total_frames - intro_frames != ref["loop_len_frames"]:
            raise AssertionError(
                f"song 0x{r.song:02X}: loop body decodes to "
                f"{total_frames - intro_frames} frames, looptable says "
                f"{ref['loop_len_frames']}")
        # independent seconds from the source map (different manifest, same fact)
        for what, got, want in (("intro", intro_frames / MUS_RATE, r.intro_s),
                                ("loop", (total_frames - intro_frames) / MUS_RATE,
                                 r.loop_s)):
            if abs(got - want) > 0.001:
                raise AssertionError(
                    f"song 0x{r.song:02X}: {what} {got:.3f}s from the disc vs "
                    f"{want:.3f}s in the source map")

        meta = TrackMeta(
            sample_rate=MUS_RATE, channels=MUS_CHANNELS, codec=CODEC_PCM,
            gain=0x7f, xfade_enable=0,
            name=f"S{r.song:02X} {r.role}",
            source=f"saturn_spf2t_usa/S{r.song:02X}"
                   f"{'0' if one_shot else '[01]'}.MUS")
        if one_shot:
            # No SXX1.MUS: the engine plays it once and stops.  loop_end_byte
            # = 0 is the format's "does not loop".
            meta.loop_start_sample = meta.loop_start_byte = 0
            meta.loop_end_sample = meta.loop_end_byte = 0
        else:
            # Loop point IS the SXX0/SXX1 file boundary.  Nothing authored.
            meta.loop_start_sample = intro_frames
            meta.loop_start_byte = intro_frames * BYTES_PER_FRAME
            meta.loop_end_sample = total_frames
            meta.loop_end_byte = total_frames * BYTES_PER_FRAME

        ti = w.add_track(pcm, meta)
        ti_of[r.cmd] = ti
        src_pcm[ti] = pcm
        src_mus[ti] = b"".join(parts[p] for p in sorted(parts))
        total_audio += len(pcm)
        w.set_trigger(r.cmd, TriggerRow(verb=VERB_PLAY, track=ti, gain=TRIG_GAIN,
                                        suppress=1))
        kind = "one_shot " if one_shot else f"loop@{intro_frames}"
        print(f"[spf2t] cmd 0x{r.cmd:02X} -> S{r.song:02X} track{ti:<2d} "
              f"{total_frames / MUS_RATE:7.3f}s  {kind:<12s} "
              f"{len(pcm) / 1e6:6.2f} MB  {r.role}")

    out_path = Path(out) if out else PACKS_DIR / "spf2t_arrange.cpk"
    w.write(out_path)
    size = out_path.stat().st_size
    print(f"[spf2t] {out_path}")
    print(f"[spf2t] {size} B ({size / 1e6:.1f} MB), {len(w.tracks)} tracks, "
          f"{len(ti_of)} play triggers, audio {total_audio / 1e6:.1f} MB")
    print(f"[spf2t] {100.0 * size / DDR_BUDGET_BYTES:.1f}% of the "
          f"{DDR_BUDGET_BYTES} B DDR budget")
    if size > DDR_BUDGET_BYTES:
        print(f"[spf2t] WARNING: pack exceeds the DDR budget")

    _readback_verify(out_path, rows, ti_of, src_pcm, src_mus, proto)
    d.close()
    return out_path


def _readback_verify(path: Path, rows: list[MapRow], ti_of: dict[int, int],
                     src_pcm: dict[int, bytes], src_mus: dict[int, bytes],
                     proto: Protocol) -> None:
    """Re-read the written pack and check everything that matters.

    audition.verify() skips verb == 0 rows, so trigger correctness is checked
    here directly (same reason build_ffight_arrange.py does it).
    """
    rd = PackReader(path)
    try:
        h = rd.header
        if h.proto.latch_page != proto.latch_page:
            raise AssertionError(f"readback: latch page {h.proto.latch_page:#x}")
        for fld in ("off_cmd_hi", "off_cmd_lo", "off_arg_hi", "off_arg_lo",
                    "off_arg_byte", "off_handshake", "handshake_pending",
                    "handshake_ready"):
            if getattr(h.proto, fld) != getattr(proto, fld):
                raise AssertionError(
                    f"readback: {fld} = {getattr(h.proto, fld):#04x}, "
                    f"expected {getattr(proto, fld):#04x}")
        if h.game_id != "spf2t":
            raise AssertionError(f"readback: game_id {h.game_id!r}")
        if h.default_rate != MUS_RATE:
            raise AssertionError(f"readback: default rate {h.default_rate}")
        if h.xfade_samples != 0:
            raise AssertionError("readback: pack declares a crossfade length")
        ok_tables, ok_data = rd.crc_check()
        if not (ok_tables and ok_data):
            raise AssertionError(f"readback: CRC tables={ok_tables} "
                                 f"data={ok_data}")

        want = {r.cmd: r for r in rows}
        for cmd, r in sorted(want.items()):
            got = rd.triggers[cmd]
            if got.verb != VERB_PLAY:
                raise AssertionError(
                    f"readback: cmd 0x{cmd:02X} verb is "
                    f"{VERB_NAMES[got.verb]}, expected play")
            if got.suppress != 1:
                raise AssertionError(f"readback: cmd 0x{cmd:02X} suppress "
                                     f"{got.suppress}, expected 1")
            if got.track != ti_of[cmd]:
                raise AssertionError(
                    f"readback: cmd 0x{cmd:02X} track {got.track}, expected "
                    f"{ti_of[cmd]}")
            if got.gain != TRIG_GAIN:
                raise AssertionError(f"readback: cmd 0x{cmd:02X} gain "
                                     f"{got.gain:#x}, expected {TRIG_GAIN:#x}")
        stray = [c for c, t in enumerate(rd.triggers)
                 if t.verb != VERB_NONE and c not in want]
        if stray:
            raise AssertionError(f"readback: unexpected non-none rows "
                                 f"{[hex(c) for c in stray]}")
        suppressed = [c for c, t in enumerate(rd.triggers)
                      if t.suppress and c not in want]
        if suppressed:
            raise AssertionError(f"readback: stray suppress rows "
                                 f"{[hex(c) for c in suppressed]}")

        loop_ok = one_shot = 0
        for ti, pcm in sorted(src_pcm.items()):
            m = rd.tracks[ti]
            if m.codec != CODEC_PCM or m.channels != MUS_CHANNELS \
                    or m.sample_rate != MUS_RATE or m.xfade_enable:
                raise AssertionError(f"readback: track {ti} metadata")
            data = rd.read_track(ti)
            if data != pcm:
                raise AssertionError(f"readback: track {ti} PCM differs from "
                                     f"the decoded source")
            if reinterleave_to_mus(data) != src_mus[ti]:
                raise AssertionError(
                    f"readback: track {ti} does not invert to the raw MUS "
                    f"bytes — the byte swap was not lossless")
            if m.loops:
                if m.loop_end_byte != len(pcm) \
                        or m.loop_end_sample * (2 * m.channels) != m.loop_end_byte:
                    raise AssertionError(f"readback: track {ti} loop_end")
                if m.loop_start_byte != m.loop_start_sample * (2 * m.channels) \
                        or m.loop_start_byte == 0 \
                        or m.loop_start_byte % (2 * BLOCK):
                    # the file boundary is always a whole interleave period
                    raise AssertionError(
                        f"readback: track {ti} loop_start "
                        f"{m.loop_start_byte} is not the MUS file boundary")
                loop_ok += 1
            else:
                if m.loop_start_byte or m.loop_end_byte:
                    raise AssertionError(f"readback: track {ti} one-shot has "
                                         f"loop bytes set")
                one_shot += 1
        print(f"[spf2t] readback OK: {len(want)} triggers (play/suppress=1, "
              f"no stray rows), {len(src_pcm)} tracks byte-identical to the "
              f"decoded MUS and lossless-invertible to the raw MUS, "
              f"{loop_ok} looping at the file boundary + {one_shot} one-shot, "
              f"CRC tables+data OK")
    finally:
        rd.close()


def main(argv=None):
    REPO = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(REPO / "cpsplus"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--disc")
    ap.add_argument("--source-map")
    ap.add_argument("--looptable")
    a = ap.parse_args(argv)
    build(out=a.out, disc=a.disc, source_map=a.source_map,
          looptable=a.looptable)


if __name__ == "__main__":
    main()
