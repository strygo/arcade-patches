"""Community authoring: `init <game>` scaffolds a pack project, `build <dir>`
compiles it (PACK_FORMAT.md §Authoring).

A pack project is a directory of ordinary audio files plus one pack.toml.
`init` pre-fills the trigger worksheet from the game's known command
inventory (HSF2: the verified ELF-table manifest; SFA1: the verified
Anthology map; sfz2al: the zero6/Saturn join; other games get a blank
worksheet until their Phase-0 census lands).
"""
from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

from . import adxcodec, protocols
from .build_common import MANIFESTS
from .format import (PackWriter, TrackMeta, TriggerRow, CODEC_ADX, CODEC_PCM,
                     VERB_PLAY, VERB_STOP)

FFPROBE = "/opt/homebrew/bin/ffprobe"


# ------------------------------------------------------------------ init ----
def _inventory(game: str) -> list[tuple[int, str]]:
    """Known (command, label) pairs for the trigger worksheet."""
    inv: list[tuple[int, str]] = []
    if game == "hsf2":
        path = MANIFESTS / "hsf2_bgm_command_map.tsv"
        rows = path.read_text().splitlines()
        hdr = rows[0].split("\t")
        seen = set()
        for line in rows[1:]:
            f = dict(zip(hdr, line.split("\t")))
            cmd = int(f["cmd"], 16)
            if f["set"] == "ARRANGE" and cmd and cmd not in seen:
                seen.add(cmd)
                label = f["name"] or f["afs_name"]
                if f["in_game"] != "yes":
                    label += f" [{f['in_game']}]"
                inv.append((cmd, label))
    elif game == "sfa1":
        for cmd, entry in sorted(protocols.ZERO1_MUSIC_MAP.items()):
            inv.append((cmd, f"SFA1 arranged track (Y_DATA entry {entry})"))
    elif game == "sfz2al":
        path = MANIFESTS / "sfz2_saturn_zero6_map.tsv"
        if path.exists():
            for line in path.read_text().splitlines():
                if line.startswith("#") or line.startswith("saturn_song"):
                    continue
                f = line.split("\t")
                if f[3] != "-":
                    note = f[13] if len(f) > 13 and f[13] else ""
                    inv.append((int(f[3], 0),
                                f"Saturn song {f[0]}" +
                                (f" — {note}" if note else "")))
            inv.sort()
    return inv


def init(game: str, dest: str | None = None) -> Path:
    d = Path(dest) if dest else Path(f"{game}_pack")
    d.mkdir(parents=True, exist_ok=True)
    (d / "audio").mkdir(exist_ok=True)
    inv = _inventory(game)
    lines = [
        "# CPS2 arranged-audio pack project (see pack/README.md)",
        f"# build with: build_pack.py build {d}/",
        "",
        "[pack]",
        f'game   = "{game}"',
        f'title  = "My {game} pack"',
        'author = ""',
        "",
        "# One [tracks.<name>] block per audio file.  Any format/rate ffmpeg",
        "# reads.  Loop points: integer = samples, string = \"m:ss.mmm\".",
        "# Omit loop_start/loop_end with loop=true for a whole-file loop.",
        "#",
        "# [tracks.ryu_stage]",
        '# file       = "audio/ryu_stage.flac"',
        "# loop       = true",
        '# loop_start = "0:12.500"',
        "# gain       = 1.0        # linear, 1.0 = unity",
        '# codec      = "adx"      # or "pcm" (bigger, bring-up only)',
        "",
        "[triggers]",
        "# command -> track name.  Also allowed:",
        '#   0x0003 = "stop"                            # explicit stop row',
        '#   0x0013 = { track = "x", gain = 0.9 }       # per-row options',
    ]
    if inv:
        lines.append("# Known music commands for this game:")
        for cmd, label in inv:
            lines.append(f'# 0x{cmd:04x} = ""    # {label}')
    else:
        lines.append(f"# (no Phase-0 command inventory for {game!r} yet — "
                     "trace the game in MAME to enumerate its music "
                     "commands; see PACK_FORMAT.md)")
    toml_path = d / "pack.toml"
    if toml_path.exists():
        raise FileExistsError(f"{toml_path} already exists")
    toml_path.write_text("\n".join(lines) + "\n")
    print(f"[init] scaffolded {toml_path} "
          f"({len(inv)} known commands pre-filled)")
    return d


# ----------------------------------------------------------------- build ----
def _probe(path: Path) -> tuple[int, int]:
    """(sample_rate, channels) of an audio file via ffprobe."""
    if not shutil.which(FFPROBE) and not Path(FFPROBE).exists():
        print(f"[warn] ffprobe not found at {FFPROBE}; assuming 44100/2")
        return 44100, 2
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {out.stderr[:200]}")
    rate, ch = out.stdout.strip().split(",")[:2]
    return int(rate), int(ch)


def _parse_time(v, rate: int) -> int:
    """Loop point: int = samples; 'm:ss.mmm' or 'ss.mmm' string = time."""
    if isinstance(v, int):
        return v
    s = str(v)
    if ":" in s:
        mins, secs = s.split(":", 1)
        t = int(mins) * 60 + float(secs)
    else:
        t = float(s)
    return round(t * rate)


