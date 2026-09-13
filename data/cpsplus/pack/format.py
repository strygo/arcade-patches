"""CPS2 audio pack (.cpk) binary layout v0 — writer and reader.

This module IS the layout contract: every offset here is frozen in
PACK_FORMAT.md §Binary layout v0.  All integers little-endian.

File structure:
    0x0000  HEADER (4096 B fixed)         — identity, section table,
                                            protocol descriptor, verb map
    0x1000  TRIGGER TABLE (rows × 4 B)    — direct-mapped by 16-bit command
    ......  TRACK INDEX (count × 32 B)
    ......  TRACK DATA (64 B aligned per track; ADX frame streams with
            container headers stripped, or raw s16le PCM)

Semantics notes (also in PACK_FORMAT.md):
  * A track loops iff loop_end_byte != 0.  The player wraps its read
    pointer after consuming loop_end_byte bytes of the track stream and
    continues at loop_start_byte, restoring the ADX predictor history it
    latched when it first crossed loop_start_byte.
  * loop_*_sample fields are the sample-domain mirror of the byte fields
    (loop_start exact; loop_end may differ from the byte-derived count by
    <32 samples when imported from CRI headers — the byte field is the
    authoritative wrap point).
  * gain is linear, 0x7f = unity.  Effective playback gain =
    (trigger_row.gain/127) * (track.gain/127).
"""
from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field, asdict
from pathlib import Path

MAGIC = b"CP2A"
FORMAT_VERSION = 2
# v2 (2026-09-11, NG+): byte-latch dialects carry an argument-command bitmap and
# fade law 3.  A pack that uses neither is still WRITTEN as v1 so every existing
# CPS pack stays byte-identical to its published hash; readers accept <= 2.
BUILDER_VERSION = 1
HEADER_SIZE = 4096
# Offset of the v1 global crossfade length (u16, samples).  Placed clear of
# the control-verb map (0xa8 .. 0xa8+32*4 = 0x128 max) so no collision.
OFF_XFADE_SAMPLES = 0x128
OFF_DIALECT = 0x12a          # v2: u8 dialect (0 QSound record, 1 CPS1 byte, 2 Neo Geo byte+args)
OFF_ARGSET = 0x130           # v2: 32-byte bitmap, bit c set = command c consumes the NEXT byte
DIALECT_QSOUND = 0
DIALECT_CPS1_BYTE = 1
DIALECT_NEOGEO_BYTE = 2
TRIGGER_ROW_SIZE = 4
TRACK_INDEX_SIZE = 32
TRACK_ALIGN = 64
DEFAULT_TRIGGER_ROWS = 0x1200        # commands 0x0000..0x11FF, per PACK_FORMAT.md
DDR_BUDGET_BYTES = 268_435_456  # 256 MiB at 0x30000000. VERIFIED, not assumed:
#   jtframe hardcodes the top nibble (ddram_addr = {4'd3, ddram_page, 0}) with an
#   18-bit page counter x 1 kB = exactly 2**28, wrapping back to 0x30000000;
#   Main_MiSTer masks every DDR write (fpga_mem(x) = 0x20000000 | (x & 0x1FFFFFFF));
#   u-boot mem=511M against exactly 1 GB, so 0x40000000 is the end of RAM.

# --- verbs (trigger rows use 0..2; control map uses 0 and 2..6) --------------
VERB_NONE = 0
VERB_PLAY = 1
VERB_STOP = 2
VERB_FADE_OUT = 3     # fade to target, loop switch off  (Anthology 0xff06)
VERB_FADE_KEEP = 4    # fade to target, loop preserved   (0xff07 / HSF2 0xff06)
VERB_RESTORE = 5      # restore full volume              (0xff0c)
VERB_MASTER_FADE = 6  # master fade-out to silence       (0xff0d)
VERB_NAMES = {0: "none", 1: "play", 2: "stop", 3: "fade_out",
              4: "fade_keep", 5: "restore", 6: "master_fade"}

# --- fade laws ----------------------------------------------------------------
FADE_NONE = 0
FADE_ANTHOLOGY = 1  # steps = const1 / arg, stepped per frame (const1=0xffff)
FADE_HSF2 = 2       # steps = (const1 / arg) * const2 (const1=0x444, const2=60)
FADE_NG_MAKOTO = 3  # frames = const2 + const1 / arg; target fixed at 0 (arg is the
                    # driver's speed byte, not a level).  samsho2 measured: const1=5860,
                    # const2=59 -> 0x20: 4.1 s, 0x60: 2.0 s, 0xa0: 1.6 s, 0xff: 1.4 s

