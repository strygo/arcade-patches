"""`build_pack.py saturn-mus` — SFZ2 pack from the Saturn disc(s), for the
sfz2al (SF Zero 2 Alpha / Alpha 2 Gold vocabulary) arcade core.

Audio: Saturn MUS streams (s16BE stereo 32000 Hz, block-interleaved 4096;
loop point = the SXX0/SXX1 file boundary — sample-exact, zero gap risk),
ADX-encoded at build time (exactly one lossy step, Capcom's own pipeline).
Per the master-carrier verdict, Saturn PCM is the preferred
carrier of the 1996 Alpha 2 arrangement over the Anthology zero6 ADX
(same recordings, lossless source, less noise).

Trigger table: manifests/sfz2al_arrange_trigger_map.tsv — a tracked
copy of Capcom's own zero6 dispatch table (Anthology comp2 +0x17fa0,
US), joined to Saturn songs via the verified identity
    adx_entry = 169 + saturn_song_id
(manifests/sfz2_saturn_zero6_map.tsv, all 60 songs, high confidence — kept
as the evidence file and cross-checked at build time).  This map is used
directly; an explicit --trigger-map TSV overrides.

CAVEAT: this covers the sfz2al command vocabulary only.  The base
SFZ2/SFA2 (non-Gold) arcade sets need their own Phase-0 command trace.

Loop-forever vs one-shot flags come from each disc's own 0.BIN control
table (8 B/song: byte0 = separate-loop-file, byte7 = 0xff loop forever),
located by the research doc's blind pattern search and cross-checked
against manifests/sfz2_saturn_looptable.tsv.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

from . import adxcodec, protocols
from .build_common import MANIFESTS, PACKS_DIR, REPO_ROOT
from .format import (PackWriter, TrackMeta, TriggerRow, CODEC_ADX,
                     VERB_PLAY, VERB_STOP)
from .isofs import IsoFS
from .mus import decode_mus, MUS_RATE, MUS_CHANNELS
from .sources import resolve_image

# Measured loudness match (EBU R128 vs the native chip music,
# tools/loudness_match.py).  Shipped packs carried this in their trigger rows
# via a post-build set_gain.py patch, which a from-source rebuild silently
# discarded; baked here so rebuilds reproduce the shipped packs byte-exactly.
TRIG_GAIN = {"sfa2": 0x25, "sfz2al": 0x25}   # per game; others keep the map gain

ADX_ENTRY_BASE = 169             # adx_entry = 169 + saturn_song_id
SATURN_MAP_TSV = MANIFESTS / "sfz2_saturn_zero6_map.tsv"
LOOPTABLE_TSV = MANIFESTS / "sfz2_saturn_looptable.tsv"


class SaturnDisc:
    """One Saturn data track: its MUS files and 0.BIN control table."""

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
        self.flags = self._control_flags()

    def _control_flags(self) -> dict[int, tuple[int, int]]:
        """song id -> (pair_flag, loop_forever_flag) from 0.BIN."""
        bin0 = self.iso.read_file("/0.BIN")
        expect = {sid: (1 if 1 in parts else 0)
                  for sid, parts in self.songs.items()}
        max_id = max(expect)
        hits = []
        for base in range(0, len(bin0) - (max_id + 1) * 8):
            for sid, pair in expect.items():
                o = base + sid * 8
                if bin0[o] != pair or bin0[o + 7] not in (0, 0xff):
                    break
            else:
                hits.append(base)
        if len(hits) != 1:
            raise ValueError(
                f"0.BIN control-table search on {self.image.name}: "
                f"{len(hits)} hits {[hex(h) for h in hits[:4]]} (need 1)")
        base = hits[0]
        print(f"[saturn] {self.image.name}: control table at 0.BIN+0x{base:x}"
              f" ({len(self.songs)} songs)")
        return {sid: (bin0[base + sid * 8], bin0[base + sid * 8 + 7])
                for sid in self.songs}

    def read_song_pcm(self, sid: int) -> tuple[bytes, int]:
        """Return (s16le stereo PCM, intro_frames) for a song."""
        parts = self.songs[sid]
        intro = decode_mus(self.iso.read_extent(*parts[0]))
        if 1 in parts:
            loop = decode_mus(self.iso.read_extent(*parts[1]))
            return intro + loop, len(intro) // 4
        return intro, 0


def _load_trigger_map_tsv(path: Path) -> tuple[dict[int, tuple[int, int | None]],
                                               list[int]]:
    """Trigger map TSV -> ({cmd: (song_id, gain or None)}, [stop_cmds]).

    Row shapes:  cmd \t song_hex [\t gain]        (legacy play row)
                 cmd \t song_hex \t play \t vol   (snapshot play row)
                 cmd \t -        \t stop \t -     (snapshot stop row)
    The vol column of snapshot rows is provenance only (see the manifest
    header); gain on legacy rows is honoured as before."""
    plays: dict[int, tuple[int, int | None]] = {}
    stops: list[int] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.lower().startswith("cmd"):
            continue
        f = line.split("\t")
        cmd = int(f[0], 0)
        if len(f) > 2 and f[2] == "stop":
            stops.append(cmd)
            continue
        gain = None
        if len(f) > 2 and f[2] not in ("play", ""):
            gain = int(f[2], 0)
        plays[cmd] = (int(f[1], 16), gain)
    return plays, stops


def build(iso_path: str, out: str | None = None,
          game: str = "sfz2al", trigger_map: str | None = None,
          crosscheck: bool = True) -> Path:
    # one disc suffices for BOTH games (measured: SFZ2' carries
    # all 60 songs; the 59 shared with the base disc are byte-identical) --
    # sfa2 pins the base disc, sfz2al the dash disc
    discs = [SaturnDisc(resolve_image(iso_path, member_hint="(Track 1).bin"))]

    # trigger source: every game ships a tracked per-pack manifest
    # (sfa2 = service-mode sweep; sfz2al = the comp2 snapshot, see its header)
    if not trigger_map:
        cand = MANIFESTS / f"{game}_arrange_trigger_map.tsv"
        if not cand.exists():
            raise ValueError(
                f"no verified trigger map for game {game!r} — this needs a "
                f"Phase-0 command trace; pass --trigger-map")
        trigger_map = cand
    plays, stops = _load_trigger_map_tsv(Path(trigger_map))
    print(f"[saturn] trigger map from {trigger_map}: "
          f"{len(plays)} play + {len(stops)} stop rows")
    if game == "sfz2al" and SATURN_MAP_TSV.exists():
        # consistency assert vs the identity-map evidence file
        want = {}
        for line in SATURN_MAP_TSV.read_text().splitlines():
            if line.startswith("#") or line.startswith("saturn_song"):
                continue
            f = line.split("\t")
            if f[3] != "-":
                want[int(f[3], 0)] = int(f[0], 16)
        got = {c: s for c, (s, _) in plays.items()}
        if got != want:
            raise AssertionError(
                f"{Path(trigger_map).name} cmd->song rows differ from "
                f"{SATURN_MAP_TSV.name}")

    # loop-table manifest cross-check (warn-only)
    ref = {}
    if LOOPTABLE_TSV.exists():
        rows = LOOPTABLE_TSV.read_text().splitlines()
        hdr = rows[0].split("\t")
        for line in rows[1:]:
            f = dict(zip(hdr, line.split("\t")))
            ref[int(f["song"], 16)] = (int(f["intro_bytes"]),
                                       int(f["loop_len_frames"]))

    proto = protocols.get_protocol(game)
    w = PackWriter(proto, title=f"{game} Saturn MUS arranged soundtrack",
                   default_rate=MUS_RATE)
    coef1, coef2 = adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF, MUS_RATE)

    all_songs = sorted({sid for d in discs for sid in d.songs})
    if game == "sfa2":
        # the sfa2 pack is the BASE game's soundtrack: songs 0x01..0x3b.
        # Gold carriers (SFZ2' / SF Collection disc 2) add Cammy's 0x3c,
        # which does not exist in base SFA2 -- storing it would change the
        # shipped 59-track pack for an unreachable song.
        all_songs = [s for s in all_songs if s <= 0x3b]
    track_of_song: dict[int, int] = {}
    src_pcm: dict[int, bytes] = {}
    for sid in all_songs:
        disc = next(d for d in discs if sid in d.songs)
        pair_flag, loop_forever = disc.flags[sid]
        pcm, intro_frames = disc.read_song_pcm(sid)
        total_frames = len(pcm) // 4
        if sid in ref:
            r_intro_b, r_loop_f = ref[sid]
            if (r_intro_b != intro_frames * 4
                    or r_loop_f != total_frames - intro_frames):
                print(f"[warn] song {sid:02X}: disc sizes differ from "
                      f"{LOOPTABLE_TSV.name} "
                      f"({intro_frames * 4}/{total_frames - intro_frames} vs "
                      f"{r_intro_b}/{r_loop_f})")
        loops = loop_forever == 0xff
        if pair_flag and not loops:
            print(f"[note] song {sid:02X}: intro+body pair played once "
                  f"(0.BIN byte7=00) — packed as one-shot")
        stream = adxcodec.encode(pcm, MUS_CHANNELS, MUS_RATE)
        meta = TrackMeta(sample_rate=MUS_RATE, channels=MUS_CHANNELS,
                         codec=CODEC_ADX, gain=0x7f, coef1=coef1, coef2=coef2,
                         name=f"S{sid:02X}", source=disc.image.name)
        if loops:
            loop_start, loop_end = intro_frames, total_frames
            meta.loop_start_sample = loop_start
            meta.loop_end_sample = loop_end
            meta.loop_start_byte = adxcodec.samples_to_stream_byte(
                loop_start, MUS_CHANNELS)
            meta.loop_end_byte = adxcodec.samples_to_stream_byte(
                loop_end, MUS_CHANNELS)
            stream = stream[:meta.loop_end_byte]
        ti = w.add_track(stream, meta)
        track_of_song[sid] = ti
        if crosscheck and len(src_pcm) < 2 and loops and intro_frames:
            src_pcm[ti] = pcm

    n_play = 0
    for cmd, (sid, gain) in sorted(plays.items()):
        if sid not in track_of_song:
            raise SystemExit(
                f"cmd 0x{cmd:02x} -> song {sid:02X} is not on this disc — "
                f"wrong disc for game {game!r}? (sfz2al needs the SFZ2' "
                f"dash disc, which carries all 60 songs)")
        w.set_trigger(cmd, TriggerRow(
            verb=VERB_PLAY, track=track_of_song[sid],
            gain=TRIG_GAIN.get(game, min(gain if gain is not None else 0x60, 0x7f)),
            suppress=1))
        n_play += 1
    for cmd in stops:
        # suppress=0 on a STOP row, deliberately.  Gating a stop keeps the
        # native driver from stopping its OWN audio, and on this board those
        # two commands are also what the boot sequence uses for the QSound
        # jingle: measured in MAME, sfz2al issues 0x3e/0x3f at frame 531/532
        # and the jingle sounds from 8.9 s to 17 s.  Suppressed, the Z80 never
        # sees them, nothing arranged plays either (it IS a stop), and the
        # jingle is simply gone -- reproduced with the gating prototype (mean
        # RMS 1114 stock -> 0), and restored to exactly stock levels with the
        # bit cleared.  Passing a stop through costs nothing: the player still
        # gets the stop event and drops the arranged track.
        w.set_trigger(cmd, TriggerRow(verb=VERB_STOP, suppress=0))

    out_path = Path(out) if out else PACKS_DIR / f"{game}_arrange.cpk"
    w.write(out_path)
    size = out_path.stat().st_size
    print(f"[saturn] {out_path}  {size / 1e6:.1f} MB, {len(w.tracks)} tracks,"
          f" {n_play} play + {len(stops)} stop triggers")

    if crosscheck and src_pcm:
        from .format import PackReader
        rd = PackReader(out_path)
        for ti, pcm in src_pcm.items():
            m = rd.tracks[ti]
            n = m.data_length // 36 * 32
            dec = adxcodec.decode(rd.read_track(ti), 2, MUS_RATE,
                                  total_samples=n)
            ncc = _ncc(pcm, dec, MUS_RATE * 5)
            print(f"[saturn] track {ti} ({m.name}) encode fidelity NCC "
                  f"(first 5 s) = {ncc:.4f}")
            if ncc < 0.90:
                raise AssertionError(f"track {ti}: ADX encode NCC {ncc:.3f} "
                                     f"< 0.90 — encoder problem?")
        rd.close()
    for d in discs:
        d.iso.close()
    return out_path


def _ncc(a_pcm: bytes, b_pcm: bytes, frames: int) -> float:
    """Normalized cross-correlation of two s16le stereo buffers at lag 0."""
    import array
    n = min(len(a_pcm), len(b_pcm), frames * 4) // 2
    a = array.array("h")
    a.frombytes(a_pcm[:n * 2])
    b = array.array("h")
    b.frombytes(b_pcm[:n * 2])
    sab = saa = sbb = 0
    for x, y in zip(a, b):
        sab += x * y
        saa += x * x
        sbb += y * y
    return sab / ((saa * sbb) ** 0.5 + 1e-9)