def _align32(name: str, label: str, v: int) -> int:
    a = (v + 16) // 32 * 32
    if a != v:
        print(f"[warn] {name}: {label} {v} rounded to ADX frame boundary "
              f"{a} ({(a - v)} samples)")
    return a


def build(project_dir: str, out: str | None = None) -> Path:
    d = Path(project_dir)
    cfg = tomllib.loads((d / "pack.toml").read_text())
    pack = cfg.get("pack", {})
    game = pack.get("game")
    if not game:
        raise ValueError("pack.toml: [pack] game is required")
    proto = protocols.get_protocol(game)
    if game not in protocols.PROTOCOLS:
        print(f"[warn] no verified protocol descriptor for {game!r}; using "
              f"the generic QSound-family descriptor (validate in Phase 0)")
    w = PackWriter(proto, title=pack.get("title", ""))

    track_index: dict[str, int] = {}
    for name, spec in cfg.get("tracks", {}).items():
        f = d / spec["file"]
        if not f.exists():
            raise FileNotFoundError(f"track {name!r}: {f} missing")
        src_rate, src_ch = _probe(f)
        rate = int(spec.get("rate", src_rate))
        ch = int(spec.get("channels", min(src_ch, 2)))
        pcm = subprocess.run(
            [adxcodec.FFMPEG, "-hide_banner", "-loglevel", "error",
             "-i", str(f), "-f", "s16le", "-ar", str(rate), "-ac", str(ch),
             "pipe:1"], stdout=subprocess.PIPE, check=True).stdout
        n = len(pcm) // (2 * ch)
        loop = bool(spec.get("loop", "loop_start" in spec
                             or "loop_end" in spec))
        ls = _parse_time(spec.get("loop_start", 0), rate) if loop else 0
        le = _parse_time(spec.get("loop_end", n), rate) if loop else 0
        gain = min(round(float(spec.get("gain", 1.0)) * 127), 127)
        codec = spec.get("codec", "adx")
        meta = TrackMeta(sample_rate=rate, channels=ch, gain=gain, name=name,
                         source=str(spec["file"]))
        if codec == "adx":
            if loop:
                ls = _align32(name, "loop_start", ls)
                le = _align32(name, "loop_end", min(le, n))
                if le > n:      # never loop into encoder padding silence
                    le -= 32
            stream = adxcodec.encode(pcm, ch, rate)
            meta.codec = CODEC_ADX
            meta.coef1, meta.coef2 = adxcodec.calc_coeffs(
                adxcodec.DEFAULT_CUTOFF, rate)
            if loop:
                meta.loop_start_sample, meta.loop_end_sample = ls, le
                meta.loop_start_byte = adxcodec.samples_to_stream_byte(ls, ch)
                meta.loop_end_byte = adxcodec.samples_to_stream_byte(le, ch)
                stream = stream[:meta.loop_end_byte]
        elif codec == "pcm":
            stream = pcm
            meta.codec = CODEC_PCM
            if loop:
                le = min(le, n)
                meta.loop_start_sample, meta.loop_end_sample = ls, le
                meta.loop_start_byte = ls * 2 * ch
                meta.loop_end_byte = le * 2 * ch
                stream = stream[:meta.loop_end_byte]
        else:
            raise ValueError(f"track {name!r}: unknown codec {codec!r}")
        track_index[name] = w.add_track(stream, meta)

    n_rows = 0
    for key, val in cfg.get("triggers", {}).items():
        cmd = int(key, 0)
        if isinstance(val, str):
            spec = {"track": val}
        else:
            spec = dict(val)
        tname = spec.get("track", "")
        suppress = 1 if spec.get("suppress", True) else 0
        if tname == "stop":
            w.set_trigger(cmd, TriggerRow(verb=VERB_STOP, suppress=suppress))
        elif tname:
            if tname not in track_index:
                raise ValueError(f"trigger 0x{cmd:04x}: unknown track "
                                 f"{tname!r}")
            gain = min(round(float(spec.get("gain", 1.0)) * 127), 127)
            w.set_trigger(cmd, TriggerRow(
                verb=VERB_PLAY, track=track_index[tname], gain=gain,
                suppress=suppress))
        n_rows += 1

    mapped = {int(k, 0) for k in cfg.get("triggers", {})}
    unmapped = [c for c, label in _inventory(game)
                if c not in mapped and "[" not in label]
    if unmapped:
        print(f"[report] {len(unmapped)} known in-game music commands "
              f"unmapped (will fall through to QSound): "
              + " ".join(f"0x{c:02x}" for c in unmapped[:20])
              + (" ..." if len(unmapped) > 20 else ""))

    out_path = Path(out) if out else d / f"{game}.cpk"
    w.write(out_path)
    print(f"[build] {out_path}  {out_path.stat().st_size / 1e6:.1f} MB, "
          f"{len(w.tracks)} tracks, {n_rows} trigger rows")
    from .audition import verify
    verify(out_path)
    return out_path