# --- codecs --------------------------------------------------------------------
CODEC_ADX = 0
CODEC_PCM = 1  # s16le, channel-interleaved

MAX_CONTROL_VERBS = 32


@dataclass
class Protocol:
    """Per-game protocol descriptor (header fields 0x08c..)."""
    game_id: str
    latch_page: int = 0x618000
    off_cmd_hi: int = 0x01
    off_cmd_lo: int = 0x03
    off_arg_hi: int = 0x07
    off_arg_lo: int = 0x09
    off_arg_byte: int = 0x05
    off_handshake: int = 0x1f
    handshake_pending: int = 0x00
    handshake_ready: int = 0xff
    fade_law: int = FADE_NONE
    fade_const1: int = 0
    fade_const2: int = 0
    control_default_verb: int = VERB_NONE
    control_region_start: int = 0xff00
    control_verbs: dict = field(default_factory=dict)  # cmd -> verb
    dialect: int = DIALECT_QSOUND                       # v2
    arg_commands: set = field(default_factory=set)      # v2: commands that take one argument byte

    def uses_v2(self) -> bool:
        return self.dialect != DIALECT_QSOUND or bool(self.arg_commands) or self.fade_law == FADE_NG_MAKOTO


@dataclass
class TriggerRow:
    verb: int = VERB_NONE
    track: int = 0        # 12-bit
    gain: int = 0x7f      # 0..0x7f
    suppress: int = 0     # 1 = gate the Z80 handshake for this command

    def pack(self) -> bytes:
        assert 0 <= self.track < 0x1000 and 0 <= self.gain <= 0x7f
        b3 = (self.suppress & 1) | ((self.track >> 8) << 4)
        return bytes((self.verb, self.track & 0xff, self.gain, b3))

    @classmethod
    def unpack(cls, b: bytes) -> "TriggerRow":
        return cls(verb=b[0], track=b[1] | ((b[3] >> 4) << 8),
                   gain=b[2], suppress=b[3] & 1)


@dataclass
class TrackMeta:
    sample_rate: int
    channels: int
    codec: int = CODEC_ADX
    gain: int = 0x7f
    loop_start_sample: int = 0
    loop_start_byte: int = 0
    loop_end_sample: int = 0
    loop_end_byte: int = 0      # 0 = track does not loop
    coef1: int = 0              # ADX predictor coefficients, s16 (0 for PCM)
    coef2: int = 0
    xfade_enable: int = 0       # 1 = player crossfades this loop (see below)
    loop_count: int = 0         # 0 = loop forever (stock); 1-3 = wrap N times,
                                # then play THROUGH loop_end to the stream end
                                # (the source's own outro/fade).  A finite-count
                                # track stores the WHOLE stream, xfade or not.
    # sidecar-only fields (not in the binary index):
    name: str = ""
    source: str = ""
    # filled by reader/writer:
    data_offset: int = 0
    data_length: int = 0

    @property
    def loops(self) -> bool:
        return self.loop_end_byte != 0

    def pack(self) -> bytes:
        # codec byte (0x1a): bits0-3 channels, bit4 codec, bit5 crossfade,
        # bits6-7 loop_count (0 = infinite)
        assert self.channels in (1, 2) and self.codec in (CODEC_ADX, CODEC_PCM)
        assert 0 <= self.loop_count <= 3
        chan_codec = ((self.channels & 0x0f) | (self.codec << 4)
                      | ((self.xfade_enable & 1) << 5)
                      | ((self.loop_count & 3) << 6))
        return struct.pack(
            "<IIIIIIHBBhh",
            self.data_offset, self.data_length,
            self.loop_start_sample, self.loop_start_byte,
            self.loop_end_sample, self.loop_end_byte,
            self.sample_rate, chan_codec, self.gain,
            self.coef1, self.coef2)

    @classmethod
    def unpack(cls, b: bytes) -> "TrackMeta":
        (do, dl, lss, lsb, les, leb, rate, cc, gain, c1, c2) = struct.unpack(
            "<IIIIIIHBBhh", b)
        return cls(sample_rate=rate, channels=cc & 0x0f, codec=(cc >> 4) & 1,
                   xfade_enable=(cc >> 5) & 1, loop_count=(cc >> 6) & 3,
                   gain=gain, loop_start_sample=lss, loop_start_byte=lsb,
                   loop_end_sample=les, loop_end_byte=leb,
                   coef1=c1, coef2=c2, data_offset=do, data_length=dl)


