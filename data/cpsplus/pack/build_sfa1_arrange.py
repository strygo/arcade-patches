"""`build_pack.py zero1-arrange` — SFA1 ARRANGE pack from the SF Alpha Anthology.

Builds `sfa1_arrange.cpk`: the 13 arranged SFA1 stage themes (Anthology
Y_DATA entries 141..153, 48 kHz stereo ADX) with HUMAN-AUTHORED loop points
from `the internal listening archive` — NOT the ADX container
loops (these arrange entries carry no header loop: loop_flag == 0).

Trigger keying reuses build_zero1's VALIDATED structure verbatim: the zero1
dispatch table maps arcade command (= table_key - 0x38) -> in-game Y_DATA entry
(e97..e109 = SFA1's 13 stage themes).  This builder substitutes each in-game
entry with the SAME-CHARACTER arrange entry.

The in-game (CPS2) bank and the arrange bank are a CHARACTER PERMUTATION, NOT
a constant offset (an early "+44" guess was WRONG — e.g. it mis-paired Gouki
and Sodom/Chun-Li).  The permutation is taken from the Hyper sound-test FAQ:
  CPS2 order   0x41..0x4d (e97..e109): Ryu Ken Chun-Li Sagat Sodom Adon
                                       Birdie Guy Nash Rose Gouki Vega Dan
  arrange order e141..e153           : Ryu Ken Gouki Nash Chun-Li Adon Sodom
                                       Guy Birdie Rose Vega Sagat Dan
Matching characters gives INGAME_TO_ARRANGE below (also consistent with the
single-cycle chroma cross-correlation).  LOOPS.tsv is already keyed by arrange
entry, so once the arrange entry is chosen its authored loop follows.

Scope: only the 13 stage themes have authored arrange loops, so only they are
mapped.  build_zero1's silence-row drops and the non-stage music rows
(e110..e131, no arrange loops in scope) fall through to QSound; control
commands (0xffxx) are unaffected (sfa1 protocol descriptor).

Loop handling (both methods are byte-exact — the ORIGINAL ADX frame bytes are
reused with no decode/re-encode, so there is no added quantisation):
  * Loop points are rounded to the 32-sample ADX frame grid (reported per
    track); max rounding here is <16 samples (<0.34 ms).
  * method=natural: a clean hard cut.  The stream is truncated at the rounded
    loop_end; the player wraps loop_end->loop_start.
  * method=crossfade (nash/gouki/rose/birdie): the good-continuity musical
    loop points are kept and XFADE_SAMPLES of natural continuation past
    loop_end (the "tail") is retained in the stored stream.  The FPGA player
    equal-power blends the tail against the loop head at PLAYBACK time
    (rtl/cpsplus_player.v, pack/xfade.py) — no bake, no re-encode, so the
    ~33 dB SNR loss of the old baked-and-re-encoded crossfade is gone.  The
    per-track crossfade-enable bit + the pack's global xfade_samples header
    field (format v1) drive it.
"""
from __future__ import annotations

from pathlib import Path

from . import adxcodec, protocols
from .afs import AfsArchive
from .build_common import PACKS_DIR, PKG_ROOT
from .format import PackWriter, TrackMeta, TriggerRow, CODEC_ADX, VERB_PLAY
from .isofs import IsoFS
from .sources import resolve_image

# Measured loudness match (EBU R128 vs the native chip music,
# tools/loudness_match.py).  Shipped packs carried this in their trigger rows
# via a post-build set_gain.py patch, which a from-source rebuild silently
# discarded; baked here so rebuilds reproduce the shipped packs byte-exactly.
TRIG_GAIN = 0x22   # replaces the per-row source vol, as set_gain.py did