# ------------------------------------------------------------------ header ----
_HDR_FIXED = struct.Struct("<4sHH16s64sHHIIIIQQQII")   # 0x000..0x08c
_HDR_PROTO = struct.Struct("<I8BBBHIII")               # 0x08c..0x0a8


def _pack_header(*, game_id: str, title: str, default_rate: int,
                 trigger_offset: int, trigger_rows: int,
                 index_offset: int, track_count: int,
                 data_offset: int, data_size: int, file_size: int,
                 crc_tables: int, crc_data: int, proto: Protocol,
                 xfade_samples: int = 0) -> bytes:
    hdr = bytearray(HEADER_SIZE)
    version = FORMAT_VERSION if proto.uses_v2() else 1
    _HDR_FIXED.pack_into(
        hdr, 0,
        MAGIC, version, HEADER_SIZE,
        game_id.encode()[:16].ljust(16, b"\0"),
        title.encode()[:64].ljust(64, b"\0"),
        BUILDER_VERSION, default_rate,
        trigger_offset, trigger_rows, index_offset, track_count,
        data_offset, data_size, file_size, crc_tables, crc_data)
    verbs = sorted(proto.control_verbs.items())
    if len(verbs) > MAX_CONTROL_VERBS:
        raise ValueError("too many control verbs")
    _HDR_PROTO.pack_into(
        hdr, 0x8c,
        proto.latch_page,
        proto.off_cmd_hi, proto.off_cmd_lo, proto.off_arg_hi, proto.off_arg_lo,
        proto.off_arg_byte, proto.off_handshake,
        proto.handshake_pending, proto.handshake_ready,
        proto.fade_law, proto.control_default_verb, proto.control_region_start,
        proto.fade_const1, proto.fade_const2, len(verbs))
    off = 0xa8
    for cmd, verb in verbs:
        struct.pack_into("<HBB", hdr, off, cmd, verb, 0)
        off += 4
    # v1: global loop-crossfade length (samples); 0 = no crossfade in this pack
    struct.pack_into("<H", hdr, OFF_XFADE_SAMPLES, xfade_samples & 0xffff)
    if version >= 2:
        hdr[OFF_DIALECT] = proto.dialect & 0xff
        bits = 0
        for c in proto.arg_commands:
            bits |= 1 << (c & 0xff)
        hdr[OFF_ARGSET:OFF_ARGSET + 32] = bits.to_bytes(32, 'little')
    return bytes(hdr)


@dataclass
class Header:
    game_id: str
    title: str
    default_rate: int
    trigger_offset: int
    trigger_rows: int
    index_offset: int
    track_count: int
    data_offset: int
    data_size: int
    file_size: int
    crc_tables: int
    crc_data: int
    builder_version: int
    proto: Protocol
    format_version: int = 0
    xfade_samples: int = 0


def _parse_header(hdr: bytes) -> Header:
    (magic, ver, hsize, gid, title, bver, drate,
     toff, trows, ioff, tcount, doff, dsize, fsize,
     crc_t, crc_d) = _HDR_FIXED.unpack_from(hdr, 0)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    if ver > FORMAT_VERSION:
        raise ValueError(f"unsupported format version {ver}")
    if hsize != HEADER_SIZE:
        raise ValueError(f"bad header size {hsize}")
    # v1: global crossfade length (0 on v0 packs — that region is zero-filled)
    xfade_samples = struct.unpack_from("<H", hdr, OFF_XFADE_SAMPLES)[0] \
        if ver >= 1 else 0
    dialect, argset = DIALECT_QSOUND, set()
    if ver >= 2:
        dialect = hdr[OFF_DIALECT]
        bits = int.from_bytes(hdr[OFF_ARGSET:OFF_ARGSET + 32], 'little')
        argset = {c for c in range(256) if bits >> c & 1}
    (latch, o1, o2, o3, o4, o5, o6, hp, hr,
     law, dverb, cstart, fc1, fc2, nverbs) = _HDR_PROTO.unpack_from(hdr, 0x8c)
    verbs = {}
    off = 0xa8
    for _ in range(nverbs):
        cmd, verb, _r = struct.unpack_from("<HBB", hdr, off)
        verbs[cmd] = verb
        off += 4
    proto = Protocol(
        game_id=gid.rstrip(b"\0").decode(), latch_page=latch,
        off_cmd_hi=o1, off_cmd_lo=o2, off_arg_hi=o3, off_arg_lo=o4,
        off_arg_byte=o5, off_handshake=o6,
        handshake_pending=hp, handshake_ready=hr,
        fade_law=law, fade_const1=fc1, fade_const2=fc2,
        control_default_verb=dverb, control_region_start=cstart,
        control_verbs=verbs, dialect=dialect, arg_commands=argset)
    return Header(
        game_id=proto.game_id, title=title.rstrip(b"\0").decode(),
        default_rate=drate, trigger_offset=toff, trigger_rows=trows,
        index_offset=ioff, track_count=tcount, data_offset=doff,
        data_size=dsize, file_size=fsize, crc_tables=crc_t, crc_data=crc_d,
        builder_version=bver, proto=proto, format_version=ver,
        xfade_samples=xfade_samples)