# WRONG KEYING SHIPPED ONCE -- see the internal hardware notes. The arcade's stage-theme
# commands are 0x01..0x0c (Ryu Ken Chun-Li Sagat Sodom Adon Birdie Guy Nash Rose
# Gouki Vega), confirmed by a service-mode sweep plus ear identification of every
# isolated song. 0x0d..0x15 are UI cues (attract, character select, vs,
# post-fight, continue, new challenger, game over, score) and must stay UNMAPPED
# so they fail open to the native chip -- the arrange album never covered them.
# Dan's theme has no command located yet, so track 12 is currently unreachable.
ARCADE_STAGE_CMDS = {          # arcade command -> character
    0x01: "ryu",   0x02: "ken",    0x03: "chunli", 0x04: "sagat",
    0x05: "sodom", 0x06: "adon",   0x07: "birdie", 0x08: "guy",
    0x09: "nash",  0x0a: "rose",   0x0b: "gouki",  0x0c: "vega",
    0x32: "dan",   # hidden character -- his theme sits outside the roster run
}
ARCADE_JOIN_OFFSET = 0x38            # arcade_cmd + 0x38 = zero1 table key

# The comp2-derived keys land the 13 stage themes on 0x09..0x15, but the
# service-mode sweep + listening established the real arcade stage commands
# as 0x01..0x0c with the 13th theme on 0x32; 0x0d..0x15 are UI cues that must
# stay UNMAPPED so they fail open to the native chip (see rekey_triggers.py,
# which applied exactly this correction to the shipped pack post-build).
# Baked here so a from-source rebuild reproduces the shipped keying.
CMD_REKEY = {**{c: c - 8 for c in range(0x09, 0x15)}, 0x15: 0x32}
STAGE_ENTRIES = range(97, 110)       # e97..e109 -> the 13 stage themes

# in-game (CPS2) Y_DATA entry -> same-character arrange entry (character
# permutation from the Hyper sound-test FAQ; see module docstring).
INGAME_TO_ARRANGE = {
    97: 141,   # Ryu
    98: 142,   # Ken
    99: 145,   # Chun-Li
    100: 152,  # Sagat
    101: 147,  # Sodom
    102: 146,  # Adon
    103: 149,  # Birdie
    104: 148,  # Guy
    105: 144,  # Nash
    106: 150,  # Rose
    107: 143,  # Gouki
    108: 151,  # Vega
    109: 153,  # Dan
}
# Authored loop points live in tracked manifests/ (the human ear-adjudicated
# result is the pack's crux, so it must be reproducible from a clean checkout).
# Fall back to the work/ audition copy if the manifest is absent.
LOOPS_TSV = PKG_ROOT / "manifests" / "sfa1_arrange_loops.tsv"
SATURN_LOOPS_TSV = (PKG_ROOT / "manifests"
                    / "sfa1_arrange_loops_saturn.tsv")
FULLCOV_TSV = (PKG_ROOT / "manifests"
               / "sfa1_fullcov_candidate_map.tsv")
# Saturn CD-DA extraction cache (full-coverage extraction);
# refilled from the user-supplied --disc when incomplete
SATURN_CD_DIR = (PKG_ROOT / "work" / "intermediate"
                 / "sfa1_fullcov" / "saturn_cdda")
# Whole-track repeat for cues the arcade loops; everything else plays once
# and ends in the CD master's own finish. Ear-gated (rev 4).
FULLCOV_LOOP_ROLES = {"select", "continue", "ending", "ending_dup"}
TRIGGER_TSV = (PKG_ROOT / "manifests"
               / "sfa1_arrange_trigger_map.tsv")
if not LOOPS_TSV.exists():
    LOOPS_TSV = (PKG_ROOT / "work" / "intermediate" /
                 "sfa1_arrange_loops" / "LOOPS.tsv")
# Global loop-crossfade length (samples).  150 ms at 48 kHz = 7200 = 225 ADX
# frames (frame-aligned).  MUST equal the RTL player XFADE_N parameter, which
# the fitter builds the weight LUT (rtl/cpsplus_xf_lut.hex) for.
XFADE_SAMPLES = 7200


# ------------------------------------------------------------- loop table ----
def _load_loops(path: Path) -> dict[int, dict]:
    """entry(int e.g. 141) -> {char, ls, le, method, quality}."""
    rows = path.read_text().splitlines()
    hdr = rows[0].split("\t")
    out: dict[int, dict] = {}
    for line in rows[1:]:
        if not line.strip():
            continue
        f = dict(zip(hdr, line.split("\t")))
        entry = int(f["entry"].lstrip("eE"))
        out[entry] = {
            "char": f["char"],
            "ls": int(f["loop_start_sample"]),
            "le": int(f["loop_end_sample"]),
            "method": f["method"].strip(),
            "quality": float(f["quality"]),
        }
    return out


def _round32(v: int) -> int:
    return (v + 16) // 32 * 32


# ----------------------------------------------------------- track builders ---
def _pcm_loop_track(pcm: bytes, rate: int, ch: int, ls: int, le: int,
                    method: str, *, name: str, source: str,
                    gain: int = 0x7f) -> tuple[bytes, TrackMeta]:
    """Saturn CD-DA source: ADX-encode, then cut per the loop method.
    natural = truncate at loop_end; crossfade = keep XFADE_SAMPLES of the
    source's own continuation past loop_end for the player's runtime blend."""
    stream = adxcodec.encode(pcm, ch, rate)
    c1, c2 = adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF, rate)
    ls_byte = adxcodec.samples_to_stream_byte(ls, ch)
    le_byte = adxcodec.samples_to_stream_byte(le, ch)
    meta = TrackMeta(sample_rate=rate, channels=ch, codec=CODEC_ADX,
                     gain=gain, coef1=c1, coef2=c2, name=name, source=source,
                     loop_start_sample=ls, loop_start_byte=ls_byte,
                     loop_end_sample=le, loop_end_byte=le_byte)
    if method == "crossfade":
        end_byte = le_byte + adxcodec.samples_to_stream_byte(XFADE_SAMPLES, ch)
        if len(stream) < end_byte:
            raise ValueError(f"{name}: no room for the crossfade tail")
        meta.xfade_enable = 1
        return stream[:end_byte], meta
    return stream[:le_byte], meta



def _natural_track(raw: bytes, ls: int, le: int, *, name: str, source: str,
                   gain: int = 0x7f) -> tuple[bytes, TrackMeta, dict]:
    """Hard-cut loop: reuse the source ADX frame bytes truncated at loop_end
    (byte-exact, no re-encode)."""
    info = adxcodec.parse_header(raw)
    ch = info.channels
    ls_byte = adxcodec.samples_to_stream_byte(ls, ch)
    le_byte = adxcodec.samples_to_stream_byte(le, ch)
    stream = raw[info.data_offset:info.data_offset + le_byte]
    if len(stream) != le_byte:
        raise ValueError(f"{name}: source truncated ({len(stream)} < "
                         f"{le_byte} loop_end bytes)")
    coef1, coef2 = adxcodec.calc_coeffs(info.cutoff or adxcodec.DEFAULT_CUTOFF,
                                        info.sample_rate)
    meta = TrackMeta(sample_rate=info.sample_rate, channels=ch,
                     codec=CODEC_ADX, gain=gain, coef1=coef1, coef2=coef2,
                     name=name, source=source,
                     loop_start_sample=ls, loop_start_byte=ls_byte,
                     loop_end_sample=le, loop_end_byte=le_byte)
    return stream, meta, {"reencoded": False}