# ------------------------------------------------------------------ writer ----
class PackWriter:
    """Assembles a pack in memory, then writes it in one pass.

    Usage:
        w = PackWriter(proto, title="...")
        t = w.add_track(stream_bytes, TrackMeta(...))
        w.set_trigger(0x01, TriggerRow(verb=VERB_PLAY, track=t, ...))
        w.write(path)          # also writes <path>.json sidecar
    """

    def __init__(self, proto: Protocol, title: str = "",
                 trigger_rows: int = DEFAULT_TRIGGER_ROWS,
                 default_rate: int = 48000, xfade_samples: int = 0):
        self.proto = proto
        self.title = title
        self.default_rate = default_rate
        self.xfade_samples = xfade_samples
        self.triggers = [TriggerRow() for _ in range(trigger_rows)]
        self.tracks: list[tuple[bytes, TrackMeta]] = []

    def add_track(self, data: bytes, meta: TrackMeta) -> int:
        if meta.codec == CODEC_ADX:
            fb = 18 * meta.channels
            if len(data) % fb:
                raise ValueError(
                    f"ADX stream length {len(data)} not a multiple of {fb}")
            if meta.loop_end_byte and meta.loop_end_byte > len(data):
                raise ValueError("loop_end_byte beyond track data")
        if meta.xfade_enable:
            # crossfade tracks keep xfade_samples of frames past loop_end (the
            # tail the player decodes and blends against the loop head), so the
            # stored stream must extend beyond loop_end by exactly that much.
            if not meta.loops:
                raise ValueError("xfade_enable set on a non-looping track")
            if not self.xfade_samples:
                raise ValueError("xfade_enable set but pack xfade_samples == 0")
            unit = (18 if meta.codec == CODEC_ADX else 2) * meta.channels
            tail = self.xfade_samples // 32 * 18 * meta.channels \
                if meta.codec == CODEC_ADX \
                else self.xfade_samples * 2 * meta.channels
            want = meta.loop_end_byte + tail
            if meta.loop_count:
                # finite-count tracks play THROUGH loop_end after the last wrap,
                # so they store the whole stream; the blend material past
                # loop_end is simply the stream's own continuation.
                if len(data) < want:
                    raise ValueError(
                        f"finite-loop xfade track shorter than loop_end+tail "
                        f"({len(data)} < {want})")
            elif len(data) != want:
                raise ValueError(
                    f"xfade track needs loop_end+tail = {want} bytes "
                    f"(loop_end {meta.loop_end_byte} + {tail} tail), "
                    f"got {len(data)}")
            if want % unit:
                raise ValueError("xfade tail not codec-frame aligned")
        self.tracks.append((data, meta))
        return len(self.tracks) - 1

    def set_trigger(self, cmd: int, row: TriggerRow):
        if not 0 <= cmd < len(self.triggers):
            raise ValueError(f"command 0x{cmd:04x} outside trigger table")
        self.triggers[cmd] = row

    def write(self, path: Path | str, sidecar: bool = True) -> Path:
        path = Path(path)
        trig = b"".join(r.pack() for r in self.triggers)
        trigger_offset = HEADER_SIZE
        index_offset = trigger_offset + len(trig)

        # lay out track data
        blobs = []
        pos = 0
        for data, meta in self.tracks:
            pad = (-pos) % TRACK_ALIGN
            pos += pad
            meta.data_offset = pos
            meta.data_length = len(data)
            blobs.append((pad, data))
            pos += len(data)
        data_size = pos
        index = b"".join(m.pack() for _, m in self.tracks)
        data_offset = index_offset + len(index)
        data_offset += (-data_offset) % TRACK_ALIGN
        index_pad = data_offset - index_offset - len(index)
        file_size = data_offset + data_size

        crc_tables = zlib.crc32(trig)
        crc_tables = zlib.crc32(index, crc_tables)
        crc_data = 0
        for pad, data in blobs:
            crc_data = zlib.crc32(b"\0" * pad, crc_data)
            crc_data = zlib.crc32(data, crc_data)

        hdr = _pack_header(
            game_id=self.proto.game_id, title=self.title,
            default_rate=self.default_rate,
            trigger_offset=trigger_offset, trigger_rows=len(self.triggers),
            index_offset=index_offset, track_count=len(self.tracks),
            data_offset=data_offset, data_size=data_size, file_size=file_size,
            crc_tables=crc_tables, crc_data=crc_data, proto=self.proto,
            xfade_samples=self.xfade_samples)

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(hdr)
            f.write(trig)
            f.write(index)
            f.write(b"\0" * index_pad)
            for pad, data in blobs:
                f.write(b"\0" * pad)
                f.write(data)
        if sidecar:
            self._write_sidecar(path)
        return path

    def _write_sidecar(self, path: Path):
        rows = {}
        for cmd, r in enumerate(self.triggers):
            if r.verb != VERB_NONE:
                rows[f"0x{cmd:04x}"] = {
                    "verb": VERB_NAMES[r.verb], "track": r.track,
                    "gain": r.gain, "suppress": r.suppress}
        side = {
            "game": self.proto.game_id, "title": self.title,
            "format_version": FORMAT_VERSION,
            "xfade_samples": self.xfade_samples,
            "tracks": [
                {"index": i, "name": m.name, "source": m.source,
                 "rate": m.sample_rate, "channels": m.channels,
                 "codec": {0: "adx", 1: "pcm"}[m.codec], "gain": m.gain,
                 "bytes": m.data_length, "loops": m.loops,
                 "xfade_enable": m.xfade_enable,
                 "loop_start_sample": m.loop_start_sample,
                 "loop_end_sample": m.loop_end_sample,
                 "loop_start_byte": m.loop_start_byte,
                 "loop_end_byte": m.loop_end_byte}
                for i, (_, m) in enumerate(self.tracks)],
            "triggers": rows,
        }
        Path(str(path) + ".json").write_text(json.dumps(side, indent=1))