def _crossfade_track(raw: bytes, ls: int, le: int, *, name: str, source: str,
                     gain: int = 0x7f, xfade_samples: int = XFADE_SAMPLES
                     ) -> tuple[bytes, TrackMeta, dict]:
    """Crossfade loop, stored byte-exact: reuse the ORIGINAL ADX frame bytes
    but keep `xfade_samples` of natural continuation past loop_end (the tail).
    The player decodes the tail and equal-power blends it against the loop head
    at playback (rtl/cpsplus_player.v).  No decode/re-encode, no SNR loss; the
    loop points stay the good-continuity musical points."""
    info = adxcodec.parse_header(raw)
    ch = info.channels
    ls_byte = adxcodec.samples_to_stream_byte(ls, ch)
    le_byte = adxcodec.samples_to_stream_byte(le, ch)
    tail_byte = adxcodec.samples_to_stream_byte(xfade_samples, ch)
    end_byte = le_byte + tail_byte
    stream = raw[info.data_offset:info.data_offset + end_byte]
    if len(stream) != end_byte:
        raise ValueError(f"{name}: source too short for the crossfade tail "
                         f"({len(stream)} < {end_byte}; need {xfade_samples} "
                         f"samples of continuation past loop_end)")
    coef1, coef2 = adxcodec.calc_coeffs(info.cutoff or adxcodec.DEFAULT_CUTOFF,
                                        info.sample_rate)
    meta = TrackMeta(sample_rate=info.sample_rate, channels=ch,
                     codec=CODEC_ADX, gain=gain, coef1=coef1, coef2=coef2,
                     name=name, source=source, xfade_enable=1,
                     loop_start_sample=ls, loop_start_byte=ls_byte,
                     loop_end_sample=le, loop_end_byte=le_byte)
    return stream, meta, {"reencoded": False, "xfade_samples": xfade_samples,
                          "tail_bytes": tail_byte}


# ------------------------------------------------------------------ build ----
def _load_fullcov(path: Path):
    """(cmd, trNN, role, char) rows that carry a Saturn track and passed the
    ear gate; fail_open rows are deliberately absent from the pack."""
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("cmd"):
            continue
        f = line.split("\t")
        if len(f) >= 6 and f[1].startswith("tr") and f[5] == "ear_confirmed":
            rows.append((int(f[0], 0), f[1], f[2], f[3]))
    return rows


def _load_saturn_loops(path: Path):
    """char -> {tr, entry, ls, le, method} from the Saturn-coordinate
    manifest (derived from the ear-adjudicated 48k points; see its header)."""
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("char"):
            continue
        f = line.split("\t")
        out[int(f[2][1:])] = dict(char=f[0], tr=f[1], ls=int(f[3]),
                                  le=int(f[4]), method=f[5])
    return out


def build(disc: str, out: str | None = None,
          loops_path: str | None = None,
          trigger_map: str | None = None) -> Path:
    loops = _load_saturn_loops(Path(loops_path) if loops_path
                               else SATURN_LOOPS_TSV)

    # arcade cmd -> in-game stage entry, from the tracked snapshot manifest
    # (see its header for the full derivation chain and its verification)
    map_tsv = Path(trigger_map) if trigger_map else TRIGGER_TSV
    stage_cmds: dict[int, int] = {}
    for line in map_tsv.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("cmd"):
            continue
        f = line.split("\t")
        stage_cmds[int(f[0], 0)] = int(f[1])
    # consistency asserts vs the tracked derivation constants -- the manifest
    # and the code-side evidence must never drift apart
    expect = {}
    for key, entry in protocols.ZERO1_MUSIC_MAP.items():
        cmd = key - ARCADE_JOIN_OFFSET
        if cmd > 0 and entry in STAGE_ENTRIES:
            expect[CMD_REKEY.get(cmd, cmd)] = entry
    if stage_cmds != expect:
        raise AssertionError(
            f"{map_tsv.name} disagrees with the derivation constants "
            f"(protocols.ZERO1_MUSIC_MAP + CMD_REKEY)")

    proto = protocols.get_protocol("sfa1")
    w = PackWriter(proto, title="SFA1 arranged soundtrack (SF Alpha "
                                "Anthology, authored loops)",
                   xfade_samples=XFADE_SAMPLES)

    # all audio -- stages AND the full-coverage cues -- comes from the ONE
    # Saturn disc; fill the extraction cache up front
    from .adxencode import read_wav
    from .discsrc import ensure_audio_cache
    fullcov = _load_fullcov(FULLCOV_TSV)
    needed = ({lp["tr"] for lp in loops.values()}
              | {tr for _, tr, _, _ in fullcov})
    ensure_audio_cache(SATURN_CD_DIR, needed, disc, "--disc", pregap="trim")

    track_of_arr: dict[int, int] = {}
    trigger_rows: list[tuple[int, int, int, str, str, int, int]] = []
    for cmd, in_game in sorted(stage_cmds.items()):
        arr = INGAME_TO_ARRANGE[in_game]
        if arr not in loops:
            raise AssertionError(f"no Saturn-loops row for arrange entry "
                                 f"{arr} (in-game {in_game}, cmd 0x{cmd:02x})")
        lp = loops[arr]
        if arr not in track_of_arr:
            pcm, rate, ch, n = read_wav(SATURN_CD_DIR / f"{lp['tr']}.wav")
            stream, meta = _pcm_loop_track(
                pcm, rate, ch, lp["ls"], lp["le"], lp["method"],
                name=f"{lp['char']}_arr_{lp['tr']}",
                source=f"sfa1_saturn/{lp['tr']}.wav[0:{n}]")
            track_of_arr[arr] = w.add_track(stream, meta)
        ti = track_of_arr[arr]
        w.set_trigger(cmd, TriggerRow(verb=VERB_PLAY, track=ti,
                                      gain=TRIG_GAIN, suppress=1))
        trigger_rows.append((cmd, in_game, arr, lp["char"], lp["method"],
                             lp["ls"], lp["le"]))

    # ---- full-coverage rows: endings/utilities/credit rolls,
    # manifests/sfa1_fullcov_candidate_map.tsv, ear-gated rev 4 -------------
    c1, c2 = adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF, 44100)
    track_of_tr: dict[str, int] = {}
    n_cov = 0
    for cmd, tr, role, char in sorted(fullcov):
        if tr not in track_of_tr:
            pcm, rate, ch, n = read_wav(SATURN_CD_DIR / f"{tr}.wav")
            stream = adxcodec.encode(pcm, ch, rate)
            meta = TrackMeta(sample_rate=rate, channels=ch, codec=CODEC_ADX,
                             gain=0x7f, coef1=c1, coef2=c2,
                             name=f"{role}_{tr}",
                             source=f"sfa1_saturn/{tr}.wav[0:{n}]")
            if role in FULLCOV_LOOP_ROLES:
                le = n // 32 * 32          # ADX frame grid (<0.8 ms trimmed)
                meta.loop_start_sample = 0
                meta.loop_end_sample = le
                meta.loop_start_byte = 0
                meta.loop_end_byte = adxcodec.samples_to_stream_byte(le, ch)
                stream = stream[:meta.loop_end_byte]
            track_of_tr[tr] = w.add_track(stream, meta)
        w.set_trigger(cmd, TriggerRow(verb=VERB_PLAY, track=track_of_tr[tr],
                                      gain=TRIG_GAIN, suppress=1))
        n_cov += 1
        kind = "loop" if role in FULLCOV_LOOP_ROLES else "once"
        print(f"[sfa1] 0x{cmd:02x} -> {tr} {role:18s} {char:8s} {kind}")

    out_path = Path(out) if out else PACKS_DIR / "sfa1_arrange.cpk"
    w.write(out_path)

    size = out_path.stat().st_size
    print(f"[sfa1] {out_path}  {size / 1e6:.1f} MB, "
          f"{len(w.tracks)} tracks, {len(trigger_rows) + n_cov} play triggers "
          f"(single Saturn disc source)")
    print("[sfa1] arcade_cmd  in-game  arrange  char     method     "
          "loop_start  loop_end  (44.1k samples)")
    for cmd, ig, arr, char, method, ls, le in trigger_rows:
        print(f"    0x{cmd:02x}        e{ig}    e{arr}   {char:<8} {method:<9} "
              f"{ls:>9}  {le:>9}")
    return out_path