# ------------------------------------------------------------------ reader ----
class PackReader:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.f = open(self.path, "rb")
        self.header = _parse_header(self.f.read(HEADER_SIZE))
        h = self.header
        self.f.seek(h.trigger_offset)
        trig = self.f.read(h.trigger_rows * TRIGGER_ROW_SIZE)
        self.triggers = [TriggerRow.unpack(trig[i * 4:i * 4 + 4])
                         for i in range(h.trigger_rows)]
        self.f.seek(h.index_offset)
        idx = self.f.read(h.track_count * TRACK_INDEX_SIZE)
        self.tracks = [TrackMeta.unpack(idx[i * 32:i * 32 + 32])
                       for i in range(h.track_count)]
        self._raw_trig, self._raw_idx = trig, idx
        side = Path(str(self.path) + ".json")
        self.sidecar = json.loads(side.read_text()) if side.exists() else None
        if self.sidecar and len(self.sidecar.get("tracks", [])) != len(self.tracks):
            # a stale sidecar from an older build of the same pack name must
            # not poison reads -- names/sources are cosmetic, drop them
            print(f"[pack] WARNING: {side.name} track count differs from the "
                  f"binary index -- stale sidecar ignored")
            self.sidecar = None
        if self.sidecar:
            for i, m in enumerate(self.tracks):
                st = self.sidecar["tracks"][i]
                m.name, m.source = st.get("name", ""), st.get("source", "")

    def read_track(self, i: int) -> bytes:
        m = self.tracks[i]
        self.f.seek(self.header.data_offset + m.data_offset)
        return self.f.read(m.data_length)

    def crc_check(self) -> tuple[bool, bool]:
        h = self.header
        ok_tables = zlib.crc32(self._raw_idx, zlib.crc32(self._raw_trig)) \
            == h.crc_tables
        self.f.seek(h.data_offset)
        crc = 0
        left = h.data_size
        while left:
            chunk = self.f.read(min(left, 1 << 22))
            if not chunk:
                return ok_tables, False
            crc = zlib.crc32(chunk, crc)
            left -= len(chunk)
        return ok_tables, crc == h.crc_data

    def close(self):
        self.f.close()
